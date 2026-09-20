"""At-least-once delivery must produce exactly one of everything.

These are the mandatory tests from spec 01 section 8 and CLAUDE.md section
7, plus the cases the outbox design exists to survive.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from src.common.acceptance import accept_push
from src.common.deps import Dependencies
from src.common.outbox import HANDLER_GMAIL_NOTIFY, task_id_for
from src.common.runner import sweep
from src.ingestion.service import SOURCE_GMAIL, SOURCE_UPLOAD, ingest_bytes
from tests.conftest import requires_database
from tests.integration.conftest import count, drain, fetch_one, seed_watch_cursor

pytestmark = requires_database

EMAIL = "ap@example.test"


def envelope(message_id: str, history_id: str = "2000") -> bytes:
    return json.dumps(
        {
            "message": {
                "messageId": message_id,
                "data": base64.b64encode(
                    json.dumps(
                        {"emailAddress": EMAIL, "historyId": history_id}
                    ).encode()
                ).decode(),
                "attributes": {},
            },
            "subscription": "projects/p/subscriptions/gmail-notifications-push",
        }
    ).encode()


async def test_same_message_three_times_produces_one_of_everything(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    """The headline guarantee.

    History 2000 holds one message with one qualifying attachment and one
    inline logo that the filter drops.
    """
    await seed_watch_cursor(engine, EMAIL, "2000")
    body = envelope("msg-replay-1")

    for _ in range(3):
        accepted = await accept_push(
            deps,
            handler=HANDLER_GMAIL_NOTIFY,
            authorization=auth_header["Authorization"],
            body=body,
        )
        assert accepted.task_id == task_id_for(HANDLER_GMAIL_NOTIFY, "msg-replay-1")
        await drain(deps)

    assert await count(engine, "processed_events", "handler = 'gmail_notify'") == 1
    assert await count(engine, "documents") == 1
    assert await count(engine, "extraction_jobs") == 1
    assert await count(engine, "extraction_results") == 1
    assert await count(engine, "extraction_jobs", "status = 'SUCCEEDED'") == 1


async def test_the_second_delivery_is_reported_as_a_duplicate(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    await seed_watch_cursor(engine, EMAIL, "2000")
    body = envelope("msg-replay-2")
    first = await accept_push(
        deps,
        handler=HANDLER_GMAIL_NOTIFY,
        authorization=auth_header["Authorization"],
        body=body,
    )
    second = await accept_push(
        deps,
        handler=HANDLER_GMAIL_NOTIFY,
        authorization=auth_header["Authorization"],
        body=body,
    )
    assert first.duplicate is False
    assert second.duplicate is True
    assert await count(engine, "ingestion_outbox") == 1


async def test_same_content_under_two_message_ids_is_one_document(
    deps: Dependencies, engine: AsyncEngine, sample_document: tuple[bytes, str, str]
) -> None:
    """Content level deduplication.

    Two different Pub/Sub messages carrying the same invoice must not become
    two documents. Overlapping Gmail history ranges make this the common
    case, not the rare one.
    """
    data, media_type, digest = sample_document
    for reference in ("gmail-message-a", "gmail-message-b"):
        await ingest_bytes(
            deps,
            data=data,
            declared_media_type=media_type,
            source=SOURCE_GMAIL,
            source_ref=reference,
            received_at=datetime.now(tz=UTC),
            attempt_key="1",
        )

    assert await count(engine, "documents") == 1
    row = await fetch_one(engine, "select content_hash from documents limit 1")
    assert row.content_hash == digest
    # Both routes in are recorded, so a deduplicated second sender is not
    # silently lost from the audit trail.
    assert await count(engine, "document_sources") == 2


async def test_duplicate_content_does_not_buy_a_second_extraction(
    deps: Dependencies, engine: AsyncEngine, sample_document: tuple[bytes, str, str]
) -> None:
    """Deduplication at documents is not enough on its own.

    Without the successful-extraction check, the same bytes would still
    start a second job under a new attempt number and be paid for twice.
    """
    data, media_type, _digest = sample_document
    first = await ingest_bytes(
        deps,
        data=data,
        declared_media_type=media_type,
        source=SOURCE_UPLOAD,
        source_ref="upload-a.pdf",
        received_at=datetime.now(tz=UTC),
        attempt_key="1",
    )
    await drain(deps)
    assert await count(engine, "extraction_results") == 1

    second = await ingest_bytes(
        deps,
        data=data,
        declared_media_type=media_type,
        source=SOURCE_UPLOAD,
        source_ref="upload-b.pdf",
        received_at=datetime.now(tz=UTC),
        attempt_key="2",
    )
    assert second.job_started is False
    assert second.reason == "already_extracted"
    assert second.document_id == first.document_id
    assert await count(engine, "extraction_jobs") == 1


async def test_concurrent_redelivery_accepts_exactly_once(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    """Two instances, one message, no lock of our own.

    They contend on the processed_events primary key. Postgres blocks the
    second insert until the first transaction resolves, so the second only
    ever sees "already accepted" after the work is durable.
    """
    await seed_watch_cursor(engine, EMAIL, "2000")
    body = envelope("msg-concurrent")

    results = await asyncio.gather(
        *(
            accept_push(
                deps,
                handler=HANDLER_GMAIL_NOTIFY,
                authorization=auth_header["Authorization"],
                body=body,
            )
            for _ in range(4)
        )
    )
    assert sum(1 for item in results if not item.duplicate) == 1
    assert await count(engine, "processed_events") == 1
    assert await count(engine, "ingestion_outbox") == 1

    await drain(deps)
    assert await count(engine, "documents") == 1
    assert await count(engine, "extraction_results") == 1


async def test_crash_after_acceptance_loses_nothing(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    """The reason the outbox exists.

    The handler commits and the instance dies before the publish lands. No
    worker is ever nudged, so only the sweeper can recover the work. Under
    the original ordering, where the marker commits alone, the redelivery
    would see the marker and the invoice would be lost.
    """
    await seed_watch_cursor(engine, EMAIL, "2000")
    accepted = await accept_push(
        deps,
        handler=HANDLER_GMAIL_NOTIFY,
        authorization=auth_header["Authorization"],
        body=envelope("msg-crash"),
    )
    # Simulate the crash: nothing runs the task.
    assert await count(engine, "ingestion_outbox", "status = 'PENDING'") == 1
    assert await count(engine, "documents") == 0

    # A redelivery is suppressed, which is only safe because the work is
    # already committed.
    repeat = await accept_push(
        deps,
        handler=HANDLER_GMAIL_NOTIFY,
        authorization=auth_header["Authorization"],
        body=envelope("msg-crash"),
    )
    assert repeat.duplicate is True

    await drain(deps)
    assert await count(engine, "documents") == 1
    assert await count(engine, "extraction_results") == 1

    # The recovered task finished, and nothing was left behind. The count of
    # DONE rows is not asserted directly: processing the notification queues
    # the completion that follows it, so the total is a property of the
    # pipeline rather than of this recovery.
    row = await fetch_one(
        engine,
        "select status from ingestion_outbox where task_id = :task_id",
        task_id=accepted.task_id,
    )
    assert row.status == "DONE"
    assert (
        await count(
            engine, "ingestion_outbox", "status in ('PENDING','LEASED','FAILED')"
        )
        == 0
    )


async def test_a_poison_message_is_recorded_not_silently_acknowledged(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    """Section 4 wanted 200 for everything. That loses the evidence.

    The message is accepted so Pub/Sub stops redelivering, and the failure
    is recorded against the outbox row where it can be seen.
    """
    accepted = await accept_push(
        deps,
        handler=HANDLER_GMAIL_NOTIFY,
        authorization=auth_header["Authorization"],
        body=b"this is not a pub/sub envelope",
    )
    assert accepted.duplicate is False

    results = await drain(deps)
    assert [item.result for item in results] == ["failed"]

    row = await fetch_one(
        engine,
        "select status, last_error, attempts from ingestion_outbox "
        "where task_id = :task_id",
        task_id=accepted.task_id,
    )
    assert row.status == "FAILED"
    assert "no readable JSON payload" in row.last_error
    # Not retried five times: the failure is deterministic.
    assert row.attempts == 1


async def test_a_transient_failure_returns_to_the_queue(
    deps: Dependencies, engine: AsyncEngine, auth_header: dict[str, str]
) -> None:
    """Transient and permanent failures must not be treated alike."""
    from src.common.errors import UpstreamUnavailableError
    from src.ingestion.gmail import HistoryPage

    await seed_watch_cursor(engine, EMAIL, "2000")
    accepted = await accept_push(
        deps,
        handler=HANDLER_GMAIL_NOTIFY,
        authorization=auth_header["Authorization"],
        body=envelope("msg-transient"),
    )

    calls = {"count": 0}
    original = deps.gmail.list_added_messages

    async def flaky(*, start_history_id: str, label_id: str | None) -> HistoryPage:
        calls["count"] += 1
        if calls["count"] == 1:
            raise UpstreamUnavailableError("Gmail is unavailable", status_code=503)
        return await original(start_history_id=start_history_id, label_id=label_id)

    object.__setattr__(deps.gmail, "list_added_messages", flaky)

    first = await sweep(deps)
    assert [item.result for item in first] == ["retry"]
    row = await fetch_one(
        engine,
        "select status from ingestion_outbox where task_id = :task_id",
        task_id=accepted.task_id,
    )
    assert row.status == "PENDING"

    second = await drain(deps)
    assert {item.result for item in second} == {"done"}
    assert await count(engine, "extraction_results") == 1


@pytest.mark.parametrize("attachments_expected", [3])
async def test_three_attachments_become_three_documents(
    deps: Dependencies,
    engine: AsyncEngine,
    auth_header: dict[str, str],
    attachments_expected: int,
) -> None:
    """Section 11 question 1, end to end.

    They share a source reference, so the email is still recoverable for
    audit, but each invoice is its own document, job and result.
    """
    await seed_watch_cursor(engine, EMAIL, "1000")
    await accept_push(
        deps,
        handler=HANDLER_GMAIL_NOTIFY,
        authorization=auth_header["Authorization"],
        body=envelope("msg-three", history_id="1000"),
    )
    await drain(deps)

    grouped = await fetch_one(
        engine,
        "select count(*) as total from documents where source_ref = 'msg-0001'",
    )
    assert grouped.total == attachments_expected
    assert await count(engine, "extraction_results") >= attachments_expected
