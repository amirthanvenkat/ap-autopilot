"""Task dispatch and failure classification.

This is where the spec's contradiction is resolved. Section 4 asked the push
handler to return 200 for everything so a poison message is never
redelivered, and also to dead letter transient failures. Those cannot both
happen in one handler.

The split lives here instead. Acceptance is cheap and almost always
succeeds, so the handler returns 200 once the work is durable. Classifying
the failure is this runner's job: a transient failure goes back on the
queue, and a permanent one is recorded and stopped. A poison message ends up
visible in Postgres rather than silently acknowledged at the edge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.errors import TransientError, is_transient
from src.common.logging import get_logger
from src.common.outbox import (
    HANDLER_EXTRACTION_COMPLETE,
    HANDLER_GMAIL_NOTIFY,
    HANDLER_MATCH_INVOICE,
    OutboxTask,
    claim_batch,
    claim_task,
    mark_done,
    mark_failed,
    release_for_retry,
)

log = get_logger(__name__)

TaskHandler = Callable[[Dependencies, OutboxTask], Awaitable[Any]]

RESULT_DONE = "done"
RESULT_RETRY = "retry"
RESULT_FAILED = "failed"
RESULT_SKIPPED = "skipped"


@dataclass(frozen=True)
class TaskResult:
    """Outcome of running one task."""

    task_id: str
    result: str
    detail: str | None = None


def _handlers() -> dict[str, TaskHandler]:
    # Imported here so the worker modules can import the runner's types
    # without a cycle.
    from src.extraction.worker import process_extraction_complete
    from src.ingestion.worker import process_gmail_notification
    from src.matching.worker import process_match_invoice

    return {
        HANDLER_GMAIL_NOTIFY: process_gmail_notification,
        HANDLER_EXTRACTION_COMPLETE: process_extraction_complete,
        HANDLER_MATCH_INVOICE: process_match_invoice,
    }


async def run_task(deps: Dependencies, task: OutboxTask) -> TaskResult:
    """Run one claimed task and record what happened to it."""
    handlers = _handlers()
    handler = handlers.get(task.handler)

    if handler is None:
        async with transaction(deps.engine) as conn:
            await mark_failed(conn, task.task_id, f"unknown handler {task.handler}")
        log.error("worker.unknown_handler", handler=task.handler)
        return TaskResult(task.task_id, RESULT_FAILED, "unknown handler")

    try:
        await handler(deps, task)
    except TransientError as exc:
        async with transaction(deps.engine) as conn:
            retrying = await release_for_retry(
                conn, task, str(exc), settings=deps.settings
            )
        log.warning(
            "worker.transient_failure",
            task_id=task.task_id,
            handler=task.handler,
            attempts=task.attempts,
            retrying=retrying,
            error=str(exc),
        )
        return TaskResult(
            task.task_id,
            RESULT_RETRY if retrying else RESULT_FAILED,
            str(exc),
        )
    except Exception as exc:
        # Anything not explicitly transient is treated as permanent.
        # Retrying an unknown failure five times buys nothing and hides the
        # cause behind a dead letter.
        async with transaction(deps.engine) as conn:
            await mark_failed(conn, task.task_id, f"{type(exc).__name__}: {exc}")
        log.error(
            "worker.permanent_failure",
            task_id=task.task_id,
            handler=task.handler,
            error_type=type(exc).__name__,
            error=str(exc),
            transient=is_transient(exc),
            exc_info=True,
        )
        return TaskResult(task.task_id, RESULT_FAILED, str(exc))

    async with transaction(deps.engine) as conn:
        await mark_done(conn, task.task_id)
    log.info("worker.task_done", task_id=task.task_id, handler=task.handler)
    return TaskResult(task.task_id, RESULT_DONE)


async def run_named_task(deps: Dependencies, task_id: str) -> TaskResult:
    """Claim and run one named task.

    This is the work topic push path. A task that is already done, or leased
    by a live worker, is skipped rather than run twice.
    """
    async with transaction(deps.engine) as conn:
        task = await claim_task(conn, task_id, settings=deps.settings)
    if task is None:
        return TaskResult(task_id, RESULT_SKIPPED, "not claimable")
    return await run_task(deps, task)


async def sweep(deps: Dependencies, *, limit: int = 10) -> list[TaskResult]:
    """Claim and run pending work.

    The safety net for a publish that never landed, and the only delivery
    mechanism in fixtures mode. Recovery latency is the sweep interval,
    which is seconds against a three minute target.
    """
    async with transaction(deps.engine) as conn:
        tasks = await claim_batch(conn, limit=limit, settings=deps.settings)
    if not tasks:
        return []
    log.info("worker.sweep_claimed", count=len(tasks))
    return [await run_task(deps, task) for task in tasks]
