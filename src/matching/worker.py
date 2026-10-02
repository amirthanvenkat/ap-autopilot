"""The match_invoice task.

README 5.2: one task, one transaction. Either the invoice exists with its
exceptions and status, or nothing does and the task can be retried.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.common.acceptance import task_data
from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.errors import PermanentError, TransientError
from src.common.ids import derived_id
from src.common.logging import get_logger
from src.common.outbox import HANDLER_MATCH_INVOICE, OutboxTask, accept_message
from src.matching.canonical import (
    FINAL_STATUSES,
    canonicalise,
    sync_fact_exceptions,
)

log = get_logger(__name__)

OUTCOME_SYNCED = "synced"
OUTCOME_SKIPPED_FINAL = "skipped_final"


@dataclass(frozen=True)
class MatchResult:
    """What one match_invoice task did."""

    outcome: str
    invoice_id: str
    match_status: str
    created: bool


async def queue_match(conn: AsyncConnection, *, extraction_id: str) -> str:
    """Queue the match for a stored extraction, in the caller's transaction.

    Called from the transaction that stores the extraction, so a stored
    extraction always has a queued match. The message id is derived, so a
    second call for the same extraction is a no-op.
    """
    acceptance = await accept_message(
        conn,
        handler=HANDLER_MATCH_INVOICE,
        message_id=derived_id("match-invoice", extraction_id),
        payload={
            "data": {"extraction_id": extraction_id},
            "attributes": {},
            "raw": None,
        },
    )
    return acceptance.task_id


async def process_match_invoice(deps: Dependencies, task: OutboxTask) -> MatchResult:
    """Canonicalise an extraction and derive its exceptions."""
    extraction_id = task_data(task).get("extraction_id")
    if not isinstance(extraction_id, str) or not extraction_id:
        raise PermanentError("match_invoice task names no extraction")

    try:
        async with transaction(deps.engine) as conn:
            invoice = await canonicalise(
                conn, extraction_id=extraction_id, rules=deps.rules
            )
            if invoice.match_status in FINAL_STATUSES:
                # A late duplicate delivery. Harmless, so not a failure.
                log.info(
                    "match.skipped_final",
                    invoice_id=invoice.invoice_id,
                    match_status=invoice.match_status,
                )
                return MatchResult(
                    OUTCOME_SKIPPED_FINAL,
                    invoice.invoice_id,
                    invoice.match_status,
                    invoice.created,
                )
            synced = await sync_fact_exceptions(
                conn, invoice_id=invoice.invoice_id, rules=deps.rules
            )
    except IntegrityError as exc:
        # The duplicate lock should make this impossible. If the unique
        # index fires anyway, the retry sees the committed original and
        # records this invoice as its duplicate.
        if "invoices_supplier_number_uidx" in str(exc.orig):
            raise TransientError(
                "Concurrent invoice with the same supplier and number",
                detail=extraction_id,
            ) from exc
        raise

    log.info(
        "match.synced",
        invoice_id=invoice.invoice_id,
        created=invoice.created,
        match_status=synced.match_status,
        upserted=synced.upserted,
        closed=synced.closed,
    )
    return MatchResult(
        OUTCOME_SYNCED, invoice.invoice_id, synced.match_status, invoice.created
    )
