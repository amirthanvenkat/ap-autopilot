"""Document and job persistence.

Explicit SQL, per CLAUDE.md section 2. Every write that a worker might
attempt twice is guarded by a natural key, so a replay conflicts rather than
duplicating.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.common.ids import new_ulid
from src.common.logging import get_logger

log = get_logger(__name__)

STATUS_ACCEPTED = "ACCEPTED"
STATUS_EXTRACTING = "EXTRACTING"
STATUS_EXTRACTED = "EXTRACTED"
STATUS_FAILED = "FAILED"


@dataclass(frozen=True)
class DocumentRecord:
    """A stored document."""

    document_id: str
    content_hash: str
    gcs_uri: str
    media_type: str
    byte_size: int
    created: bool


@dataclass(frozen=True)
class JobRecord:
    """An extraction job."""

    job_id: str
    document_id: str
    status: str
    attempt: int
    operation_name: str | None
    created: bool


_INSERT_DOCUMENT_SQL = text(
    """
    insert into documents (
        document_id, source, source_ref, gcs_uri, content_hash,
        media_type, byte_size, received_at
    )
    values (
        :document_id, :source, :source_ref, :gcs_uri, :content_hash,
        :media_type, :byte_size, :received_at
    )
    on conflict (content_hash) do nothing
    returning document_id
    """
)

_SELECT_DOCUMENT_BY_HASH_SQL = text(
    """
    select document_id, content_hash, gcs_uri, media_type, byte_size
      from documents
     where content_hash = :content_hash
    """
)

_INSERT_DOCUMENT_SOURCE_SQL = text(
    """
    insert into document_sources (document_id, source, source_ref, received_at)
    values (:document_id, :source, :source_ref, :received_at)
    on conflict on constraint document_sources_uidx do nothing
    """
)


async def upsert_document(
    conn: AsyncConnection,
    *,
    source: str,
    source_ref: str | None,
    gcs_uri: str,
    content_hash: str,
    media_type: str,
    byte_size: int,
    received_at: datetime,
) -> DocumentRecord:
    """Insert a document, or return the existing one with the same bytes.

    Content level deduplication. The same invoice arriving as two messages
    is one document.
    """
    document_id = new_ulid()
    inserted = (
        await conn.execute(
            _INSERT_DOCUMENT_SQL,
            {
                "document_id": document_id,
                "source": source,
                "source_ref": source_ref,
                "gcs_uri": gcs_uri,
                "content_hash": content_hash,
                "media_type": media_type,
                "byte_size": byte_size,
                "received_at": received_at,
            },
        )
    ).first()

    if inserted is not None:
        record = DocumentRecord(
            document_id=document_id,
            content_hash=content_hash,
            gcs_uri=gcs_uri,
            media_type=media_type,
            byte_size=byte_size,
            created=True,
        )
    else:
        existing = (
            await conn.execute(
                _SELECT_DOCUMENT_BY_HASH_SQL, {"content_hash": content_hash}
            )
        ).one()
        record = DocumentRecord(
            document_id=existing.document_id,
            content_hash=existing.content_hash,
            gcs_uri=existing.gcs_uri,
            media_type=existing.media_type,
            byte_size=int(existing.byte_size),
            created=False,
        )
        log.info(
            "documents.deduplicated",
            document_id=record.document_id,
            content_hash=content_hash,
        )

    # Record provenance for every route by which the document arrived, so a
    # deduplicated second sender is not lost.
    if source_ref:
        await conn.execute(
            _INSERT_DOCUMENT_SOURCE_SQL,
            {
                "document_id": record.document_id,
                "source": source,
                "source_ref": source_ref,
                "received_at": received_at,
            },
        )
    return record


_LOCK_DOCUMENT_SQL = text("select pg_advisory_xact_lock(hashtext(:document_id))")

_HAS_SUCCESS_SQL = text(
    """
    select 1
      from extraction_jobs
     where document_id = :document_id
       and status = 'SUCCEEDED'
     limit 1
    """
)

_INSERT_JOB_SQL = text(
    """
    insert into extraction_jobs (
        job_id, document_id, processor, status, attempt
    )
    select
        :job_id, :document_id, :processor, 'RUNNING',
        coalesce(
            (select max(attempt) from extraction_jobs
              where document_id = :document_id),
            0
        ) + 1
    on conflict (job_id) do nothing
    returning job_id, document_id, status, attempt, operation_name
    """
)

_SELECT_JOB_SQL = text(
    """
    select job_id, document_id, status, attempt, operation_name
      from extraction_jobs
     where job_id = :job_id
    """
)


async def has_successful_extraction(conn: AsyncConnection, document_id: str) -> bool:
    """Has this document already been extracted successfully?

    Checked before starting a job. Deduplication at the documents table
    stops a second row, but without this check the same bytes would still
    buy a second Document AI call under a new attempt number.
    """
    row = (await conn.execute(_HAS_SUCCESS_SQL, {"document_id": document_id})).first()
    return row is not None


async def create_job(
    conn: AsyncConnection,
    *,
    job_id: str,
    document_id: str,
    processor: str,
) -> JobRecord:
    """Create a RUNNING job, or return the existing one with this id.

    The advisory lock serialises job creation per document, so two messages
    carrying the same invoice cannot both compute the same next attempt
    number and collide on the (document_id, attempt) index.
    """
    await conn.execute(_LOCK_DOCUMENT_SQL, {"document_id": document_id})
    row = (
        await conn.execute(
            _INSERT_JOB_SQL,
            {
                "job_id": job_id,
                "document_id": document_id,
                "processor": processor,
            },
        )
    ).first()
    if row is not None:
        return JobRecord(
            job_id=row.job_id,
            document_id=row.document_id,
            status=row.status,
            attempt=int(row.attempt),
            operation_name=row.operation_name,
            created=True,
        )
    existing = (await conn.execute(_SELECT_JOB_SQL, {"job_id": job_id})).one()
    return JobRecord(
        job_id=existing.job_id,
        document_id=existing.document_id,
        status=existing.status,
        attempt=int(existing.attempt),
        operation_name=existing.operation_name,
        created=False,
    )


_SET_OPERATION_SQL = text(
    """
    update extraction_jobs
       set operation_name = :operation_name
     where job_id = :job_id
    """
)

_FAIL_JOB_SQL = text(
    """
    update extraction_jobs
       set status = 'FAILED',
           error_code = :error_code,
           error_detail = :error_detail,
           finished_at = now()
     where job_id = :job_id
       and status = 'RUNNING'
    """
)

_SUCCEED_JOB_SQL = text(
    """
    update extraction_jobs
       set status = 'SUCCEEDED', finished_at = now()
     where job_id = :job_id
       and status = 'RUNNING'
    """
)


async def set_operation_name(
    conn: AsyncConnection, *, job_id: str, operation_name: str
) -> None:
    await conn.execute(
        _SET_OPERATION_SQL,
        {"job_id": job_id, "operation_name": operation_name},
    )


async def fail_job(
    conn: AsyncConnection,
    *,
    job_id: str,
    error_code: str,
    error_detail: str,
) -> None:
    await conn.execute(
        _FAIL_JOB_SQL,
        {
            "job_id": job_id,
            "error_code": error_code,
            "error_detail": error_detail[:4000],
        },
    )


async def succeed_job(conn: AsyncConnection, *, job_id: str) -> None:
    await conn.execute(_SUCCEED_JOB_SQL, {"job_id": job_id})


_LOCK_JOB_SQL = text(
    """
    select job_id, document_id, status, attempt, operation_name
      from extraction_jobs
     where job_id = :job_id
       for update
    """
)


async def lock_job(conn: AsyncConnection, job_id: str) -> JobRecord | None:
    """Take a row lock on a job.

    Serialises the several Cloud Storage finalize events that one batch
    produces, so only one of them assembles the result.
    """
    row = (await conn.execute(_LOCK_JOB_SQL, {"job_id": job_id})).first()
    if row is None:
        return None
    return JobRecord(
        job_id=row.job_id,
        document_id=row.document_id,
        status=row.status,
        attempt=int(row.attempt),
        operation_name=row.operation_name,
        created=False,
    )


_STALE_JOBS_SQL = text(
    """
    select job_id, document_id, status, attempt, operation_name
      from extraction_jobs
     where status = 'RUNNING'
       and started_at < now() - make_interval(secs => :timeout_seconds)
     order by started_at
     limit :limit
    """
)


async def find_stale_jobs(
    conn: AsyncConnection, *, timeout_seconds: int, limit: int = 50
) -> list[JobRecord]:
    """Jobs still RUNNING past their timeout.

    A failed Document AI operation writes no output, so no finalize event
    ever fires and nothing else would notice these.
    """
    rows = (
        await conn.execute(
            _STALE_JOBS_SQL, {"timeout_seconds": timeout_seconds, "limit": limit}
        )
    ).all()
    return [
        JobRecord(
            job_id=row.job_id,
            document_id=row.document_id,
            status=row.status,
            attempt=int(row.attempt),
            operation_name=row.operation_name,
            created=False,
        )
        for row in rows
    ]


_DOCUMENT_STATUS_SQL = text(
    """
    select d.document_id,
           d.content_hash,
           d.media_type,
           d.byte_size,
           d.received_at,
           j.job_id,
           j.status as job_status,
           j.error_code,
           j.error_detail,
           r.extraction_id
      from documents d
      left join lateral (
            select * from extraction_jobs ej
             where ej.document_id = d.document_id
             order by ej.attempt desc
             limit 1
      ) j on true
      left join extraction_results r on r.job_id = j.job_id
     where d.document_id = :document_id
    """
)


async def get_document_status(
    conn: AsyncConnection, document_id: str
) -> dict[str, Any] | None:
    """Current status of a document, derived rather than stored.

    There is no status column. Deriving it from the latest job keeps one
    source of truth, since a stored column would need updating in every
    path that changes a job and would eventually disagree.
    """
    row = (
        await conn.execute(_DOCUMENT_STATUS_SQL, {"document_id": document_id})
    ).first()
    if row is None:
        return None

    if row.job_status is None:
        status = STATUS_ACCEPTED
    elif row.job_status == "FAILED":
        status = STATUS_FAILED
    elif row.job_status == "SUCCEEDED" and row.extraction_id:
        status = STATUS_EXTRACTED
    else:
        status = STATUS_EXTRACTING

    return {
        "document_id": row.document_id,
        "status": status,
        "content_hash": row.content_hash,
        "media_type": row.media_type,
        "byte_size": int(row.byte_size),
        "received_at": row.received_at,
        "job_id": row.job_id,
        "extraction_id": row.extraction_id,
        "error_code": row.error_code,
        "error_detail": row.error_detail,
    }
