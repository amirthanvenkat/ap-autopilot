"""Map a raw Document AI response onto the extraction schema.

Everything here is pure. It takes the cached response shape and returns the
envelope structure that invoice_extraction.schema.json describes, so the
normalisation can be tested against committed fixtures with no client, no
database and no network.

Amounts become decimal strings. Document AI reports money as integer units
plus nanos, so the exact value is available and there is never a reason to
let it become a float.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# Document AI Invoice Parser entity types mapped onto our field names.
_SCALAR_ENTITIES: dict[str, str] = {
    "invoice_id": "invoice_number",
    "purchase_order": "po_number",
    "invoice_date": "invoice_date",
    "due_date": "due_date",
    "supplier_name": "supplier_name",
    "supplier_tax_id": "supplier_tax_id",
    "net_amount": "net_amount",
    "total_tax_amount": "tax_amount",
    "total_amount": "total_amount",
}

_MONEY_FIELDS = frozenset({"net_amount", "tax_amount", "total_amount"})
_DATE_FIELDS = frozenset({"invoice_date", "due_date"})

_LINE_ITEM_PROPERTIES: dict[str, str] = {
    "line_item/description": "description",
    "line_item/quantity": "quantity",
    "line_item/unit_price": "unit_price",
    "line_item/amount": "line_total",
    "line_item/product_code": "sku",
}

_MONEY_LINE_FIELDS = frozenset({"unit_price", "line_total"})
_NANOS = Decimal(1_000_000_000)


def _page_number(entity: dict[str, Any]) -> int | None:
    """Document AI page refs are zero based; our column is one based."""
    refs = (entity.get("pageAnchor") or {}).get("pageRefs") or []
    if not refs:
        return None
    raw = refs[0].get("page")
    if raw is None:
        return 1
    try:
        return int(raw) + 1
    except (TypeError, ValueError):
        return None


def _confidence(entity: dict[str, Any]) -> float | None:
    raw = entity.get("confidence")
    if raw is None:
        return None
    try:
        return round(float(raw), 4)
    except (TypeError, ValueError):
        return None


def _money_string(entity: dict[str, Any]) -> str | None:
    """Exact decimal string for a monetary entity.

    Prefers the normalised money value, which is integer units and nanos and
    therefore exact. Falls back to the raw text only when Document AI did
    not normalise the field.
    """
    normalised = entity.get("normalizedValue") or {}
    money = normalised.get("moneyValue")
    if isinstance(money, dict):
        units = Decimal(str(money.get("units", "0") or "0"))
        nanos = Decimal(str(money.get("nanos", 0) or 0))
        return str((units + nanos / _NANOS).quantize(Decimal("0.0001")))
    return _decimal_from_text(normalised.get("text") or entity.get("mentionText"))


def _decimal_from_text(raw: Any, places: str = "0.0001") -> str | None:
    """Parse a loose numeric string into an exact decimal string."""
    if raw is None:
        return None
    cleaned = str(raw).strip().replace(",", "").replace(" ", "")
    for symbol in ("$", "£", "€", "¥", "₹", "RM", "SGD", "USD"):
        cleaned = cleaned.replace(symbol, "")
    cleaned = cleaned.strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    if not cleaned:
        return None
    try:
        return str(Decimal(cleaned).quantize(Decimal(places)))
    except ArithmeticError:
        return None


def _date_string(entity: dict[str, Any]) -> str | None:
    """ISO date from the normalised value, or None when it is unusable."""
    normalised = entity.get("normalizedValue") or {}
    value = normalised.get("dateValue")
    if isinstance(value, dict):
        year = value.get("year")
        month = value.get("month")
        day = value.get("day")
        if year and month and day:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return None
    text = normalised.get("text") or entity.get("mentionText")
    if not text:
        return None
    candidate = str(text).strip()
    # Only accept what is already unambiguous. Guessing between day first
    # and month first ordering would silently corrupt due dates.
    if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
        return candidate
    return None


def _text_value(entity: dict[str, Any]) -> str | None:
    normalised = entity.get("normalizedValue") or {}
    raw = normalised.get("text") or entity.get("mentionText")
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned or None


def _envelope(value: str | None, entity: dict[str, Any] | None) -> dict[str, Any]:
    """Build one field envelope.

    A present key with a null value means the extractor looked and found
    nothing. That is deliberately different from the key being absent, which
    means it never reported the field at all.
    """
    if entity is None:
        return {"value": None, "confidence": None, "page_number": None}
    return {
        "value": value,
        "confidence": _confidence(entity),
        "page_number": _page_number(entity),
    }


def _best(entities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the highest confidence mention of a repeated entity type."""
    if not entities:
        return None
    return max(entities, key=lambda item: _confidence(item) or 0.0)


