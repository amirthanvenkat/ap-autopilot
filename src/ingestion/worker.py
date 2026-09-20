"""The Gmail ingestion worker.

Runs outside the push handler, after the acceptance transaction has already
committed the task. Nothing here is on the one second path, so it is free to
call Gmail, write to Cloud Storage and start a Document AI batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from src.common.acceptance import task_data
from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.errors import GmailHistoryExpiredError
from src.common.logging import get_logger
from src.common.outbox import OutboxTask
from src.ingestion import watch
from src.ingestion.gmail import HistoryPage
from src.ingestion.service import SOURCE_GMAIL, ingest_bytes

log = get_logger(__name__)


@dataclass(frozen=True)
class NotificationResult:
    """What one Gmail notification produced."""

    messages_seen: int
    attachments_ingested: int
    documents_created: int
    jobs_started: int
    resynced: bool


async def process_gmail_notification(
    deps: Dependencies, task: OutboxTask
) -> NotificationResult:
    """Read the history range a notification points at and ingest it."""
    payload = task_data(task)
    email_address = str(payload.get("emailAddress") or deps.settings.gmail_user_id)
    notified_history_id = str(payload.get("historyId") or "")

    async with transaction(deps.engine) as conn:
        state = await watch.get_state(conn, email_address)

    start_history_id = state.history_id if state else notified_history_id
    if not start_history_id:
        # No cursor and no id in the notification. There is nothing to diff
        # from, so take the notification's position and wait for the next.
        log.warning("gmail.no_cursor", email_address=email_address)
        return NotificationResult(0, 0, 0, 0, resynced=False)

    resynced = False
    try:
        page = await deps.gmail.list_added_messages(
            start_history_id=start_history_id,
            label_id=deps.settings.gmail_label,
        )
    except GmailHistoryExpiredError:
        # The cursor aged out. A history diff can never succeed again, so
        # enumerate the label instead. Content hash deduplication makes
        # re-reading old messages cheap.
        log.warning(
            "gmail.history_expired",
            email_address=email_address,
            history_id=start_history_id,
        )
        page = await deps.gmail.resync(label_id=deps.settings.gmail_label)
        resynced = True

    ingested = 0
    documents_created = 0
    jobs_started = 0

    for message_id in page.message_ids:
        attachments = await deps.gmail.list_attachments(message_id)
        for attachment in attachments:
            data = await deps.gmail.get_attachment(
                message_id=message_id, attachment_id=attachment.attachment_id
            )
            outcome = await ingest_bytes(
                deps,
                data=data,
                declared_media_type=attachment.media_type,
                source=SOURCE_GMAIL,
                # Three attachments on one email become three documents.
                # The message id links them for audit without coupling them.
                source_ref=message_id,
                received_at=attachment.received_at,
                attempt_key="1",
            )
            ingested += 1
            documents_created += int(outcome.document_created)
            jobs_started += int(outcome.job_started)

    await _advance_cursor(deps, email_address, page, notified_history_id)

    log.info(
        "gmail.notification_processed",
        messages=len(page.message_ids),
        attachments=ingested,
        documents_created=documents_created,
        jobs_started=jobs_started,
        resynced=resynced,
    )
    return NotificationResult(
        messages_seen=len(page.message_ids),
        attachments_ingested=ingested,
        documents_created=documents_created,
        jobs_started=jobs_started,
        resynced=resynced,
    )


async def _advance_cursor(
    deps: Dependencies,
    email_address: str,
    page: HistoryPage,
    fallback_history_id: str,
) -> None:
    """Move the cursor only once every attachment has been committed.

    A cursor advanced before the work is durable would skip the range on a
    crash, and a skipped range is an invoice nobody ever sees again.
    """
    new_history_id = page.new_history_id or fallback_history_id
    if not new_history_id:
        return
    async with transaction(deps.engine) as conn:
        await watch.save_state(
            conn, email_address=email_address, history_id=new_history_id
        )


async def renew_gmail_watch(deps: Dependencies) -> dict[str, object]:
    """Re-arm users.watch and record the new expiry.

    Called on a schedule. Without it the subscription lapses after seven
    days and ingestion stops with no error anywhere.
    """
    topic = (
        f"projects/{deps.settings.gcp_project_id}"
        f"/topics/{deps.settings.pubsub_work_topic}"
    )
    response = await deps.gmail.renew_watch(
        topic=topic, label_ids=[deps.settings.gmail_label]
    )
    expiration = response.get("expiration")
    expires_at = (
        datetime.fromtimestamp(int(expiration) / 1000, tz=UTC) if expiration else None
    )
    history_id = str(response.get("historyId") or "")
    if history_id:
        async with transaction(deps.engine) as conn:
            await watch.save_state(
                conn,
                email_address=deps.settings.gmail_user_id,
                history_id=history_id,
                watch_expires_at=expires_at,
            )
    return {"expiration": expiration, "history_id": history_id}
