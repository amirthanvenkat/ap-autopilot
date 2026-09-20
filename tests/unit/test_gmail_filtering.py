"""Attachment filtering and media type handling.

The filter is a cost control, not tidiness. Every email signature logo that
gets through becomes a document row and a paid Document AI call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.common import media
from src.common.errors import UnsupportedMediaTypeError
from src.ingestion.gmail import qualifying_attachments

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _message(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "msg-test",
        "internalDate": "1755600000000",
        "payload": {"mimeType": "multipart/mixed", "parts": parts},
    }


def _part(
    filename: str,
    mime: str,
    size: int,
    *,
    attachment_id: str = "att-1",
    headers: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "mimeType": mime,
        "headers": headers or [],
        "body": {"attachmentId": attachment_id, "size": size},
    }


def test_a_plain_pdf_attachment_qualifies() -> None:
    message = _message([_part("invoice.pdf", "application/pdf", 40_000)])
    assert len(qualifying_attachments(message, min_bytes=8192)) == 1


def test_inline_parts_are_skipped_by_content_id() -> None:
    message = _message(
        [
            _part(
                "logo.png",
                "image/png",
                40_000,
                headers=[{"name": "Content-ID", "value": "<logo@x>"}],
            )
        ]
    )
    assert qualifying_attachments(message, min_bytes=8192) == []


def test_inline_parts_are_skipped_by_disposition() -> None:
    message = _message(
        [
            _part(
                "logo.png",
                "image/png",
                40_000,
                headers=[
                    {"name": "Content-Disposition", "value": "inline; filename=x.png"}
                ],
            )
        ]
    )
    assert qualifying_attachments(message, min_bytes=8192) == []


def test_unsupported_types_are_skipped() -> None:
    message = _message([_part("terms.docx", "application/msword", 40_000)])
    assert qualifying_attachments(message, min_bytes=8192) == []


def test_small_parts_are_skipped() -> None:
    message = _message([_part("tiny.pdf", "application/pdf", 400)])
    assert qualifying_attachments(message, min_bytes=8192) == []


def test_nested_multipart_attachments_are_found() -> None:
    message = {
        "id": "msg-nested",
        "internalDate": "1755600000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/related",
                    "filename": "",
                    "body": {},
                    "parts": [_part("deep.pdf", "application/pdf", 40_000)],
                }
            ],
        },
    }
    assert len(qualifying_attachments(message, min_bytes=8192)) == 1


def test_three_attachments_yield_three_documents() -> None:
    """Section 11 question 1, decided as three.

    The downstream unit is the invoice, so three invoices on one email are
    three documents that happen to share a source reference.
    """
    message = json.loads(
        (FIXTURES / "gmail" / "messages" / "msg-0001.json").read_text("utf-8")
    )
    attachments = qualifying_attachments(message, min_bytes=8192)
    assert len(attachments) == 3
    assert len({item.attachment_id for item in attachments}) == 3
    assert {item.message_id for item in attachments} == {"msg-0001"}


def test_signature_logo_in_the_committed_fixture_is_filtered_out() -> None:
    message = json.loads(
        (FIXTURES / "gmail" / "messages" / "msg-0002.json").read_text("utf-8")
    )
    attachments = qualifying_attachments(message, min_bytes=8192)
    assert [item.filename for item in attachments] == ["harbourpoint-it.pdf"]


def test_sniffed_type_beats_a_lying_declaration() -> None:
    """The bytes are what Document AI will read, so the bytes decide."""
    pdf = (FIXTURES / "source" / "acme-office-supplies.pdf").read_bytes()
    assert media.require_accepted("image/png", pdf) == media.PDF


def test_unknown_content_is_rejected() -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        media.require_accepted("application/pdf", b"not a document at all")


def test_unsupported_declared_type_is_rejected() -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        media.require_accepted("application/zip", b"PK\x03\x04rest")


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("acme-office-supplies.pdf", media.PDF),
        ("kingsway-couriers.png", media.PNG),
        ("orchid-labs.tif", media.TIFF),
    ],
)
def test_committed_source_documents_sniff_correctly(slug: str, expected: str) -> None:
    data = (FIXTURES / "source" / slug).read_bytes()
    assert media.sniff(data) == expected
    assert media.require_accepted(None, data) == expected
