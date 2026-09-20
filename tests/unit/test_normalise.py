"""Mapping raw Document AI output onto the extraction schema."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.extraction.docai import merge_shards
from src.extraction.normalise import normalise_document, trim_raw_payload

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _entity(kind: str, mention: str, confidence: float, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": kind,
        "mentionText": mention,
        "confidence": confidence,
        "pageAnchor": {"pageRefs": [{"page": "0"}]},
    }
    node.update(extra)
    return node


def test_money_comes_from_units_and_nanos_exactly() -> None:
    document = {
        "entities": [
            _entity(
                "total_amount",
                "$1,234.56",
                0.98,
                normalizedValue={
                    "moneyValue": {
                        "currencyCode": "SGD",
                        "units": "1234",
                        "nanos": 560000000,
                    }
                },
            )
        ]
    }
    payload = normalise_document(document)
    assert payload["total_amount"]["value"] == "1234.5600"
    assert Decimal(payload["total_amount"]["value"]) == Decimal("1234.56")


def test_money_falls_back_to_text_without_losing_precision() -> None:
    document = {"entities": [_entity("total_amount", "SGD 99.99", 0.9)]}
    payload = normalise_document(document)
    assert payload["total_amount"]["value"] == "99.9900"


def test_bracketed_amount_is_read_as_negative() -> None:
    document = {"entities": [_entity("net_amount", "(250.00)", 0.9)]}
    payload = normalise_document(document)
    assert payload["net_amount"]["value"] == "-250.0000"


def test_currency_is_taken_from_a_normalised_amount_when_absent() -> None:
    document = {
        "entities": [
            _entity(
                "total_amount",
                "100",
                0.9,
                normalizedValue={
                    "moneyValue": {
                        "currencyCode": "eur",
                        "units": "100",
                        "nanos": 0,
                    }
                },
            )
        ]
    }
    assert normalise_document(document)["currency"]["value"] == "EUR"


def test_ambiguous_date_text_is_refused_rather_than_guessed() -> None:
    """03/04/2026 is two different dates depending on locale.

    Guessing would silently corrupt a due date, which is worse than
    reporting nothing.
    """
    document = {"entities": [_entity("due_date", "03/04/2026", 0.9)]}
    assert normalise_document(document)["due_date"]["value"] is None


def test_normalised_date_value_is_used_when_present() -> None:
    document = {
        "entities": [
            _entity(
                "invoice_date",
                "3 April 2026",
                0.9,
                normalizedValue={"dateValue": {"year": 2026, "month": 4, "day": 3}},
            )
        ]
    }
    assert normalise_document(document)["invoice_date"]["value"] == "2026-04-03"


def test_page_numbers_become_one_based() -> None:
    document = {
        "entities": [
            {
                "type": "invoice_id",
                "mentionText": "INV-9",
                "confidence": 0.9,
                "pageAnchor": {"pageRefs": [{"page": "2"}]},
            }
        ]
    }
    assert normalise_document(document)["invoice_number"]["page_number"] == 3


def test_required_fields_are_present_with_null_when_unreported() -> None:
    payload = normalise_document({"entities": []})
    assert payload["invoice_number"]["value"] is None
    assert payload["total_amount"]["value"] is None
    assert "supplier_name" not in payload


def test_highest_confidence_mention_wins() -> None:
    document = {
        "entities": [
            _entity("invoice_id", "INV-WRONG", 0.41),
            _entity("invoice_id", "INV-RIGHT", 0.97),
        ]
    }
    assert normalise_document(document)["invoice_number"]["value"] == "INV-RIGHT"


def test_shards_merge_in_page_order_and_keep_every_entity() -> None:
    """A multi page batch writes several output objects.

    Reading only the first would silently drop line items from later pages.
    """
    second = {
        "pages": [{"pageNumber": 3}],
        "entities": [_entity("line_item", "late line", 0.9)],
        "text": "second",
    }
    first = {
        "pages": [{"pageNumber": 1}, {"pageNumber": 2}],
        "entities": [_entity("line_item", "early line", 0.9)],
        "text": "first",
    }
    merged = merge_shards([second, first])
    assert [page["pageNumber"] for page in merged["pages"]] == [1, 2, 3]
    assert [entity["mentionText"] for entity in merged["entities"]] == [
        "early line",
        "late line",
    ]
    assert merged["text"] == "firstsecond"


def test_trimming_drops_geometry_but_keeps_entities() -> None:
    """The trimmed payload is what lands in Postgres.

    The untrimmed response carries the OCR text layer and per token
    geometry, which is megabytes for a scanned multi page invoice.
    """
    document = {
        "mimeType": "application/pdf",
        "text": "x" * 100_000,
        "pages": [
            {
                "pageNumber": 1,
                "tokens": [{"layout": {"boundingPoly": {"vertices": [1] * 500}}}],
            }
        ],
        "entities": [_entity("invoice_id", "INV-1", 0.9)],
    }
    trimmed = trim_raw_payload(document)
    assert "text" not in trimmed
    assert trimmed["pageCount"] == 1
    assert trimmed["entities"][0]["mentionText"] == "INV-1"
    assert len(json.dumps(trimmed)) < len(json.dumps(document)) / 10


def test_multi_page_fixture_keeps_lines_from_every_page() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text("utf-8"))
    record = next(item for item in manifest if item["slug"] == "grandmere-equipment")
    raw = json.loads(
        (FIXTURES / "extractions" / f"{record['content_hash']}.json").read_text("utf-8")
    )
    payload = normalise_document(raw)
    pages = {line["description"]["page_number"] for line in payload["line_items"]}
    assert len(payload["line_items"]) == 7
    assert pages == {1, 2, 3}


def test_poor_quality_scan_reports_nulls_not_guesses() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text("utf-8"))
    record = next(item for item in manifest if item["slug"] == "dockside-marine")
    raw = json.loads(
        (FIXTURES / "extractions" / f"{record['content_hash']}.json").read_text("utf-8")
    )
    payload = normalise_document(raw)
    assert "supplier_tax_id" not in payload
    assert "due_date" not in payload
    assert payload["total_amount"]["value"] == "1890.0000"
    assert payload["line_items"][1]["unit_price"]["value"] is None
    assert float(payload["total_amount"]["confidence"]) < 0.6
