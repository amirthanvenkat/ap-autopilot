"""Extraction completion: shards, replays, schema failures and the reaper."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.common.acceptance import accept_push
from src.common.deps import Dependencies
from src.common.outbox import HANDLER_EXTRACTION_COMPLETE
from src.common.runner import sweep
from src.extraction.docai import OperationStatus
from src.extraction.worker import reap_stale_jobs
from src.ingestion.service import SOURCE_UPLOAD, ingest_bytes, output_prefix_for
from tests.conftest import requires_database
from tests.integration.conftest import count, fetch_one

pytestmark = requires_database

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def finalize_envelope(message_id: str, job_id: str, shard: str) -> bytes:
    """A Cloud Storage finalize event for one batch output object."""
    return json.dumps(
        {
            "message": {
                "messageId": message_id,
                "data": base64.b64encode(
                    json.dumps(
                        {
                            "bucket": "test-bucket",
                            "name": f"{output_prefix_for(job_id)}{shard}",
                        }
                    ).encode()
                ).decode(),
                "attributes": {},
            },
            "subscription": "projects/p/subscriptions/extraction-complete-push",
        }
    ).encode()


async def _ingest(
    deps: Dependencies, slug: str = "acme-office-supplies"
) -> tuple[str, str]:
    """Ingest one fixture and return its document and job ids."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text("utf-8"))
    record = next(item for item in manifest if item["slug"] == slug)
    data = (FIXTURES / "source" / str(record["file"])).read_bytes()
    outcome = await ingest_bytes(
        deps,
        data=data,
        declared_media_type=str(record["media_type"]),
        source=SOURCE_UPLOAD,
        source_ref=str(record["file"]),
        received_at=datetime.now(tz=UTC),
        attempt_key="1",
    )
    assert outcome.job_id
    return outcome.document_id, outcome.job_id


async def _clear_outbox(engine: AsyncEngine) -> None:
    """Drop the synthetic completion queued by fixtures mode."""
    async with engine.begin() as conn:
        await conn.execute(text("delete from ingestion_outbox"))
        await conn.execute(text("delete from processed_events"))


