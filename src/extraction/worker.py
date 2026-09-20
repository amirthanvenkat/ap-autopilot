"""The extraction completion worker.

A Document AI batch writes its output across several objects for a multi
page document, and Eventarc emits a finalize event for each one. So this
worker receives several genuinely distinct messages for one job, and message
level deduplication neither will nor should collapse them.

The event is therefore treated as a trigger rather than as data. Each one
asks whether the operation has finished. The last to arrive finds it done
and assembles the result; the earlier ones return cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.common.acceptance import task_data
from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.errors import ExtractionSchemaError
from src.common.logging import get_logger
from src.common.outbox import OutboxTask
from src.extraction import repository as extraction_repo
from src.extraction.normalise import normalise_document, trim_raw_payload
from src.extraction.validation import (
    flatten_fields,
    to_result_columns,
    validate_extraction,
)
from src.ingestion import repository as ingestion_repo
from src.ingestion.service import output_prefix_for, parse_object_name

log = get_logger(__name__)

OUTCOME_STORED = "stored"
OUTCOME_ALREADY_DONE = "already_done"
OUTCOME_WAITING = "waiting"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN_JOB = "unknown_job"
OUTCOME_NOT_OURS = "not_ours"


@dataclass(frozen=True)
class CompletionResult:
    """What one completion trigger produced."""

    outcome: str
    job_id: str | None = None
    extraction_id: str | None = None
    detail: str | None = None


def resolve_job_id(payload: dict[str, Any]) -> str | None:
    """Find the job a trigger refers to.

    A Cloud Storage finalize event names the object; a synthetic completion
    names the job directly.
    """
    explicit = payload.get("job_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    name = payload.get("name")
    if isinstance(name, str) and name:
        return parse_object_name(name)
    return None


async def process_extraction_complete(
    deps: Dependencies, task: OutboxTask
) -> CompletionResult:
    """Assemble and persist an extraction, if its operation has finished."""
    job_id = resolve_job_id(task_data(task))
    if job_id is None:
        log.info("extraction.event_ignored", payload_keys=sorted(task_data(task)))
        return CompletionResult(outcome=OUTCOME_NOT_OURS)

    async with transaction(deps.engine) as conn:
        job = await ingestion_repo.lock_job(conn, job_id)

    if job is None:
        log.warning("extraction.unknown_job", job_id=job_id)
        return CompletionResult(outcome=OUTCOME_UNKNOWN_JOB, job_id=job_id)
    if job.status != "RUNNING":
        return CompletionResult(outcome=OUTCOME_ALREADY_DONE, job_id=job_id)

    # Poll outside any lock. A network call inside a row lock would hold it
    # for the duration of the call and serialise every other shard event
    # behind it.
    if job.operation_name:
        status = await deps.documentai.operation_status(job.operation_name)
        if not status.done:
            return CompletionResult(outcome=OUTCOME_WAITING, job_id=job_id)
        if status.failed:
            await _fail(
                deps,
                job_id=job_id,
                error_code=status.error_code or "DOCAI_ERROR",
                error_detail=status.error_message or "operation failed",
            )
            return CompletionResult(
                outcome=OUTCOME_FAILED,
                job_id=job_id,
                detail=status.error_message,
            )

    output_prefix = output_prefix_for(job_id)
    raw = await deps.documentai.fetch_raw(
        output_prefix=output_prefix,
        content_hash=await _content_hash_for(deps, job.document_id),
    )

    try:
        payload = validate_extraction(normalise_document(raw))
    except ExtractionSchemaError as exc:
        # Deterministic. Retrying it five times would burn the delivery
        # budget to reach the same answer, so record and stop.
        await _fail(
            deps,
            job_id=job_id,
            error_code=exc.code,
            error_detail=f"{exc.pointer}: {exc.detail or exc.message}",
        )
        log.warning("extraction.schema_invalid", job_id=job_id, pointer=exc.pointer)
        return CompletionResult(
            outcome=OUTCOME_FAILED, job_id=job_id, detail=exc.pointer
        )

    columns = to_result_columns(payload)
    fields = flatten_fields(payload)
    raw_gcs_uri = deps.object_store.uri(output_prefix)

    # One transaction for the result, every field row and the job status.
    # Splitting them is what would let a retry double the field rows.
    async with transaction(deps.engine) as conn:
        current = await ingestion_repo.lock_job(conn, job_id)
        if current is None or current.status != "RUNNING":
            return CompletionResult(outcome=OUTCOME_ALREADY_DONE, job_id=job_id)
        stored = await extraction_repo.insert_extraction(
            conn,
            job_id=job_id,
            document_id=job.document_id,
            columns=columns,
            fields=fields,
            raw_payload=trim_raw_payload(raw),
            raw_gcs_uri=raw_gcs_uri,
        )
        await ingestion_repo.succeed_job(conn, job_id=job_id)

    log.info(
        "extraction.stored",
        job_id=job_id,
        extraction_id=stored.extraction_id,
        fields=len(fields),
    )
    return CompletionResult(
        outcome=OUTCOME_STORED,
        job_id=job_id,
        extraction_id=stored.extraction_id,
    )


async def _fail(
    deps: Dependencies, *, job_id: str, error_code: str, error_detail: str
) -> None:
    """Record a terminal job failure. Nothing is partially persisted."""
    async with transaction(deps.engine) as conn:
        await ingestion_repo.fail_job(
            conn,
            job_id=job_id,
            error_code=error_code,
            error_detail=error_detail,
        )


async def _content_hash_for(deps: Dependencies, document_id: str) -> str:
    """The document's content hash, which is the fixture cache key."""
    from sqlalchemy import text

    async with transaction(deps.engine) as conn:
        row = (
            await conn.execute(
                text(
                    "select content_hash from documents "
                    "where document_id = :document_id"
                ),
                {"document_id": document_id},
            )
        ).first()
    return str(row.content_hash) if row else ""