def _currency_code(document_entities: list[dict[str, Any]]) -> dict[str, Any]:
    """Currency from an explicit entity, else from a normalised amount."""
    explicit = _best(
        [item for item in document_entities if item.get("type") == "currency"]
    )
    if explicit is not None:
        value = _text_value(explicit)
        code = value.upper()[:3] if value else None
        return _envelope(code if code and code.isalpha() else None, explicit)

    for entity in document_entities:
        if entity.get("type") not in _MONEY_FIELDS | {"total_amount"}:
            continue
        money = (entity.get("normalizedValue") or {}).get("moneyValue")
        if isinstance(money, dict) and money.get("currencyCode"):
            return _envelope(str(money["currencyCode"]).upper()[:3], entity)
    return {"value": None, "confidence": None, "page_number": None}


def _line_item(entity: dict[str, Any]) -> dict[str, Any]:
    """One invoice line from a line_item entity and its properties."""
    by_field: dict[str, dict[str, Any]] = {}
    for prop in entity.get("properties") or []:
        field = _LINE_ITEM_PROPERTIES.get(str(prop.get("type", "")))
        if field is None:
            continue
        # Keep the most confident mention if the parser repeats a property.
        existing = by_field.get(field)
        if existing is None or (_confidence(prop) or 0.0) > (
            _confidence(existing) or 0.0
        ):
            by_field[field] = prop

    line: dict[str, Any] = {}
    # Optional, so absent unless reported. The four spec 01 properties are
    # required and always present, carrying a null when not reported.
    sku = by_field.get("sku")
    if sku is not None:
        line["sku"] = _envelope(_text_value(sku), sku)
    for field in ("description", "quantity", "unit_price", "line_total"):
        prop = by_field.get(field)
        if prop is None:
            line[field] = {"value": None, "confidence": None, "page_number": None}
            continue
        if field in _MONEY_LINE_FIELDS:
            line[field] = _envelope(_money_string(prop), prop)
        elif field == "quantity":
            line[field] = _envelope(
                _decimal_from_text(
                    (prop.get("normalizedValue") or {}).get("text")
                    or prop.get("mentionText"),
                    places="0.000001",
                ),
                prop,
            )
        else:
            line[field] = _envelope(_text_value(prop), prop)
    return line


def normalise_document(document: dict[str, Any]) -> dict[str, Any]:
    """Turn a raw Document AI response into a schema conformant payload."""
    entities: list[dict[str, Any]] = list(document.get("entities") or [])
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in entities:
        by_type.setdefault(str(item.get("type", "")), []).append(item)

    payload: dict[str, Any] = {}
    for entity_type, field in _SCALAR_ENTITIES.items():
        entity: dict[str, Any] | None = _best(by_type.get(entity_type, []))
        if entity is None:
            # Required fields must still be present, carrying an explicit
            # null. Optional ones stay absent, which is a different fact.
            if field in {"invoice_number", "total_amount"}:
                payload[field] = _envelope(None, None)
            continue
        if field in _MONEY_FIELDS:
            payload[field] = _envelope(_money_string(entity), entity)
        elif field in _DATE_FIELDS:
            payload[field] = _envelope(_date_string(entity), entity)
        else:
            payload[field] = _envelope(_text_value(entity), entity)

    payload["currency"] = _currency_code(entities)
    payload["line_items"] = [
        _line_item(entity) for entity in by_type.get("line_item", [])
    ]

    pages = document.get("pages") or []
    payload["page_count"] = len(pages) or None
    return payload


def trim_raw_payload(document: dict[str, Any]) -> dict[str, Any]:
    """Shrink a raw response to what is worth keeping in Postgres.

    Accepted change 6. The full response carries the OCR text layer and per
    token geometry, which is megabytes for a scanned multi page invoice.
    Entities and page dimensions are what anyone actually reads back, and
    the untrimmed response stays one object fetch away in Cloud Storage.
    """
    trimmed_entities: list[dict[str, Any]] = []
    for entity in document.get("entities") or []:
        item: dict[str, Any] = {
            "type": entity.get("type"),
            "mentionText": entity.get("mentionText"),
            "confidence": entity.get("confidence"),
            "normalizedValue": entity.get("normalizedValue"),
            "pageAnchor": entity.get("pageAnchor"),
        }
        properties = entity.get("properties")
        if properties:
            item["properties"] = [
                {
                    "type": prop.get("type"),
                    "mentionText": prop.get("mentionText"),
                    "confidence": prop.get("confidence"),
                    "normalizedValue": prop.get("normalizedValue"),
                    "pageAnchor": prop.get("pageAnchor"),
                }
                for prop in properties
            ]
        trimmed_entities.append(item)

    return {
        "mimeType": document.get("mimeType"),
        "pageCount": len(document.get("pages") or []),
        "entities": trimmed_entities,
    }
