"""Extraction result persistence.

The result row, every field row and the job status change are written in one
transaction. Splitting them is what would let a worker retry double the
field rows, and a doubled extraction_fields table corrupts every per field
confidence analytic silently rather than loudly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.common.ids import derived_id
from src.common.logging import get_logger
from src.extraction.validation import ExtractedField

log = get_logger(__name__)


@dataclass(frozen=True)
class StoredExtraction:
    """A persisted extraction result."""

    extraction_id: str
    job_id: str
    created: bool


def extraction_id_for(job_id: str) -> str:
    """Derive the extraction id so a replay collides on the primary key."""
    return derived_id("extraction", job_id)


_INSERT_RESULT_SQL = text(
    """
    insert into extraction_results (
        extraction_id, job_id, document_id,
        supplier_name, supplier_tax_id, invoice_number, po_number,
        invoice_date, due_date, currency,
        net_amount, tax_amount, total_amount,
        raw_payload, raw_gcs_uri
    )
    values (
        :extraction_id, :job_id, :document_id,
        :supplier_name, :supplier_tax_id, :invoice_number, :po_number,
        :invoice_date, :due_date, :currency,
        :net_amount, :tax_amount, :total_amount,
        cast(:raw_payload as jsonb), :raw_gcs_uri
    )
    on conflict (job_id) do nothing
    returning extraction_id
    """
)

_SELECT_RESULT_BY_JOB_SQL = text(
    """
    select extraction_id from extraction_results where job_id = :job_id
    """
)

_INSERT_FIELD_SQL = text(
    """
    insert into extraction_fields (
        extraction_id, field_path, field_value, confidence, page_number
    )
    values (
        :extraction_id, :field_path, :field_value, :confidence, :page_number
    )
    on conflict on constraint extraction_fields_path_uidx do nothing
    """
)


async def insert_extraction(
    conn: AsyncConnection,
    *,
    job_id: str,
    document_id: str,
    columns: dict[str, Any],
    fields: list[ExtractedField],
    raw_payload: dict[str, Any],
    raw_gcs_uri: str,
) -> StoredExtraction:
    """Write the result and all of its fields.

    Caller must already be inside a transaction that also marks the job
    SUCCEEDED, so the three writes commit together.
    """
    extraction_id = extraction_id_for(job_id)
    row = (
        await conn.execute(
            _INSERT_RESULT_SQL,
            {
                "extraction_id": extraction_id,
                "job_id": job_id,
                "document_id": document_id,
                "raw_payload": json.dumps(raw_payload, separators=(",", ":")),
                "raw_gcs_uri": raw_gcs_uri,
                **columns,
            },
        )
    ).first()

    if row is None:
        existing = (
            await conn.execute(_SELECT_RESULT_BY_JOB_SQL, {"job_id": job_id})
        ).one()
        log.info("extraction.already_present", job_id=job_id)
        return StoredExtraction(
            extraction_id=existing.extraction_id, job_id=job_id, created=False
        )

    if fields:
        await conn.execute(
            _INSERT_FIELD_SQL,
            [
                {
                    "extraction_id": extraction_id,
                    "field_path": field.field_path,
                    "field_value": field.field_value,
                    "confidence": field.confidence,
                    "page_number": field.page_number,
                }
                for field in fields
            ],
        )

    return StoredExtraction(extraction_id=extraction_id, job_id=job_id, created=True)


_SELECT_EXTRACTION_SQL = text(
    """
    select extraction_id, job_id, document_id, supplier_name, supplier_tax_id,
           invoice_number, po_number, invoice_date, due_date, currency,
           net_amount, tax_amount, total_amount, raw_gcs_uri, created_at
      from extraction_results
     where extraction_id = :extraction_id
    """
)

_SELECT_FIELDS_SQL = text(
    """
    select field_path, field_value, confidence, page_number
      from extraction_fields
     where extraction_id = :extraction_id
     order by field_path
    """
)


async def get_extraction(
    conn: AsyncConnection, extraction_id: str
) -> dict[str, Any] | None:
    """Read one extraction and its fields.

    Note the explicit column list. raw_payload is deliberately not selected:
    it is the largest column in the schema and the callers of this function
    do not read it.
    """
    row = (
        await conn.execute(_SELECT_EXTRACTION_SQL, {"extraction_id": extraction_id})
    ).first()
    if row is None:
        return None
    fields = (
        await conn.execute(_SELECT_FIELDS_SQL, {"extraction_id": extraction_id})
    ).all()
    return {
        "extraction_id": row.extraction_id,
        "job_id": row.job_id,
        "document_id": row.document_id,
        "supplier_name": row.supplier_name,
        "supplier_tax_id": row.supplier_tax_id,
        "invoice_number": row.invoice_number,
        "po_number": row.po_number,
        "invoice_date": row.invoice_date,
        "due_date": row.due_date,
        "currency": row.currency,
        "net_amount": row.net_amount,
        "tax_amount": row.tax_amount,
        "total_amount": row.total_amount,
        "raw_gcs_uri": row.raw_gcs_uri,
        "created_at": row.created_at,
        "fields": [
            {
                "field_path": field.field_path,
                "field_value": field.field_value,
                "confidence": field.confidence,
                "page_number": field.page_number,
            }
            for field in fields
        ],
    }
