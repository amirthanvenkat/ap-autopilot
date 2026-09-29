"""Migration 0002: the constraints spec 02 relies on.

The matcher leans on these rather than on application checks, so each one
is exercised directly against Postgres.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.common.tables import metadata
from tests.conftest import requires_database

pytestmark = requires_database


async def _extraction(conn: AsyncConnection, suffix: str) -> str:
    """The minimum spec 01 chain an invoice row needs for its foreign key."""
    ids = {"d": f"doc-{suffix}", "j": f"job-{suffix}", "e": f"ext-{suffix}"}
    for statement in (
        """
        insert into documents (document_id, source, gcs_uri, content_hash,
                               media_type, byte_size, received_at)
        values (:d, 'UPLOAD', 'gs://b/' || :d, :d, 'application/pdf', 1, now())
        """,
        """
        insert into extraction_jobs (job_id, document_id, processor, status)
        values (:j, :d, 'fixture', 'SUCCEEDED')
        """,
        """
        insert into extraction_results (extraction_id, job_id, document_id,
                                        raw_payload, raw_gcs_uri)
        values (:e, :j, :d, '{}'::jsonb, 'gs://b/out')
        """,
    ):
        params = {k: v for k, v in ids.items() if f":{k}" in statement}
        await conn.execute(text(statement), params)
    return f"ext-{suffix}"


async def _supplier(conn: AsyncConnection, supplier_id: str = "sup-1") -> None:
    await conn.execute(
        text(
            "insert into suppliers (supplier_id, legal_name, currency) "
            "values (:s, 'Acme Office Supplies Pte Ltd', 'SGD')"
        ),
        {"s": supplier_id},
    )


async def _invoice(
    conn: AsyncConnection,
    invoice_id: str,
    *,
    supplier_id: str | None = "sup-1",
    invoice_number: str | None = "INV-1001",
    duplicate_of: str | None = None,
) -> None:
    extraction_id = await _extraction(conn, invoice_id)
    await conn.execute(
        text(
            """
            insert into invoices (invoice_id, extraction_id, supplier_id,
                                  supplier_resolution_method, invoice_number,
                                  duplicate_of_invoice_id)
            values (:i, :e, :s,
                    case when cast(:s as text) is null then null else 'TAX_ID' end,
                    :n, :dup)
            """
        ),
        {
            "i": invoice_id,
            "e": extraction_id,
            "s": supplier_id,
            "n": invoice_number,
            "dup": duplicate_of,
        },
    )


async def test_core_mirror_matches_the_migrated_schema(engine: AsyncEngine) -> None:
    """tables.py and the migration must describe the same columns."""

    def _columns(sync_conn: object) -> dict[str, set[str]]:
        inspector = inspect(sync_conn)
        return {
            name: {c["name"] for c in inspector.get_columns(name)}
            for name in inspector.get_table_names()
            if name != "alembic_version"
        }

    async with engine.connect() as conn:
        live = await conn.run_sync(_columns)
    mirrored = {t.name: {c.name for c in t.columns} for t in metadata.tables.values()}
    assert mirrored == live


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Office Supplies Pte. Ltd.", "acme office supplies"),
        ("ACME OFFICE SUPPLIES PTE LTD", "acme office supplies"),
        ("Northgate Consulting LLP", "northgate consulting"),
        ("Dupont S.A.S.", "dupont"),
        ("Harbour-Point I.T. Co., Ltd.", "harbour point it"),
        # A name that is only a suffix keeps it rather than becoming empty.
        ("Ltd", "ltd"),
        ("   ", None),
    ],
)
async def test_supplier_name_normalisation(
    engine: AsyncEngine, raw: str, expected: str | None
) -> None:
    async with engine.connect() as conn:
        result = await conn.scalar(
            text("select normalise_supplier_name(:n)"), {"n": raw}
        )
    assert result == expected


async def test_same_supplier_and_number_is_rejected(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await _supplier(conn)
        await _invoice(conn, "inv-1")
    with pytest.raises(IntegrityError, match="invoices_supplier_number_uidx"):
        async with engine.begin() as conn:
            await _invoice(conn, "inv-2")


async def test_recorded_duplicate_can_be_stored(engine: AsyncEngine) -> None:
    """DUPLICATE_EXACT needs a row to attach to."""
    async with engine.begin() as conn:
        await _supplier(conn)
        await _invoice(conn, "inv-1")
        await _invoice(conn, "inv-2", duplicate_of="inv-1")
        stored = await conn.scalar(text("select count(*) from invoices"))
    assert stored == 2


async def test_invoice_without_number_or_supplier_is_stored(
    engine: AsyncEngine,
) -> None:
    """LOW_CONFIDENCE and SUPPLIER_UNRESOLVED both need the invoice to exist."""
    async with engine.begin() as conn:
        await _invoice(conn, "inv-1", supplier_id=None, invoice_number=None)
        await _invoice(conn, "inv-2", supplier_id=None, invoice_number=None)
        stored = await conn.scalar(text("select count(*) from invoices"))
    assert stored == 2


async def test_supplier_without_resolution_method_is_rejected(
    engine: AsyncEngine,
) -> None:
    """A reviewer must always be able to see why a supplier was chosen."""
    async with engine.begin() as conn:
        await _supplier(conn)
        extraction_id = await _extraction(conn, "inv-1")
    with pytest.raises(IntegrityError, match="invoices_resolution_complete"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into invoices (invoice_id, extraction_id, supplier_id) "
                    "values ('inv-1', :e, 'sup-1')"
                ),
                {"e": extraction_id},
            )


_EXCEPTION_UPSERT = text(
    """
    insert into exceptions (invoice_id, exception_key, exception_type,
                            severity, detail, variance_amount)
    values ('inv-1', 'field:/invoice_date:LOW_CONFIDENCE', 'LOW_CONFIDENCE',
            'BLOCK', 'below threshold', :v)
    on conflict (invoice_id, exception_key)
    do update set variance_amount = excluded.variance_amount,
                  updated_at = now()
    returning exception_id
    """
)


async def test_exception_upsert_keeps_its_id(engine: AsyncEngine) -> None:
    """A re-run must not change exception_id, or spec 03 references break."""
    async with engine.begin() as conn:
        await _invoice(conn, "inv-1", supplier_id=None)
        first = await conn.scalar(_EXCEPTION_UPSERT, {"v": 1})
        second = await conn.scalar(_EXCEPTION_UPSERT, {"v": 2})
        rows = await conn.scalar(text("select count(*) from exceptions"))
    assert first == second
    assert rows == 1


@pytest.mark.parametrize(
    ("status", "resolved_by", "resolved_at"),
    [
        ("RESOLVED", None, None),
        ("WAIVED", "REVIEWER", None),
        ("OPEN", "MATCHER", "now()"),
    ],
)
async def test_exception_resolution_must_be_consistent(
    engine: AsyncEngine,
    status: str,
    resolved_by: str | None,
    resolved_at: str | None,
) -> None:
    async with engine.begin() as conn:
        await _invoice(conn, "inv-1", supplier_id=None)
    with pytest.raises(IntegrityError, match="exceptions_resolution_consistent"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    insert into exceptions (invoice_id, exception_key,
                        exception_type, severity, detail, status,
                        resolved_by, resolved_at)
                    values ('inv-1', 'k', 'LOW_CONFIDENCE', 'BLOCK', 'd',
                            :status, :by, {resolved_at or "null"})
                    """
                ),
                {"status": status, "by": resolved_by},
            )


async def test_unknown_exception_type_is_rejected(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await _invoice(conn, "inv-1", supplier_id=None)
    with pytest.raises(IntegrityError, match="exception_type_check"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into exceptions (invoice_id, exception_key, "
                    "exception_type, severity, detail) "
                    "values ('inv-1', 'k', 'PRICE_VARIANCES', 'BLOCK', 'd')"
                )
            )
