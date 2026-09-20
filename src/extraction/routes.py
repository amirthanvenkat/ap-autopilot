"""Extraction endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from src.common.acceptance import accept_push
from src.common.deps import Dependencies
from src.common.logging import get_logger
from src.common.outbox import HANDLER_EXTRACTION_COMPLETE
from src.extraction.worker import reap_stale_jobs

log = get_logger(__name__)

router = APIRouter()


class AcceptResponse(BaseModel):
    """What a push endpoint returns."""

    status: str = Field(description="accepted or duplicate")
    message_id: str
    task_id: str


def _deps(request: Request) -> Dependencies:
    deps: Dependencies = request.app.state.deps
    return deps


@router.post(
    "/internal/extraction/complete",
    response_model=AcceptResponse,
    summary="Pub/Sub push target for extraction completion",
)
async def extraction_complete(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AcceptResponse:
    """Accept the trigger and return.

    One batch produces several output objects and so several of these
    requests. They are distinct messages, they each get their own row, and
    the worker decides which one finds the operation finished.
    """
    deps = _deps(request)
    body = await request.body()
    accepted = await accept_push(
        deps,
        handler=HANDLER_EXTRACTION_COMPLETE,
        authorization=authorization,
        body=body,
    )
    return AcceptResponse(
        status="duplicate" if accepted.duplicate else "accepted",
        message_id=accepted.message_id,
        task_id=accepted.task_id,
    )


@router.post(
    "/internal/extraction/reap",
    summary="Resolve jobs stuck in RUNNING",
)
async def reap(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Scheduled.

    A failed Document AI operation writes no output, so no finalize event
    fires and nothing else would ever notice the job.
    """
    deps = _deps(request)
    await deps.verifier.verify(authorization)
    results = await reap_stale_jobs(deps)
    return {
        "reaped": len(results),
        "jobs": [{"job_id": item.job_id, "outcome": item.outcome} for item in results],
    }
