"""Ingestion endpoints.

The Gmail push target does nothing but accept. The upload endpoint is the
development path, and it is the one the Postman collection drives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response, UploadFile
from fastapi import File as FileParam
from pydantic import BaseModel, Field

from src.common.acceptance import accept_push
from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.errors import PayloadTooLargeError
from src.common.logging import get_logger
from src.common.outbox import HANDLER_GMAIL_NOTIFY
from src.ingestion import repository
from src.ingestion.service import SOURCE_UPLOAD, ingest_bytes
from src.ingestion.worker import renew_gmail_watch

log = get_logger(__name__)

router = APIRouter()


class AcceptResponse(BaseModel):
    """What a push endpoint returns."""

    status: str = Field(description="accepted or duplicate")
    message_id: str
    task_id: str


class DocumentAccepted(BaseModel):
    """Response for an upload."""

    document_id: str
    status: str
    received_at: datetime
    content_hash: str
    duplicate: bool = False


class DocumentStatus(BaseModel):
    """Current state of a document and, when ready, its extraction."""

    document_id: str
    status: str
    content_hash: str
    media_type: str
    byte_size: int
    received_at: datetime
    job_id: str | None = None
    extraction_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    extraction: dict[str, Any] | None = None


def _deps(request: Request) -> Dependencies:
    deps: Dependencies = request.app.state.deps
    return deps


@router.post(
    "/internal/gmail/notify",
    response_model=AcceptResponse,
    summary="Pub/Sub push target for Gmail notifications",
)
async def gmail_notify(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AcceptResponse:
    """Verify, record, queue, return.

    No Gmail call happens here. The body is not even fully parsed beyond
    the message id, because everything else can be done by a worker that is
    not holding an open request.
    """
    deps = _deps(request)
    body = await request.body()
    accepted = await accept_push(
        deps,
        handler=HANDLER_GMAIL_NOTIFY,
        authorization=authorization,
        body=body,
    )
    return AcceptResponse(
        status="duplicate" if accepted.duplicate else "accepted",
        message_id=accepted.message_id,
        task_id=accepted.task_id,
    )


@router.post(
    "/v1/documents",
    response_model=DocumentAccepted,
    summary="Upload a document directly",
)
async def upload_document(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, FileParam()],
) -> DocumentAccepted:
    """Accept a file, deduplicating on content synchronously.

    Returns 200 with the existing document id when these exact bytes are
    already known, and 202 only when the document is genuinely new. The
    hash is computed anyway in order to write the object, so the lookup
    costs one indexed read, and 202 would be a false claim for a duplicate.
    """
    deps = _deps(request)
    data = await file.read()
    if len(data) > deps.settings.max_upload_bytes:
        raise PayloadTooLargeError(
            "File exceeds the size limit",
            detail=f"{len(data)} bytes, limit {deps.settings.max_upload_bytes}",
        )

    received_at = datetime.now(tz=UTC)
    outcome = await ingest_bytes(
        deps,
        data=data,
        declared_media_type=file.content_type,
        source=SOURCE_UPLOAD,
        source_ref=file.filename,
        received_at=received_at,
        attempt_key="1",
    )

    async with transaction(deps.engine) as conn:
        current = await repository.get_document_status(conn, outcome.document_id)

    duplicate = not outcome.document_created
    response.status_code = 200 if duplicate else 202
    response.headers["Location"] = f"/v1/documents/{outcome.document_id}"

    return DocumentAccepted(
        document_id=outcome.document_id,
        status=(current or {}).get("status", repository.STATUS_ACCEPTED),
        received_at=received_at,
        content_hash=outcome.content_hash,
        duplicate=duplicate,
    )


@router.get(
    "/v1/documents/{document_id}",
    response_model=DocumentStatus,
    summary="Document status and extraction",
)
async def get_document(request: Request, document_id: str) -> DocumentStatus:
    """Status is derived from the latest job, never stored on the document."""
    from src.common.errors import NotFoundError
    from src.extraction import repository as extraction_repo

    deps = _deps(request)
    async with transaction(deps.engine) as conn:
        current = await repository.get_document_status(conn, document_id)
        if current is None:
            raise NotFoundError("Unknown document", detail=document_id)
        extraction = None
        if current.get("extraction_id"):
            extraction = await extraction_repo.get_extraction(
                conn, str(current["extraction_id"])
            )

    return DocumentStatus(**current, extraction=extraction)


@router.post(
    "/internal/gmail/watch/renew",
    summary="Re-arm users.watch",
)
async def renew_watch(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Scheduled. users.watch lapses after seven days without a word."""
    deps = _deps(request)
    await deps.verifier.verify(authorization)
    return await renew_gmail_watch(deps)