async def test_several_shard_events_produce_one_result(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    """A batch writes several output objects, so several distinct messages.

    Message level deduplication will not collapse them and should not. Only
    one may assemble the result.
    """
    _document_id, job_id = await _ingest(deps, "grandmere-equipment")
    await _clear_outbox(engine)

    for index in range(3):
        await accept_push(
            deps,
            handler=HANDLER_EXTRACTION_COMPLETE,
            authorization=auth_header["Authorization"],
            body=finalize_envelope(
                f"shard-{index}", job_id, f"output-{index}-to-{index}.json"
            ),
        )

    assert await count(engine, "ingestion_outbox") == 3
    await sweep(deps)

    assert await count(engine, "extraction_results") == 1
    assert await count(engine, "extraction_jobs", "status = 'SUCCEEDED'") == 1


async def test_shard_events_arriving_out_of_order_still_produce_one_result(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    _document_id, job_id = await _ingest(deps, "atlas-freight")
    await _clear_outbox(engine)

    for message_id, shard in (
        ("shard-late", "output-2-to-2.json"),
        ("shard-early", "output-0-to-0.json"),
    ):
        await accept_push(
            deps,
            handler=HANDLER_EXTRACTION_COMPLETE,
            authorization=auth_header["Authorization"],
            body=finalize_envelope(message_id, job_id, shard),
        )
    await sweep(deps)
    assert await count(engine, "extraction_results") == 1


async def test_replaying_completion_does_not_double_the_field_rows(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    """The defect the unique constraint exists to make loud.

    A doubled extraction_fields table corrupts every per field confidence
    analytic in Spec 02 and week 4, and the numbers still look plausible.
    """
    _document_id, job_id = await _ingest(deps)
    await sweep(deps)

    first = await count(engine, "extraction_fields")
    assert first > 10

    for index in range(3):
        await accept_push(
            deps,
            handler=HANDLER_EXTRACTION_COMPLETE,
            authorization=auth_header["Authorization"],
            body=finalize_envelope(f"replay-{index}", job_id, "output-0-to-0.json"),
        )
        await sweep(deps)

    assert await count(engine, "extraction_fields") == first
    assert await count(engine, "extraction_results") == 1


async def test_field_paths_are_json_pointers_with_array_indices(
    deps: Dependencies, engine: AsyncEngine
) -> None:
    await _ingest(deps)
    await sweep(deps)
    rows = await fetch_one(
        engine,
        "select count(*) as total from extraction_fields "
        "where field_path like '/line_items/%/unit_price'",
    )
    assert rows.total == 3


async def test_a_malformed_payload_fails_the_job_and_persists_nothing(
    deps: Dependencies, engine: AsyncEngine
) -> None:
    """Mandatory test. The pointer is recorded and no result row appears."""
    _document_id, job_id = await _ingest(deps, "malformed-invoice")
    results = await sweep(deps)
    assert [item.result for item in results] == ["done"]

    job = await fetch_one(
        engine,
        "select status, error_code, error_detail from extraction_jobs "
        "where job_id = :job_id",
        job_id=job_id,
    )
    assert job.status == "FAILED"
    assert job.error_code == "EXTRACTION_SCHEMA_ERROR"
    assert job.error_detail.startswith("/invoice_number/confidence")
    assert await count(engine, "extraction_results") == 0
    assert await count(engine, "extraction_fields") == 0


async def test_a_failed_operation_is_recorded_and_not_retried(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    _document_id, job_id = await _ingest(deps)
    await _clear_outbox(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "update extraction_jobs set operation_name = 'operations/abc' "
                "where job_id = :job_id"
            ),
            {"job_id": job_id},
        )

    async def failed(operation_name: str) -> OperationStatus:
        del operation_name
        return OperationStatus(
            done=True, error_code="3", error_message="invalid page count"
        )

    object.__setattr__(deps.documentai, "operation_status", failed)

    await accept_push(
        deps,
        handler=HANDLER_EXTRACTION_COMPLETE,
        authorization=auth_header["Authorization"],
        body=finalize_envelope("op-failed", job_id, "output-0-to-0.json"),
    )
    await sweep(deps)

    job = await fetch_one(
        engine,
        "select status, error_detail from extraction_jobs where job_id = :job_id",
        job_id=job_id,
    )
    assert job.status == "FAILED"
    assert "invalid page count" in job.error_detail
    assert await count(engine, "extraction_results") == 0


async def test_an_unfinished_operation_waits_for_a_later_event(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    _document_id, job_id = await _ingest(deps)
    await _clear_outbox(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "update extraction_jobs set operation_name = 'operations/abc' "
                "where job_id = :job_id"
            ),
            {"job_id": job_id},
        )

    async def running(operation_name: str) -> OperationStatus:
        del operation_name
        return OperationStatus(done=False)

    object.__setattr__(deps.documentai, "operation_status", running)

    await accept_push(
        deps,
        handler=HANDLER_EXTRACTION_COMPLETE,
        authorization=auth_header["Authorization"],
        body=finalize_envelope("still-running", job_id, "output-0-to-0.json"),
    )
    await sweep(deps)

    job = await fetch_one(
        engine,
        "select status from extraction_jobs where job_id = :job_id",
        job_id=job_id,
    )
    assert job.status == "RUNNING"
    assert await count(engine, "extraction_results") == 0


async def test_the_reaper_resolves_a_job_that_never_got_an_event(
    deps: Dependencies, engine: AsyncEngine
) -> None:
    """A failed operation writes no output, so no finalize event fires.

    Without this sweep the job sits RUNNING forever, the three minute
    target is quietly missed, and nothing anywhere reports it.
    """
    _document_id, job_id = await _ingest(deps)
    await _clear_outbox(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "update extraction_jobs "
                "set started_at = now() - interval '2 hours' "
                "where job_id = :job_id"
            ),
            {"job_id": job_id},
        )

    reaped = await reap_stale_jobs(deps)
    assert len(reaped) == 1

    job = await fetch_one(
        engine,
        "select status, error_code from extraction_jobs where job_id = :job_id",
        job_id=job_id,
    )
    assert job.status == "FAILED"
    assert job.error_code == "NO_OPERATION"


async def test_the_reaper_finishes_a_job_whose_event_was_lost(
    deps: Dependencies, engine: AsyncEngine
) -> None:
    _document_id, job_id = await _ingest(deps)
    await _clear_outbox(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "update extraction_jobs "
                "set started_at = now() - interval '2 hours', "
                "    operation_name = 'operations/abc' "
                "where job_id = :job_id"
            ),
            {"job_id": job_id},
        )

    async def done(operation_name: str) -> OperationStatus:
        del operation_name
        return OperationStatus(done=True)

    object.__setattr__(deps.documentai, "operation_status", done)

    await reap_stale_jobs(deps)

    job = await fetch_one(
        engine,
        "select status from extraction_jobs where job_id = :job_id",
        job_id=job_id,
    )
    assert job.status == "SUCCEEDED"
    assert await count(engine, "extraction_results") == 1


async def test_an_event_for_an_unknown_job_is_ignored(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    await accept_push(
        deps,
        handler=HANDLER_EXTRACTION_COMPLETE,
        authorization=auth_header["Authorization"],
        body=finalize_envelope("orphan", "job-that-never-existed", "out.json"),
    )
    results = await sweep(deps)
    assert [item.result for item in results] == ["done"]
    assert await count(engine, "extraction_results") == 0


async def test_an_object_outside_the_prefix_is_not_ours(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    body = json.dumps(
        {
            "message": {
                "messageId": "inbox-write",
                "data": base64.b64encode(
                    json.dumps(
                        {"bucket": "test-bucket", "name": "inbox/abc123.pdf"}
                    ).encode()
                ).decode(),
            }
        }
    ).encode()
    await accept_push(
        deps,
        handler=HANDLER_EXTRACTION_COMPLETE,
        authorization=auth_header["Authorization"],
        body=body,
    )
    results = await sweep(deps)
    assert [item.result for item in results] == ["done"]
    assert await count(engine, "extraction_jobs") == 0


async def test_raw_payload_is_trimmed_and_the_full_response_is_addressable(
    deps: Dependencies, engine: AsyncEngine
) -> None:
    """Section 11 question 3, decided as trimmed in Postgres.

    The untrimmed response stays one object fetch away, so nothing is lost
    for debugging.
    """
    _document_id, job_id = await _ingest(deps)
    await sweep(deps)

    row = await fetch_one(
        engine,
        "select raw_payload, raw_gcs_uri from extraction_results "
        "where job_id = :job_id",
        job_id=job_id,
    )
    payload = row.raw_payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert "text" not in payload
    assert payload["entities"]
    assert row.raw_gcs_uri.startswith("gs://")
    assert job_id in row.raw_gcs_uri


async def test_money_survives_the_round_trip_as_an_exact_decimal(
    deps: Dependencies, engine: AsyncEngine
) -> None:
    """Rule 5.7, checked against the database rather than in memory."""
    from decimal import Decimal

    _document_id, job_id = await _ingest(deps, "cedarline-print")
    await sweep(deps)

    row = await fetch_one(
        engine,
        "select net_amount, tax_amount, total_amount, currency "
        "from extraction_results where job_id = :job_id",
        job_id=job_id,
    )
    assert isinstance(row.total_amount, Decimal)
    assert row.net_amount == Decimal("742.5000")
    assert row.tax_amount == Decimal("66.8300")
    assert row.total_amount == Decimal("809.3300")
    assert row.net_amount + row.tax_amount == row.total_amount
    assert row.currency == "SGD"


async def test_no_outbound_http_happens_in_fixtures_mode(
    deps: Dependencies,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 5.4, proved rather than asserted.

    Every outbound client is patched to fail. The full path from document to
    persisted extraction must still complete.
    """

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("fixtures mode made an outbound HTTP call")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)

    await _ingest(deps)
    await sweep(deps)

    assert await count(engine, "documents") == 1
    assert await count(engine, "extraction_results") == 1
    assert await count(engine, "extraction_jobs", "status = 'SUCCEEDED'") == 1


async def test_po_number_and_skus_are_persisted(
    deps: Dependencies, engine: AsyncEngine
) -> None:
    """Spec 02 resolves the PO from the column and matches lines on SKU."""
    _document_id, job_id = await _ingest(deps, "ironbridge-hardware")
    await sweep(deps)

    result = await fetch_one(
        engine,
        "select extraction_id, po_number from extraction_results "
        "where job_id = :job_id",
        job_id=job_id,
    )
    assert result.po_number == "PO-2026-0109"
    sku = await fetch_one(
        engine,
        "select field_value from extraction_fields "
        "where extraction_id = :e and field_path = '/line_items/1/sku'",
        e=result.extraction_id,
    )
    assert sku.field_value == "IBH-VST-HV-L"
