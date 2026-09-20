"""Push envelope parsing, identifier derivation and poison handling."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from src.common.acceptance import parse_envelope, task_data
from src.common.errors import PermanentError
from src.common.ids import content_hash, derived_id, new_ulid
from src.common.outbox import OutboxTask, task_id_for
from src.ingestion.service import parse_object_name

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _envelope(message_id: str, data: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "message": {
                "messageId": message_id,
                "data": base64.b64encode(json.dumps(data).encode()).decode(),
                "attributes": {},
            },
            "subscription": "projects/p/subscriptions/s",
        }
    ).encode()


def test_message_id_and_data_are_read_from_the_envelope() -> None:
    body = _envelope("msg-1", {"emailAddress": "ap@example.test", "historyId": "9"})
    message_id, data, _attributes, _raw = parse_envelope(body)
    assert message_id == "msg-1"
    assert data["historyId"] == "9"


def test_committed_pubsub_fixtures_parse() -> None:
    for name in ("gmail_notify", "extraction_complete"):
        body = (FIXTURES / "pubsub" / f"{name}.json").read_bytes()
        message_id, data, _attributes, _raw = parse_envelope(body)
        assert message_id
        assert data


def test_eventarc_attributes_supply_the_object_name() -> None:
    """A Cloud Storage notification can carry the object in attributes."""
    body = json.dumps(
        {
            "message": {
                "messageId": "msg-2",
                "attributes": {
                    "objectId": "extractions/job-7/output-0-to-1.json",
                    "bucketId": "docs",
                },
            }
        }
    ).encode()
    _message_id, data, _attributes, _raw = parse_envelope(body)
    assert data["name"] == "extractions/job-7/output-0-to-1.json"


def test_unparseable_body_still_gets_a_stable_id() -> None:
    """A broken body must deduplicate on redelivery like any other.

    Without a stable id, every redelivery of the same poison message would
    create another row.
    """
    body = b"this is not json"
    first = parse_envelope(body)[0]
    second = parse_envelope(body)[0]
    assert first == second
    assert first.startswith("sha256:")


def test_unparseable_body_keeps_its_raw_text_for_diagnosis() -> None:
    _message_id, data, _attributes, raw = parse_envelope(b"not json")
    assert data == {}
    assert raw == "not json"


def test_committed_malformed_fixture_yields_no_data() -> None:
    body = (FIXTURES / "pubsub" / "malformed.json").read_bytes()
    message_id, data, _attributes, _raw = parse_envelope(body)
    assert message_id == "pubsub-msg-0003"
    assert data == {}


def test_a_message_with_no_readable_payload_fails_loudly() -> None:
    """A poison message is recorded, not quietly acknowledged.

    Returning 200 and doing nothing would lose the invoice with no trace.
    """
    task = OutboxTask(
        task_id="t",
        handler="gmail_notify",
        message_id="m",
        payload={"data": {}, "raw": "garbage"},
        attempts=1,
    )
    with pytest.raises(PermanentError) as caught:
        task_data(task)
    assert "garbage" in str(caught.value)


def test_task_ids_are_derived_and_stable() -> None:
    """A replay must collide on the primary key rather than duplicate."""
    first = task_id_for("gmail_notify", "msg-1")
    second = task_id_for("gmail_notify", "msg-1")
    assert first == second
    assert first != task_id_for("extraction_complete", "msg-1")


def test_random_identifiers_differ() -> None:
    assert new_ulid() != new_ulid()


def test_derived_identifiers_do_not_collide_across_part_boundaries() -> None:
    """Joining parts naively would make ("ab","c") equal ("a","bc")."""
    assert derived_id("ab", "c") != derived_id("a", "bc")


def test_content_hash_is_sha256_of_the_bytes() -> None:
    assert content_hash(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("extractions/job-7/output-0-to-1.json", "job-7"),
        ("extractions/job-7/nested/output.json", "job-7"),
        ("inbox/abc.pdf", None),
        ("extractions/", None),
        ("something/else/entirely.json", None),
    ],
)
def test_job_id_is_recovered_from_the_output_prefix(
    name: str, expected: str | None
) -> None:
    assert parse_object_name(name) == expected
