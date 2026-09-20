"""Operational endpoints: work dispatch, sweeping and visibility."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel
from sqlalchemy import text

from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.logging import get_logger
from src.common.runner import run_named_task, sweep

log = get_logger(__name__)

router = APIRouter()


class Health(BaseModel):
    """Liveness and database reachability."""

    status: str
    database: str
    fixtures_mode: bool


def _deps(request: Request) -> Dependencies:
    deps: Dependencies = request.app.state.deps
    return deps


@router.get("/health", response_model=Health, summary="Health and keepalive")
async def health(request: Request) -> Health:
    """Also the Neon keepalive target.

    The free tier suspends idle compute after five minutes and resuming can
    take seconds, which would land on whichever invoice arrives first after
    a quiet spell. A scheduled ping every four minutes avoids that.
    """
    deps = _deps(request)
    database = "ok"
    try:
        async with transaction(deps.engine) as conn:
            await conn.execute(text("select 1"))
    except Exception as exc:
        log.warning("health.database_unreachable", error=str(exc))
        database = "unreachable"
    return Health(
        status="ok",
        database=database,
        fixtures_mode=deps.settings.replay_fixtures,
    )


@router.post("/internal/work", summary="Work topic push target")
async def run_work(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Run one queued task.

    This is where the slow work happens: Gmail reads, object writes and
    Document AI calls. It is a separate endpoint precisely so the push
    handlers stay inside their budget.
    """
    deps = _deps(request)
    await deps.verifier.verify(authorization)
    body = await request.json()
    task_id = _task_id_from(body)
    if not task_id:
        return {"status": "ignored", "reason": "no task_id"}
    result = await run_named_task(deps, task_id)
    return {"status": result.result, "task_id": result.task_id, "detail": result.detail}


def _task_id_from(body: Any) -> str | None:
    """Accept either a plain body or a Pub/Sub push envelope."""
    import base64
    import json

    if not isinstance(body, dict):
        return None
    direct = body.get("task_id")
    if isinstance(direct, str) and direct:
        return direct
    message = body.get("message")
    if not isinstance(message, dict):
        return None
    encoded = message.get("data")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        parsed = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(parsed, dict):
        value = parsed.get("task_id")
        return value if isinstance(value, str) and value else None
    return None


@router.post("/internal/sweep", summary="Claim and run pending work")
async def run_sweep(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Scheduled.

    The safety net for a publish that never landed, and the only delivery
    mechanism in fixtures mode.
    """
    deps = _deps(request)
    await deps.verifier.verify(authorization)
    results = await sweep(deps, limit=limit)
    return {
        "claimed": len(results),
        "tasks": [
            {"task_id": item.task_id, "result": item.result, "detail": item.detail}
            for item in results
        ],
    }


@router.get("/internal/deadletter", summary="List dead lettered messages")
async def list_dead_letters(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Make dead lettered messages visible rather than silently lost.

    This lists them without acknowledging, so inspection does not consume
    the evidence. It is not an alert: a misconfigured audience dead letters
    every message, and nobody watches a list. Alert on subscription depth
    as well.
    """
    deps = _deps(request)
    await deps.verifier.verify(authorization)
    publisher = deps.publisher
    puller = getattr(publisher, "pull_dead_letters", None)
    if puller is None:
        return {"messages": [], "note": "publisher does not support pulling"}
    messages = await puller(limit)
    return {
        "count": len(messages),
        "messages": [
            {
                "message_id": item.message_id,
                "publish_time": item.publish_time,
                "delivery_attempt": item.delivery_attempt,
                "attributes": item.attributes,
            }
            for item in messages
        ],
    }


@router.get("/internal/outbox", summary="Outbox contents by status")
async def outbox_summary(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Counts by status, plus the most recent failures.

    A FAILED outbox row is a poison message that was accepted, recorded and
    stopped. It is visible here rather than acknowledged away at the edge.
    """
    deps = _deps(request)
    await deps.verifier.verify(authorization)
    async with transaction(deps.engine) as conn:
        counts = (
            await conn.execute(
                text(
                    "select status, count(*) as total "
                    "from ingestion_outbox group by status order by status"
                )
            )
        ).all()
        failures = (
            await conn.execute(
                text(
                    "select task_id, handler, attempts, last_error, updated_at "
                    "from ingestion_outbox where status = 'FAILED' "
                    "order by updated_at desc limit 20"
                )
            )
        ).all()
    return {
        "counts": {row.status: int(row.total) for row in counts},
        "recent_failures": [
            {
                "task_id": row.task_id,
                "handler": row.handler,
                "attempts": int(row.attempts),
                "last_error": row.last_error,
                "updated_at": row.updated_at,
            }
            for row in failures
        ],
    }
