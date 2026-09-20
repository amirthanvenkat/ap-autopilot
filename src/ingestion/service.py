"""Turning bytes into a document and a running extraction job.

Shared by the Gmail worker and the manual upload endpoint, so both paths
produce identical rows and identical deduplication behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from src.common import media
from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.ids import content_hash as hash_bytes
from src.common.ids import derived_id
from src.common.logging import get_logger
from src.common.outbox import HANDLER_EXTRACTION_COMPLETE, accept_message
from src.extraction.docai import BatchHandle
from src.ingestion import repository

log = get_logger(__name__)

SOURCE_GMAIL = "GMAIL"
SOURCE_UPLOAD = "UPLOAD"


@dataclass(frozen=True)
class IngestOutcome:
    """What happened to one set of bytes."""

    document_id: str
    content_hash: str
    job_id: str | None
    document_created: bool
    job_started: bool
    reason: str


def inbox_object_name(content_hash_value: str, media_type: str) -> str:
    """Object path for an inbound document.

    Accepted change 5: keyed on the content hash, not the document id. A
    redelivery writes identical bytes to an identical path, so the
    create-if-absent precondition makes the second write a no-op instead of
    leaving an orphaned object behind.
    """
    return f"inbox/{content_hash_value}{media.extension_for(media_type)}"


def output_prefix_for(job_id: str) -> str:
    """Batch output prefix. One prefix per job, as the spec specifies."""
    return f"extractions/{job_id}/"


def job_id_for(content_hash_value: str, attempt_key: str) -> str:
    """Derive a job id so a worker retry reuses the same job row.

    Reusing the id means a retry that already started a batch writes to the
    same output prefix, so a duplicated start costs one wasted Document AI
    call rather than a duplicate result row.
    """
    return derived_id("job", content_hash_value, attempt_key)


async def ingest_bytes(
    deps: Dependencies,
    *,
    data: bytes,
    declared_media_type: str | None,
    source: str,
    source_ref: str | None,
    received_at: datetime,
    attempt_key: str,
) -> IngestOutcome:
    """Store bytes, record the document and start an extraction.

    Ordering matters. The object write and the job row both happen before
    the Document AI call, so a crash between them leaves a RUNNING job whose
    output prefix is already known, which the retry can find rather than
    paying for a second batch.
    """
    media_type = media.require_accepted(declared_media_type, data)
    digest = hash_bytes(data)

    stored = await deps.object_store.put_if_absent(
        inbox_object_name(digest, media_type), data, content_type=media_type
    )

    job_id = job_id_for(digest, attempt_key)
    output_prefix = output_prefix_for(job_id)

    async with transaction(deps.engine) as conn:
        document = await repository.upsert_document(
            conn,
            source=source,
            source_ref=source_ref,
            gcs_uri=stored.uri,
            content_hash=digest,
            media_type=media_type,
            byte_size=len(data),
            received_at=received_at,
        )
        if await repository.has_successful_extraction(conn, document.document_id):
            # The same bytes have already been extracted. Charging Document
            # AI again for a known answer is the cost trap this guards.
            log.info(
                "ingest.already_extracted",
                document_id=document.document_id,
                content_hash=digest,
            )
            return IngestOutcome(
                document_id=document.document_id,
                content_hash=digest,
                job_id=None,
                document_created=document.created,
                job_started=False,
                reason="already_extracted",
            )
        job = await repository.create_job(
            conn,
            job_id=job_id,
            document_id=document.document_id,
            processor=deps.settings.docai_processor_id or "fixtures",
        )

    if not job.created and job.operation_name:
        log.info("ingest.job_already_running", job_id=job.job_id)
        return IngestOutcome(
            document_id=document.document_id,
            content_hash=digest,
            job_id=job.job_id,
            document_created=document.created,
            job_started=False,
            reason="job_already_running",
        )

    handle = await _start_batch(
        deps,
        gcs_input_uri=stored.uri,
        media_type=media_type,
        output_prefix=output_prefix,
        content_hash=digest,
        job_id=job.job_id,
    )

    async with transaction(deps.engine) as conn:
        if handle.operation_name:
            await repository.set_operation_name(
                conn, job_id=job.job_id, operation_name=handle.operation_name
            )
        if handle.immediate:
            # Fixtures mode has no Eventarc, so the completion event is
            # queued directly. The completion worker is the same code that
            # a real finalize event drives.
            await _queue_completion(conn, job_id=job.job_id)

    return IngestOutcome(
        document_id=document.document_id,
        content_hash=digest,
        job_id=job.job_id,
        document_created=document.created,
        job_started=True,
        reason="started",
    )


async def _start_batch(
    deps: Dependencies,
    *,
    gcs_input_uri: str,
    media_type: str,
    output_prefix: str,
    content_hash: str,
    job_id: str,
) -> BatchHandle:
    """Start the batch, reusing an operation already writing to this prefix.

    Document AI has no request level idempotency key, so this lookup is the
    only guard against a crash between the call and recording its name.
    """
    existing = await deps.documentai.find_operation_for_prefix(output_prefix)
    if existing:
        return BatchHandle(
            operation_name=existing, output_prefix=output_prefix, immediate=False
        )
    handle = await deps.documentai.start_batch(
        gcs_input_uri=gcs_input_uri,
        mime_type=media_type,
        output_prefix=output_prefix,
        content_hash=content_hash,
    )
    log.info(
        "ingest.batch_started",
        job_id=job_id,
        operation=handle.operation_name,
        output_prefix=output_prefix,
    )
    return handle


async def _queue_completion(conn: AsyncConnection, *, job_id: str) -> None:
    """Enqueue a completion task without going through Pub/Sub."""
    message_id = derived_id("synthetic-complete", job_id)
    await accept_message(
        conn,
        handler=HANDLER_EXTRACTION_COMPLETE,
        message_id=message_id,
        payload={
            "data": {"job_id": job_id, "synthetic": True},
            "attributes": {},
            "raw": None,
        },
    )


def parse_object_name(name: str) -> str | None:
    """Recover a job id from a batch output object name.

    Output objects live under extractions/{job_id}/, so the job id is the
    second path segment. Anything else is an object this handler does not
    own.
    """
    parts = name.split("/")
    if len(parts) < 3 or parts[0] != "extractions":
        return None
    return parts[1] or None


def decode_pubsub_payload(body: dict[str, object]) -> dict[str, object]:
    """Pull the JSON out of a Pub/Sub push envelope.

    Returns an empty mapping when the data is absent or unreadable. A
    malformed body is not an error here: it is accepted, stored and failed
    in the worker, where the failure is visible rather than discarded.
    """
    import base64

    message = body.get("message")
    if not isinstance(message, dict):
        return {}
    raw = message.get("data")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        decoded = base64.b64decode(raw)
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
