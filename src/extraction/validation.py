"""Schema validation and flattening for extraction payloads.

Validation failures raise ExtractionSchemaError carrying the JSON Pointer of
the offending node, which the job row records in error_detail. Nothing is
partially persisted: the caller validates before it opens the write
transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from src.common.errors import ExtractionSchemaError

SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "invoice_extraction.schema.json"
)

# Envelope keys. A node holding these is a field, not a container.
_VALUE = "value"
_CONFIDENCE = "confidence"
_PAGE = "page_number"

# Top level envelopes that map onto extraction_results columns.
_TEXT_COLUMNS = ("supplier_name", "supplier_tax_id", "invoice_number")
_DATE_COLUMNS = ("invoice_date", "due_date")
_MONEY_COLUMNS = ("net_amount", "tax_amount", "total_amount")


@dataclass(frozen=True)
class ExtractedField:
    """One row destined for extraction_fields."""

    field_path: str
    field_value: str | None
    confidence: Decimal | None
    page_number: int | None


def escape_pointer_token(token: str) -> str:
    """Escape a single JSON Pointer token per RFC 6901."""
    return token.replace("~", "~0").replace("/", "~1")


def pointer_from_parts(parts: list[str | int]) -> str:
    """Build a JSON Pointer from a path of keys and array indices."""
    if not parts:
        return ""
    return "/" + "/".join(
        str(part) if isinstance(part, int) else escape_pointer_token(part)
        for part in parts
    )


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """Read the schema from disk once."""
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema: dict[str, Any] = json.load(handle)
    return schema


@lru_cache(maxsize=1)
def get_validator() -> Draft202012Validator:
    """A validator with the schema checked for correctness itself."""
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_extraction(payload: Any) -> dict[str, Any]:
    """Validate a payload, raising with the pointer of the first failure.

    Errors are sorted so the reported pointer is stable across runs. An
    unstable pointer would make the recorded error_detail useless for
    grouping failures.
    """
    validator = get_validator()
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda err: (list(err.absolute_path), err.validator or ""),
    )
    if errors:
        first: ValidationError = errors[0]
        pointer = pointer_from_parts(list(first.absolute_path)) or "/"
        raise ExtractionSchemaError(
            "Extraction payload failed validation",
            pointer=pointer,
            detail=first.message,
        )
    if not isinstance(payload, dict):
        raise ExtractionSchemaError("Extraction payload is not an object", pointer="/")
    return payload


def _is_envelope(node: Any) -> bool:
    return isinstance(node, dict) and _VALUE in node and _CONFIDENCE in node


def _to_confidence(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    # Via str, so a float confidence never carries its binary error into
    # numeric(5,4).
    return Decimal(str(raw)).quantize(Decimal("0.0001"))


def flatten_fields(payload: dict[str, Any]) -> list[ExtractedField]:
    """Walk the payload into one row per field, keyed by JSON Pointer.

    Array indices are part of the pointer, so /line_items/3/unit_price is
    distinct from /line_items/4/unit_price. That is what lets
    extraction_fields carry a unique constraint on (extraction_id,
    field_path) and so what stops a replay silently doubling every per field
    analytic.
    """
    rows: list[ExtractedField] = []

    def walk(node: Any, parts: list[str | int]) -> None:
        if _is_envelope(node):
            value = node.get(_VALUE)
            rows.append(
                ExtractedField(
                    field_path=pointer_from_parts(parts),
                    field_value=None if value is None else str(value),
                    confidence=_to_confidence(node.get(_CONFIDENCE)),
                    page_number=node.get(_PAGE),
                )
            )
            return
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, [*parts, key])
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, [*parts, index])

    walk(payload, [])
    return sorted(rows, key=lambda row: row.field_path)


def _money(payload: dict[str, Any], key: str) -> Decimal | None:
    node = payload.get(key)
    if not isinstance(node, dict):
        return None
    raw = node.get(_VALUE)
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise ExtractionSchemaError(
            "Amount is not a decimal",
            pointer=pointer_from_parts([key, _VALUE]),
            detail=str(raw),
        ) from exc


def _text(payload: dict[str, Any], key: str) -> str | None:
    node = payload.get(key)
    if not isinstance(node, dict):
        return None
    raw = node.get(_VALUE)
    return None if raw is None else str(raw)


def _date(payload: dict[str, Any], key: str) -> date | None:
    raw = _text(payload, key)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ExtractionSchemaError(
            "Date is not ISO 8601",
            pointer=pointer_from_parts([key, _VALUE]),
            detail=raw,
        ) from exc


def to_result_columns(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the payload onto the extraction_results columns.

    Amounts arrive as strings and become Decimal here. They are never floats
    at any point, which is the whole reason the schema types them as strings.
    """
    columns: dict[str, Any] = {}
    for key in _TEXT_COLUMNS:
        columns[key] = _text(payload, key)
    for key in _DATE_COLUMNS:
        columns[key] = _date(payload, key)
    for key in _MONEY_COLUMNS:
        columns[key] = _money(payload, key)
    columns["currency"] = _text(payload, "currency")
    return columns
