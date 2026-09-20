"""Schema validation, flattening and the money discipline."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.common.errors import ExtractionSchemaError
from src.extraction.normalise import normalise_document
from src.extraction.validation import (
    flatten_fields,
    get_validator,
    pointer_from_parts,
    to_result_columns,
    validate_extraction,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _valid_payload() -> dict[str, Any]:
    return {
        "invoice_number": {"value": "INV-1", "confidence": 0.99, "page_number": 1},
        "total_amount": {"value": "100.0000", "confidence": 0.98, "page_number": 1},
        "currency": {"value": "SGD", "confidence": 0.97, "page_number": 1},
        "line_items": [
            {
                "description": {"value": "Thing", "confidence": 0.9},
                "quantity": {"value": "2.000000", "confidence": 0.9},
                "unit_price": {"value": "50.0000", "confidence": 0.9},
                "line_total": {"value": "100.0000", "confidence": 0.9},
            }
        ],
    }


def test_schema_is_itself_valid() -> None:
    get_validator()


def test_valid_payload_passes() -> None:
    assert validate_extraction(_valid_payload())


def test_missing_required_key_fails_with_pointer() -> None:
    payload = _valid_payload()
    del payload["total_amount"]
    with pytest.raises(ExtractionSchemaError) as caught:
        validate_extraction(payload)
    assert caught.value.pointer == "/"
    assert "total_amount" in str(caught.value)


def test_confidence_above_one_fails_at_its_pointer() -> None:
    payload = _valid_payload()
    payload["line_items"][0]["unit_price"]["confidence"] = 1.5
    with pytest.raises(ExtractionSchemaError) as caught:
        validate_extraction(payload)
    assert caught.value.pointer == "/line_items/0/unit_price/confidence"


def test_currency_must_be_three_uppercase_letters() -> None:
    payload = _valid_payload()
    payload["currency"]["value"] = "sgd"
    with pytest.raises(ExtractionSchemaError) as caught:
        validate_extraction(payload)
    assert caught.value.pointer == "/currency/value"


def test_amount_as_json_number_is_rejected() -> None:
    """Money must be a string.

    A JSON number is parsed to a float before validation runs, so accepting
    one would put binary floating point between the extractor and
    numeric(18,4). The schema refuses it rather than rounding it later.
    """
    payload = _valid_payload()
    payload["total_amount"]["value"] = 100.0
    with pytest.raises(ExtractionSchemaError) as caught:
        validate_extraction(payload)
    assert caught.value.pointer == "/total_amount/value"


def test_null_value_is_distinct_from_a_missing_key() -> None:
    """A reported-but-empty field and an unreported field differ.

    Spec 02 treats them differently, so the schema must not collapse them.
    """
    payload = _valid_payload()
    payload["supplier_name"] = {"value": None, "confidence": None}
    validated = validate_extraction(payload)
    assert "supplier_name" in validated
    assert validated["supplier_name"]["value"] is None
    assert "supplier_tax_id" not in validated


def test_envelope_without_confidence_is_rejected() -> None:
    payload = _valid_payload()
    payload["supplier_name"] = {"value": "Someone"}
    with pytest.raises(ExtractionSchemaError) as caught:
        validate_extraction(payload)
    assert caught.value.pointer == "/supplier_name"


def test_pointer_escaping_follows_rfc_6901() -> None:
    assert pointer_from_parts(["a/b"]) == "/a~1b"
    assert pointer_from_parts(["a~b"]) == "/a~0b"
    assert pointer_from_parts(["line_items", 3, "unit_price"]) == (
        "/line_items/3/unit_price"
    )


def test_flatten_gives_one_row_per_field_with_unique_pointers() -> None:
    payload = _valid_payload()
    payload["line_items"].append(payload["line_items"][0].copy())
    fields = flatten_fields(validate_extraction(payload))
    paths = [field.field_path for field in fields]
    assert len(paths) == len(set(paths))
    assert "/line_items/0/unit_price" in paths
    assert "/line_items/1/unit_price" in paths


def test_columns_are_decimal_not_float() -> None:
    columns = to_result_columns(validate_extraction(_valid_payload()))
    assert isinstance(columns["total_amount"], Decimal)
    assert columns["total_amount"] == Decimal("100.0000")


def test_confidence_converts_without_binary_error() -> None:
    payload = _valid_payload()
    payload["total_amount"]["confidence"] = 0.1 + 0.2
    fields = {
        field.field_path: field
        for field in flatten_fields(validate_extraction(payload))
    }
    assert fields["/total_amount"].confidence == Decimal("0.3000")


@pytest.mark.parametrize(
    "record",
    [
        record
        for record in json.loads((FIXTURES / "manifest.json").read_text("utf-8"))
        if not record.get("malformed")
    ],
    ids=lambda record: str(record["slug"]),
)
def test_every_fixture_normalises_and_validates(record: dict[str, Any]) -> None:
    raw = json.loads(
        (FIXTURES / "extractions" / f"{record['content_hash']}.json").read_text("utf-8")
    )
    payload = validate_extraction(normalise_document(raw))
    fields = flatten_fields(payload)
    paths = [field.field_path for field in fields]
    assert len(paths) == len(set(paths))
    columns = to_result_columns(payload)
    assert columns["total_amount"] == Decimal(str(record["total_amount"]))
    assert columns["currency"] == record["currency"]


def test_the_malformed_fixture_fails_validation_with_a_pointer() -> None:
    """A committed fixture that must fail, so the failure path has a case."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text("utf-8"))
    record = next(item for item in manifest if item.get("malformed"))
    raw = json.loads(
        (FIXTURES / "extractions" / f"{record['content_hash']}.json").read_text("utf-8")
    )
    with pytest.raises(ExtractionSchemaError) as caught:
        validate_extraction(normalise_document(raw))
    assert caught.value.pointer == "/invoice_number/confidence"