async def reap_stale_jobs(deps: Dependencies) -> list[CompletionResult]:
    """Resolve jobs stuck in RUNNING past their timeout.

    A failed Document AI operation writes no output, so no finalize event
    ever fires. Without this sweep those jobs sit RUNNING forever, the three
    minute target is quietly missed, and nothing anywhere reports it.
    """
    async with transaction(deps.engine) as conn:
        stale = await ingestion_repo.find_stale_jobs(
            conn, timeout_seconds=deps.settings.job_timeout_seconds
        )

    results: list[CompletionResult] = []
    for job in stale:
        if not job.operation_name:
            await _fail(
                deps,
                job_id=job.job_id,
                error_code="NO_OPERATION",
                error_detail=(
                    "job timed out without an operation name; the batch was "
                    "never started or its name was never recorded"
                ),
            )
            results.append(CompletionResult(outcome=OUTCOME_FAILED, job_id=job.job_id))
            continue

        status = await deps.documentai.operation_status(job.operation_name)
        if status.done and not status.failed:
            # It finished but the event never arrived. Finish it here.
            results.append(
                await process_extraction_complete(
                    deps,
                    OutboxTask(
                        task_id=f"reaper:{job.job_id}",
                        handler="extraction_complete",
                        message_id=f"reaper:{job.job_id}",
                        payload={"data": {"job_id": job.job_id}},
                        attempts=1,
                    ),
                )
            )
            continue

        await _fail(
            deps,
            job_id=job.job_id,
            error_code=status.error_code or "TIMED_OUT",
            error_detail=status.error_message
            or f"operation still running after {deps.settings.job_timeout_seconds}s",
        )
        results.append(CompletionResult(outcome=OUTCOME_FAILED, job_id=job.job_id))

    if results:
        log.warning("extraction.reaped", count=len(results))
    return results
