"""Canonicalisation: turn a stored extraction into an invoice and its lines.

README 5.2. Runs inside the caller's transaction. Supplier and PO are
resolved once, when the invoice is created, and never again by a re-match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.common.errors import NotFoundError
from src.common.ids import derived_id
from src.common.logging import get_logger
from src.extraction.validation import load_schema
from src.matching.rules import HEADER_FIELDS, ExceptionType, MatchingRules
from src.matching.sql import query
from src.matching.suppliers import resolve_supplier

log = get_logger(__name__)

FINAL_STATUSES = frozenset({"POSTED", "REJECTED"})

_EXISTING_SQL = text(
    "select invoice_id, match_status from invoices where extraction_id = :e"
)

_HEADER_SQL = text(
    """
    select supplier_name, supplier_tax_id, invoice_number, po_number
      from extraction_results
     where extraction_id = :e
    """
)

# Serialises two copies of one invoice, so exactly one becomes the
# original. hashtext is 32 bit: an unrelated pair can share a lock, which
# costs a wait and never a wrong answer.
_DUPLICATE_LOCK_SQL = text(
    "select pg_advisory_xact_lock(hashtext(:supplier_id || '|' || :invoice_number))"
)

_ORIGINAL_SQL = text(
    """
    select invoice_id
      from invoices
     where supplier_id = :supplier_id
       and invoice_number = :invoice_number
       and duplicate_of_invoice_id is null
       and extraction_id <> :e
    """
)


@dataclass(frozen=True)
class CanonicalInvoice:
    """The invoice an extraction became."""

    invoice_id: str
    match_status: str
    created: bool


def invoice_id_for(extraction_id: str) -> str:
    """Derive the invoice id so a replay collides on the primary key."""
    return derived_id("invoice", extraction_id)


def required_header_fields() -> frozenset[str]:
    """Header fields the extraction schema requires to be present.

    The normaliser always emits these, carrying a null when unread, so a
    null here means reported but unreadable.
    """
    required = load_schema().get("required", [])
    return frozenset(field for field in required if field in HEADER_FIELDS)


async def canonicalise(
    conn: AsyncConnection, *, extraction_id: str, rules: MatchingRules
) -> CanonicalInvoice:
    """Create the invoice for an extraction, or return the existing one."""
    existing = (await conn.execute(_EXISTING_SQL, {"e": extraction_id})).first()
    if existing is not None:
        return CanonicalInvoice(existing.invoice_id, existing.match_status, False)

    header = (await conn.execute(_HEADER_SQL, {"e": extraction_id})).first()
    if header is None:
        raise NotFoundError("No extraction to canonicalise", detail=extraction_id)

    supplier = await resolve_supplier(
        conn,
        name=header.supplier_name,
        tax_id=header.supplier_tax_id,
        rules=rules,
    )

    duplicate_of: str | None = None
    if supplier.supplier_id is not None and header.invoice_number is not None:
        keys = {
            "supplier_id": supplier.supplier_id,
            "invoice_number": header.invoice_number,
        }
        await conn.execute(_DUPLICATE_LOCK_SQL, keys)
        duplicate_of = (
            await conn.execute(_ORIGINAL_SQL, {**keys, "e": extraction_id})
        ).scalar_one_or_none()

    po = (
        await conn.execute(
            query("resolve_po"),
            {"po_number": header.po_number, "supplier_id": supplier.supplier_id},
        )
    ).one()

    invoice_id = invoice_id_for(extraction_id)
    inserted = (
        await conn.execute(
            query("insert_invoice"),
            {
                "invoice_id": invoice_id,
                "extraction_id": extraction_id,
                "supplier_id": supplier.supplier_id,
                "supplier_method": supplier.method,
                "supplier_score": supplier.score,
                "supplier_note": supplier.reason,
                "po_id": po.po_id,
                "po_note": po.note,
                "duplicate_of": duplicate_of,
            },
        )
    ).scalar_one_or_none()

    if inserted is None:
        # Another worker created it first and has committed: the insert
        # waited on the extraction's unique index. Use what it wrote.
        existing = (await conn.execute(_EXISTING_SQL, {"e": extraction_id})).one()
        return CanonicalInvoice(existing.invoice_id, existing.match_status, False)

    await conn.execute(
        query("insert_invoice_lines"),
        {"invoice_id": invoice_id, "extraction_id": extraction_id},
    )
    log.info(
        "canonical.created",
        invoice_id=invoice_id,
        extraction_id=extraction_id,
        supplier_id=supplier.supplier_id,
        supplier_method=supplier.method,
        po_id=po.po_id,
        duplicate_of=duplicate_of,
    )
    return CanonicalInvoice(invoice_id, "PENDING", True)


@dataclass(frozen=True)
class ExceptionSync:
    """What one exception sync changed."""

    match_status: str
    upserted: int
    closed: int


async def sync_fact_exceptions(
    conn: AsyncConnection, *, invoice_id: str, rules: MatchingRules
) -> ExceptionSync:
    """Make the invoice's exceptions agree with its recorded facts."""
    params: dict[str, Any] = {
        "invoice_id": invoice_id,
        "thresholds": json.dumps(
            {
                field: str(rules.confidence_threshold(field))
                for field in sorted(HEADER_FIELDS)
            }
        ),
        "required": sorted(required_header_fields()),
        "severities": json.dumps(
            {kind.value: rules.severity(kind) for kind in ExceptionType}
        ),
    }
    row = (await conn.execute(query("sync_fact_exceptions"), params)).one()
    return ExceptionSync(
        match_status=row.match_status,
        upserted=int(row.upserted),
        closed=int(row.closed),
    )
