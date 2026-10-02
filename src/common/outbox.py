"""Message acceptance and the work outbox.

This module holds the one property the idempotency design depends on:
processed_events is never committed on its own. The marker row and the unit
of work are written by a single statement, so either both exist or neither
does.

Committing the marker first and then doing the work leaves a window where a
crash loses the work permanently: the redelivery sees the marker, concludes
the message was handled, and returns 200 with nothing to show for it.
Committing the work first and the marker second duplicates the work instead.
One statement removes the window in both directions.

It also makes concurrent redelivery safe without any lock of our own. Two
instances racing on the same message_id contend on the primary key. Postgres
blocks the second insert until the first transaction resolves, so the second
only ever sees "already accepted" after the work is durable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.common.config import Settings, get_settings
from src.common.ids import derived_id
from src.common.logging import get_logger

log = get_logger(__name__)

HANDLER_GMAIL_NOTIFY = "gmail_notify"
HANDLER_EXTRACTION_COMPLETE = "extraction_complete"
HANDLER_MATCH_INVOICE = "match_invoice"

_ACCEPT_SQL = text(
    """
    with claimed as (
        insert into processed_events (message_id, handler)
        values (:message_id, :handler)
        on conflict (message_id) do nothing
        returning message_id
    ), queued as (
        insert into ingestion_outbox (task_id, handler, message_id, payload)
        select :task_id, :handler, claimed.message_id, cast(:payload as jsonb)
        from claimed
        on conflict (task_id) do nothing
        returning task_id
    )
    select
        (select count(*) from claimed) as accepted,
        (select task_id from queued) as task_id
    """
)


@dataclass(frozen=True)
class Acceptance:
    """Outcome of the acceptance transaction."""

    task_id: str
    duplicate: bool


@dataclass(frozen=True)
class OutboxTask:
    """A claimed unit of work."""

    task_id: str
    handler: str
    message_id: str
    payload: dict[str, Any]
    attempts: int


def task_id_for(handler: str, message_id: str) -> str:
    """Derive the task id so a replay collides rather than duplicating."""
    return derived_id("outbox", handler, message_id)


async def accept_message(
    conn: AsyncConnection,
    *,
    handler: str,
    message_id: str,
    payload: dict[str, Any],
) -> Acceptance:
    """Record the message and queue its work in one statement.

    Returns duplicate=True when this message has already been accepted, in
    which case the caller returns 200 without doing anything further.
    """
    task_id = task_id_for(handler, message_id)
    row = (
        await conn.execute(
            _ACCEPT_SQL,
            {
                "message_id": message_id,
                "handler": handler,
                "task_id": task_id,
                "payload": json.dumps(payload, separators=(",", ":")),
            },
        )
    ).one()
    accepted = int(row.accepted) == 1
    return Acceptance(task_id=task_id, duplicate=not accepted)


_CLAIM_ONE_SQL = text(
    """
    update ingestion_outbox
       set status = 'LEASED',
           attempts = attempts + 1,
           leased_until = now() + make_interval(secs => :lease_seconds),
           updated_at = now()
     where task_id = (
        select task_id
          from ingestion_outbox
         where task_id = :task_id
           and (
                status = 'PENDING'
             or (status = 'LEASED' and leased_until < now())
           )
           for update skip locked
     )
    returning task_id, handler, message_id, payload, attempts
    """
)

_CLAIM_BATCH_SQL = text(
    """
    update ingestion_outbox
       set status = 'LEASED',
           attempts = attempts + 1,
           leased_until = now() + make_interval(secs => :lease_seconds),
           updated_at = now()
     where task_id in (
        select task_id
          from ingestion_outbox
         where status = 'PENDING'
            or (status = 'LEASED' and leased_until < now())
         order by created_at
           for update skip locked
         limit :limit
     )
    returning task_id, handler, message_id, payload, attempts
    """
)


async def claim_task(
    conn: AsyncConnection,
    task_id: str,
    *,
    settings: Settings | None = None,
) -> OutboxTask | None:
    """Lease one named task.

    Returns None when the task is already done, already leased by a live
    worker, or absent. All three mean this delivery has nothing to do.
    """
    cfg = settings or get_settings()
    row = (
        await conn.execute(
            _CLAIM_ONE_SQL,
            {"task_id": task_id, "lease_seconds": cfg.outbox_lease_seconds},
        )
    ).first()
    return _to_task(row)


async def claim_batch(
    conn: AsyncConnection,
    *,
    limit: int = 10,
    settings: Settings | None = None,
) -> list[OutboxTask]:
    """Lease up to limit tasks that are pending or whose lease has expired.

    This is the sweeper's query. It is what makes a failed publish cost
    latency rather than a lost invoice.
    """
    cfg = settings or get_settings()
    rows = (
        await conn.execute(
            _CLAIM_BATCH_SQL,
            {"limit": limit, "lease_seconds": cfg.outbox_lease_seconds},
        )
    ).all()
    tasks = [_to_task(row) for row in rows]
    return [task for task in tasks if task is not None]


def _to_task(row: Any) -> OutboxTask | None:
    if row is None:
        return None
    payload = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    return OutboxTask(
        task_id=row.task_id,
        handler=row.handler,
        message_id=row.message_id,
        payload=payload,
        attempts=int(row.attempts),
    )


_MARK_DONE_SQL = text(
    """
    update ingestion_outbox
       set status = 'DONE', leased_until = null, last_error = null, updated_at = now()
     where task_id = :task_id
    """
)

_MARK_FAILED_SQL = text(
    """
    update ingestion_outbox
       set status = 'FAILED', leased_until = null, last_error = :error,
           updated_at = now()
     where task_id = :task_id
    """
)

_RELEASE_SQL = text(
    """
    update ingestion_outbox
       set status = 'PENDING', leased_until = null, last_error = :error,
           updated_at = now()
     where task_id = :task_id
    """
)


async def mark_done(conn: AsyncConnection, task_id: str) -> None:
    await conn.execute(_MARK_DONE_SQL, {"task_id": task_id})


async def mark_failed(conn: AsyncConnection, task_id: str, error: str) -> None:
    """Terminal failure. Nothing will retry this task."""
    await conn.execute(_MARK_FAILED_SQL, {"task_id": task_id, "error": error[:4000]})


async def release_for_retry(
    conn: AsyncConnection,
    task: OutboxTask,
    error: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Return a transiently failed task to the queue.

    Gives up once the attempt budget is spent, because a transient failure
    that has survived five attempts is not transient.
    """
    cfg = settings or get_settings()
    if task.attempts >= cfg.outbox_max_attempts:
        await mark_failed(
            conn,
            task.task_id,
            f"exhausted {task.attempts} attempts: {error}",
        )
        return False
    await conn.execute(_RELEASE_SQL, {"task_id": task.task_id, "error": error[:4000]})
    return True


def lease_expiry(settings: Settings | None = None) -> timedelta:
    """How long a worker holds a task before the sweeper may take it back."""
    cfg = settings or get_settings()
    return timedelta(seconds=cfg.outbox_lease_seconds)
