"""Canonicalisation and fact-derived exceptions: README 5.2 and 5.6.

Extractions are written directly so each test controls exactly what the
model "said". One test goes through the real ingestion path in fixtures
mode. Names, tax IDs and numbers are synthetic.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.outbox import HANDLER_MATCH_INVOICE, OutboxTask
from src.common.runner import RESULT_DONE
from src.ingestion.service import SOURCE_UPLOAD, ingest_bytes
from src.matching.canonical import invoice_id_for
from src.matching.rules import MatchingRules
from src.matching.worker import (
    OUTCOME_SKIPPED_FINAL,
    MatchResult,
    process_match_invoice,
    queue_match,
)
from tests.conftest import requires_database
from tests.integration.conftest import count, drain, fetch_all, fetch_one

pytestmark = requires_database

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

HIGH = Decimal("0.9900")

# A clean header: every field present, confident, and matching the seed.
CLEAN: dict[str, tuple[str | None, Decimal | None]] = {
    "supplier_name": ("Acme Office Supplies Pte Ltd", HIGH),
    "supplier_tax_id": ("200812345K", HIGH),
    "invoice_number": ("INV-1001", HIGH),
    "invoice_date": ("2026-08-03", HIGH),
    "currency": ("SGD", HIGH),
    "net_amount": ("1200.0000", HIGH),
    "tax_amount": ("108.0000", HIGH),
    "total_amount": ("1308.0000", HIGH),
    "po_number": ("PO-2026-0101", HIGH),
}

LINES: list[dict[str, str]] = [
    {
        "description": "A4 copier paper, box of 5 reams",
        "sku": "AOS-PAP-A4-5R",
        "quantity": "12.000000",
        "unit_price": "42.0000",
        "line_total": "504.0000",
    },
    {
        # A service line: no SKU reported, and a six place quantity.
        "description": "Delivery",
        "quantity": "2.123456",
        "unit_price": "10.0000",
        "line_total": "21.2300",
    },
]

_COLUMNS = (
    "supplier_name",
    "supplier_tax_id",
    "invoice_number",
    "po_number",
    "invoice_date",
    "currency",
    "net_amount",
    "tax_amount",
    "total_amount",
)


@pytest.fixture
async def seeded(engine: AsyncEngine) -> AsyncEngine:
    """Two suppliers and their purchase orders."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                insert into suppliers (supplier_id, legal_name, tax_id, currency)
                values
                    ('sup-acme', 'Acme Office Supplies Pte Ltd', '200812345K', 'SGD'),
                    ('sup-harbour', 'Harbourpoint IT Services', '201234567W', 'SGD')
                """
            )
        )
        await conn.execute(
            text(
                """
                insert into purchase_orders
                    (po_id, po_number, supplier_id, currency, po_date, status)
                select po_id, po_number, supplier_id, 'SGD', date '2026-07-01', status
                  from (values
                    ('po-open', 'PO-2026-0101', 'sup-acme', 'OPEN'),
                    ('po-closed', 'PO-2026-0102', 'sup-acme', 'CLOSED'),
                    ('po-cancelled', 'PO-2026-0103', 'sup-acme', 'CANCELLED'),
                    ('po-harbour', 'PO-2026-0104', 'sup-harbour', 'OPEN')
                  ) as v (po_id, po_number, supplier_id, status)
                """
            )
        )
    return engine


async def extraction(
    engine: AsyncEngine,
    key: str,
    *,
    header: dict[str, tuple[str | None, Decimal | None]] | None = None,
    lines: list[dict[str, str]] | None = None,
) -> str:
    """Write a stored extraction as spec 01 would, and return its id.

    A header field left out of the mapping is never reported: it gets no
    extraction_fields row, which is how spec 01 records absence.
    """
    fields = CLEAN if header is None else header
    rows = LINES if lines is None else lines
    extraction_id = f"ext-{key}"
    values = {column: (fields.get(column) or (None, None))[0] for column in _COLUMNS}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "insert into documents (document_id, source, gcs_uri, content_hash, "
                "media_type, byte_size, received_at) values (:d, 'UPLOAD', 'gs://b/x', "
                ":d, 'application/pdf', 1, now())"
            ),
            {"d": f"doc-{key}"},
        )
        await conn.execute(
            text(
                "insert into extraction_jobs (job_id, document_id, processor, status) "
                "values (:j, :d, 'fixture', 'SUCCEEDED')"
            ),
            {"j": f"job-{key}", "d": f"doc-{key}"},
        )
        await conn.execute(
            text(
                """
                insert into extraction_results (
                    extraction_id, job_id, document_id, supplier_name,
                    supplier_tax_id, invoice_number, po_number, invoice_date,
                    currency, net_amount, tax_amount, total_amount,
                    raw_payload, raw_gcs_uri)
                values (:e, :j, :d, :supplier_name, :supplier_tax_id,
                        :invoice_number, :po_number, cast(:invoice_date as date),
                        :currency, cast(:net_amount as numeric),
                        cast(:tax_amount as numeric), cast(:total_amount as numeric),
                        '{}'::jsonb, 'gs://b/out')
                """
            ),
            {"e": extraction_id, "j": f"job-{key}", "d": f"doc-{key}", **values},
        )
        field_rows: list[dict[str, Any]] = [
            {"path": f"/{name}", "value": value, "confidence": confidence}
            for name, (value, confidence) in fields.items()
        ]
        for index, line in enumerate(rows):
            field_rows += [
                {
                    "path": f"/line_items/{index}/{name}",
                    "value": value,
                    "confidence": Decimal("0.95"),
                }
                for name, value in line.items()
            ]
        for row in field_rows:
            await conn.execute(
                text(
                    "insert into extraction_fields (extraction_id, field_path, "
                    "field_value, confidence) values (:e, :path, :value, :confidence)"
                ),
                {"e": extraction_id, **row},
            )
    return extraction_id


def _task(extraction_id: str) -> OutboxTask:
    return OutboxTask(
        task_id=f"task-{extraction_id}",
        handler=HANDLER_MATCH_INVOICE,
        message_id=f"msg-{extraction_id}",
        payload={"data": {"extraction_id": extraction_id}},
        attempts=1,
    )


async def run(deps: Dependencies, extraction_id: str) -> MatchResult:
    return await process_match_invoice(deps, _task(extraction_id))


async def exceptions_of(engine: AsyncEngine, invoice_id: str) -> dict[str, Any]:
    rows = await fetch_all(
        engine,
        "select exception_key, exception_id, exception_type, severity, status, "
        "resolved_by, detail from exceptions where invoice_id = :i",
        i=invoice_id,
    )
    return {row.exception_key: row for row in rows}


def _with(**changes: tuple[str | None, Decimal | None] | None) -> dict[str, Any]:
    header: dict[str, Any] = dict(CLEAN)
    for name, value in changes.items():
        if value is None:
            header.pop(name, None)
        else:
            header[name] = value
    return header


# ------------------------------------------------------------ canonical rows


async def test_an_extraction_becomes_an_invoice_and_its_lines(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    extraction_id = await extraction(seeded, "a")
    result = await run(deps, extraction_id)

    assert result.created
    assert result.invoice_id == invoice_id_for(extraction_id)
    invoice = await fetch_one(
        seeded, "select * from invoices where invoice_id = :i", i=result.invoice_id
    )
    assert invoice.supplier_id == "sup-acme"
    assert invoice.supplier_resolution_method == "TAX_ID"
    assert invoice.po_id == "po-open"
    assert invoice.total_amount == Decimal("1308.0000")

    lines = await fetch_all(
        seeded,
        "select * from invoice_lines where invoice_id = :i order by line_number",
        i=result.invoice_id,
    )
    assert [line.line_number for line in lines] == [1, 2]
    assert lines[0].sku == "AOS-PAP-A4-5R"
    assert lines[0].unit_price == Decimal("42.0000")
    # Never reported, so null rather than guessed.
    assert lines[1].sku is None
    # Six places rounded to the column's four.
    assert lines[1].quantity == Decimal("2.1235")


async def test_clean_facts_raise_nothing_and_leave_the_invoice_pending(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """Pending, not matched: the line match has not run yet."""
    result = await run(deps, await extraction(seeded, "a"))
    assert result.match_status == "PENDING"
    assert await count(seeded, "exceptions") == 0


# ---------------------------------------------------------------- idempotency


async def test_the_same_message_three_times_gives_one_of_everything(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """CLAUDE.md section 7, mandatory test 1, for this handler."""
    extraction_id = await extraction(seeded, "a", header=_with(supplier_name=None))
    for _ in range(3):
        async with transaction(seeded) as conn:
            await queue_match(conn, extraction_id=extraction_id)
    results = await drain(deps)

    assert [result.result for result in results] == [RESULT_DONE]
    assert await count(seeded, "invoices") == 1
    assert await count(seeded, "invoice_lines") == 2
    assert await count(seeded, "exceptions") == 1


async def test_rerunning_changes_nothing(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    extraction_id = await extraction(seeded, "a", header=_with(supplier_name=None))
    first = await run(deps, extraction_id)
    before = await exceptions_of(seeded, first.invoice_id)
    second = await run(deps, extraction_id)

    assert not second.created
    after = await exceptions_of(seeded, first.invoice_id)
    assert after.keys() == before.keys()
    assert {k: r.exception_id for k, r in after.items()} == {
        k: r.exception_id for k, r in before.items()
    }


async def test_two_workers_on_one_extraction_create_one_invoice(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """A lease that expires under a slow worker hands the task to a second."""
    extraction_id = await extraction(seeded, "a")
    results = await asyncio.gather(run(deps, extraction_id), run(deps, extraction_id))

    assert sorted(result.created for result in results) == [False, True]
    assert await count(seeded, "invoices") == 1
    assert await count(seeded, "invoice_lines") == 2


async def test_the_supplier_is_resolved_once(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """A re-match never re-resolves. Changing that is a reviewer action."""
    header = _with(
        supplier_name=("Lumen Marketing Collective", HIGH), supplier_tax_id=None
    )
    extraction_id = await extraction(seeded, "a", header=header)
    first = await run(deps, extraction_id)
    async with seeded.begin() as conn:
        await conn.execute(
            text(
                "insert into suppliers (supplier_id, legal_name, currency) "
                "values ('sup-lumen', 'Lumen Marketing Collective', 'SGD')"
            )
        )
    await run(deps, extraction_id)

    invoice = await fetch_one(
        seeded,
        "select supplier_id from invoices where invoice_id = :i",
        i=first.invoice_id,
    )
    assert invoice.supplier_id is None


async def test_a_final_invoice_is_skipped_not_failed(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    extraction_id = await extraction(seeded, "a")
    first = await run(deps, extraction_id)
    async with seeded.begin() as conn:
        await conn.execute(
            text("update invoices set match_status = 'POSTED' where invoice_id = :i"),
            {"i": first.invoice_id},
        )
    again = await run(deps, extraction_id)
    assert again.outcome == OUTCOME_SKIPPED_FINAL
    assert again.match_status == "POSTED"


# ------------------------------------------------------------------ supplier


async def test_an_unresolved_supplier_blocks_with_its_reason(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    header = _with(supplier_name=("Unknown Trading", HIGH), supplier_tax_id=None)
    result = await run(deps, await extraction(seeded, "a", header=header))

    assert result.match_status == "EXCEPTION"
    raised = await exceptions_of(seeded, result.invoice_id)
    row = raised["invoice:SUPPLIER_UNRESOLVED"]
    assert row.severity == "BLOCK"
    assert row.detail == "no supplier matches the tax ID or the name"


# ------------------------------------------------------------------------ PO


async def test_no_po_number_raises_no_po_reference_only(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """Absent, so not also LOW_CONFIDENCE (README 5.6, corrected)."""
    result = await run(
        deps, await extraction(seeded, "a", header=_with(po_number=None))
    )
    assert set(await exceptions_of(seeded, result.invoice_id)) == {
        "invoice:NO_PO_REFERENCE"
    }


async def test_po_lookup_ignores_case_and_punctuation(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    header = _with(po_number=("po 2026 0101", HIGH))
    result = await run(deps, await extraction(seeded, "a", header=header))
    invoice = await fetch_one(
        seeded, "select po_id from invoices where invoice_id = :i", i=result.invoice_id
    )
    assert invoice.po_id == "po-open"


@pytest.mark.parametrize(
    ("po_number", "expected"),
    [
        ("PO-9999-0000", "no purchase order matches 'PO-9999-0000'"),
        ("PO-2026-0102", "purchase order PO-2026-0102 is closed"),
        ("PO-2026-0103", "purchase order PO-2026-0103 is cancelled"),
        (
            "PO-2026-0104",
            "purchase order PO-2026-0104 belongs to supplier sup-harbour, not sup-acme",
        ),
    ],
)
async def test_a_po_that_cannot_be_billed_raises_po_not_found(
    deps: Dependencies, seeded: AsyncEngine, po_number: str, expected: str
) -> None:
    header = _with(po_number=(po_number, HIGH))
    result = await run(deps, await extraction(seeded, "a", header=header))

    assert result.match_status == "EXCEPTION"
    raised = await exceptions_of(seeded, result.invoice_id)
    assert set(raised) == {"invoice:PO_NOT_FOUND"}
    assert raised["invoice:PO_NOT_FOUND"].detail == expected


# ----------------------------------------------------------------- duplicates


async def test_the_same_supplier_and_number_twice_is_a_recorded_duplicate(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    original = await run(deps, await extraction(seeded, "a"))
    duplicate = await run(deps, await extraction(seeded, "b"))

    row = await fetch_one(
        seeded,
        "select duplicate_of_invoice_id from invoices where invoice_id = :i",
        i=duplicate.invoice_id,
    )
    assert row.duplicate_of_invoice_id == original.invoice_id
    assert duplicate.match_status == "EXCEPTION"
    raised = await exceptions_of(seeded, duplicate.invoice_id)
    assert set(raised) == {f"duplicate:{original.invoice_id}:DUPLICATE_EXACT"}


async def test_concurrent_copies_produce_exactly_one_original(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """The advisory lock serialises them; the unique index never fires."""
    ids = [await extraction(seeded, key) for key in ("a", "b", "c")]
    await asyncio.gather(*(run(deps, extraction_id) for extraction_id in ids))

    assert await count(seeded, "invoices", "duplicate_of_invoice_id is null") == 1
    assert await count(seeded, "exceptions", "exception_type = 'DUPLICATE_EXACT'") == 2


# ----------------------------------------------------------------- confidence


@pytest.mark.parametrize(
    ("changes", "expected_key"),
    [
        # Below the 0.98 threshold for total_amount.
        ({"total_amount": ("1308.0000", Decimal("0.9799"))}, "/total_amount"),
        # A value with no confidence at all.
        ({"invoice_date": ("2026-08-03", None)}, "/invoice_date"),
        # A required field reported with no value.
        ({"invoice_number": (None, None)}, "/invoice_number"),
    ],
)
async def test_low_confidence_is_raised_for_header_fields(
    deps: Dependencies,
    seeded: AsyncEngine,
    changes: dict[str, Any],
    expected_key: str,
) -> None:
    result = await run(deps, await extraction(seeded, "a", header=_with(**changes)))
    raised = await exceptions_of(seeded, result.invoice_id)
    assert f"field:{expected_key}:LOW_CONFIDENCE" in raised
    assert result.match_status == "EXCEPTION"


async def test_confidence_exactly_at_the_threshold_passes(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    header = _with(total_amount=("1308.0000", Decimal("0.9800")))
    result = await run(deps, await extraction(seeded, "a", header=header))
    assert await count(seeded, "exceptions") == 0
    assert result.match_status == "PENDING"


async def test_an_absent_optional_field_raises_nothing(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """A supplier that is not GST registered shows no tax ID."""
    header = _with(supplier_tax_id=None, due_date=None)
    result = await run(deps, await extraction(seeded, "a", header=header))
    assert await count(seeded, "exceptions") == 0
    assert result.match_status == "PENDING"


async def test_line_item_confidence_is_never_checked(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """Header fields only, decided 2026-09-26."""
    lines = [dict(LINES[0])]
    extraction_id = await extraction(seeded, "a", lines=lines)
    async with seeded.begin() as conn:
        await conn.execute(
            text(
                "update extraction_fields set confidence = 0.10 "
                "where extraction_id = :e and field_path like '/line_items/%'"
            ),
            {"e": extraction_id},
        )
    await run(deps, extraction_id)
    assert await count(seeded, "exceptions") == 0


# ------------------------------------------------------------ rules and upsert


def _with_threshold(rules: MatchingRules, field: str, value: str) -> MatchingRules:
    confidence = rules.confidence.model_copy(
        update={"fields": {**rules.confidence.fields, field: Decimal(value)}}
    )
    return rules.model_copy(update={"confidence": confidence})


async def test_a_rules_change_resolves_and_reopens_the_same_exception(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    """Done 6: no code change. 6.8: the exception keeps its id throughout."""
    header = _with(total_amount=("1308.0000", Decimal("0.9700")))
    extraction_id = await extraction(seeded, "a", header=header)
    key = "field:/total_amount:LOW_CONFIDENCE"

    first = await run(deps, extraction_id)
    raised = (await exceptions_of(seeded, first.invoice_id))[key]
    assert first.match_status == "EXCEPTION"

    lenient = dataclasses.replace(
        deps, rules=_with_threshold(deps.rules, "total_amount", "0.95")
    )
    relaxed = await run(lenient, extraction_id)
    resolved = (await exceptions_of(seeded, first.invoice_id))[key]
    assert relaxed.match_status == "PENDING"
    assert (resolved.status, resolved.resolved_by) == ("RESOLVED", "MATCHER")
    assert resolved.exception_id == raised.exception_id

    strict = await run(deps, extraction_id)
    reopened = (await exceptions_of(seeded, first.invoice_id))[key]
    assert strict.match_status == "EXCEPTION"
    assert (reopened.status, reopened.resolved_by) == ("OPEN", None)
    assert reopened.exception_id == raised.exception_id


async def test_a_waived_exception_stays_waived_and_stops_blocking(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    header = _with(total_amount=("1308.0000", Decimal("0.9700")))
    extraction_id = await extraction(seeded, "a", header=header)
    first = await run(deps, extraction_id)
    async with seeded.begin() as conn:
        await conn.execute(
            text(
                "update exceptions set status = 'WAIVED', resolved_by = 'REVIEWER', "
                "resolved_at = now() where invoice_id = :i"
            ),
            {"i": first.invoice_id},
        )
    again = await run(deps, extraction_id)

    rows = await exceptions_of(seeded, first.invoice_id)
    assert [row.status for row in rows.values()] == ["WAIVED"]
    assert again.match_status == "PENDING"


async def test_severity_comes_from_the_rules(
    deps: Dependencies, seeded: AsyncEngine
) -> None:
    overrides = {**deps.rules.severity_overrides, "LOW_CONFIDENCE": "WARN"}
    warn_only = dataclasses.replace(
        deps, rules=deps.rules.model_copy(update={"severity_overrides": overrides})
    )
    header = _with(total_amount=("1308.0000", Decimal("0.9700")))
    result = await run(warn_only, await extraction(seeded, "a", header=header))

    raised = await exceptions_of(seeded, result.invoice_id)
    assert [row.severity for row in raised.values()] == ["WARN"]
    assert result.match_status == "PENDING"


# ------------------------------------------------------------- end to end


async def test_a_fixture_document_reaches_a_canonical_invoice(
    deps: Dependencies, engine: AsyncEngine
) -> None:
    """Ingestion to invoice through the real queue, with no network."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text("utf-8"))
    record = next(item for item in manifest if item["slug"] == "ironbridge-hardware")
    await ingest_bytes(
        deps,
        data=(FIXTURES / "source" / record["file"]).read_bytes(),
        declared_media_type=record["media_type"],
        source=SOURCE_UPLOAD,
        source_ref=record["file"],
        received_at=datetime.now(tz=UTC),
        attempt_key="1",
    )
    await drain(deps)

    invoice = await fetch_one(engine, "select * from invoices")
    assert invoice.po_number == "PO-2026-0109"
    # Nothing seeded, so the supplier cannot be resolved.
    assert invoice.match_status == "EXCEPTION"
    skus = await fetch_all(engine, "select sku from invoice_lines order by line_number")
    assert [row.sku for row in skus] == ["IBH-HLM-WHT", "IBH-VST-HV-L"]
    assert await count(engine, "ingestion_outbox", "status <> 'DONE'") == 0
