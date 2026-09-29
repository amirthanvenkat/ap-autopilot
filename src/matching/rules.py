"""Matching rules: load, validate and query config/rules.yaml.

Loaded once at startup. An invalid file raises ConfigError there, never at
match time (spec 02 section 9).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src.common.errors import ConfigError
from src.common.logging import get_logger

log = get_logger(__name__)


class ExceptionType(StrEnum):
    """Spec 02 section 7, plus OVER_ORDER (decided 2026-09-26)."""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    SUPPLIER_UNRESOLVED = "SUPPLIER_UNRESOLVED"
    NO_PO_REFERENCE = "NO_PO_REFERENCE"
    PO_NOT_FOUND = "PO_NOT_FOUND"
    NO_PO_MATCH = "NO_PO_MATCH"
    NO_GOODS_RECEIPT = "NO_GOODS_RECEIPT"
    OVER_RECEIPT = "OVER_RECEIPT"
    OVER_ORDER = "OVER_ORDER"
    PRICE_VARIANCE = "PRICE_VARIANCE"
    QUANTITY_VARIANCE = "QUANTITY_VARIANCE"
    LINE_TOTAL_MISMATCH = "LINE_TOTAL_MISMATCH"
    HEADER_TOTAL_MISMATCH = "HEADER_TOTAL_MISMATCH"
    TAX_MISMATCH = "TAX_MISMATCH"
    TAX_ID_INVALID = "TAX_ID_INVALID"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    DATE_INVALID = "DATE_INVALID"
    DUPLICATE_EXACT = "DUPLICATE_EXACT"
    DUPLICATE_SUSPECTED = "DUPLICATE_SUSPECTED"


Severity = Literal["BLOCK", "WARN"]

_WARN_BY_DEFAULT = frozenset(
    {
        ExceptionType.QUANTITY_VARIANCE,
        ExceptionType.TAX_ID_INVALID,
        ExceptionType.DATE_INVALID,
        ExceptionType.DUPLICATE_SUSPECTED,
    }
)

# The scalar fields of the extraction schema. Confidence thresholds apply to
# these only (decided 2026-09-26).
HEADER_FIELDS = frozenset(
    {
        "supplier_name",
        "supplier_tax_id",
        "invoice_number",
        "po_number",
        "invoice_date",
        "due_date",
        "currency",
        "net_amount",
        "tax_amount",
        "total_amount",
    }
)

Probability = Annotated[Decimal, Field(ge=0, le=1)]
NonNegative = Annotated[Decimal, Field(ge=0)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfidenceRules(_Strict):
    default: Probability
    fields: dict[str, Probability] = Field(default_factory=dict)

    @field_validator("fields")
    @classmethod
    def _header_fields_only(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        unknown = sorted(set(value) - HEADER_FIELDS)
        if unknown:
            raise ValueError(f"not header fields: {', '.join(unknown)}")
        return value


class AbsoluteTolerances(_Strict):
    """The currency dependent tolerances. Every key is optional."""

    price_abs: NonNegative | None = None
    line_total_abs: NonNegative | None = None
    header_total_abs: NonNegative | None = None
    tax_abs: NonNegative | None = None


AbsoluteToleranceName = Literal[
    "price_abs", "line_total_abs", "header_total_abs", "tax_abs"
]


class Tolerances(_Strict):
    price_pct: NonNegative
    price_abs: NonNegative
    quantity_pct: NonNegative
    line_total_abs: NonNegative
    header_total_abs: NonNegative
    tax_abs: NonNegative
    currency_overrides: dict[CurrencyCode, AbsoluteTolerances] = Field(
        default_factory=dict
    )


class SupplierResolutionRules(_Strict):
    trigram_threshold: Probability
    ambiguity_margin: Probability
    # README challenge 6.4: a tax ID hit counts only when the extracted name
    # scores at least this against the supplier's names. A floor, not a
    # match: it rejects a misread tax ID that lands on an unrelated
    # supplier, and tolerates heavy OCR damage to the right name.
    tax_id_name_floor: Probability

    @model_validator(mode="after")
    def _ordered(self) -> SupplierResolutionRules:
        if self.ambiguity_margin >= self.trigram_threshold:
            raise ValueError("ambiguity_margin must be below trigram_threshold")
        if self.tax_id_name_floor > self.trigram_threshold:
            raise ValueError("tax_id_name_floor must not exceed trigram_threshold")
        return self

    @property
    def candidate_floor(self) -> Decimal:
        """The lowest score that can still make a trigram match ambiguous.

        A runner-up matters only when it is within the margin of a best
        score that cleared the threshold.
        """
        return self.trigram_threshold - self.ambiguity_margin


class DuplicateRules(_Strict):
    amount_tolerance_abs: NonNegative
    date_window_days: Annotated[int, Field(gt=0)]


class TaxRate(_Strict):
    code: str
    rate: Probability
    effective_from: date


class TaxRules(_Strict):
    rates: list[TaxRate] = Field(min_length=1)
    tax_id_pattern: str | None

    @field_validator("rates")
    @classmethod
    def _one_rate_per_date(cls, value: list[TaxRate]) -> list[TaxRate]:
        dates = [rate.effective_from for rate in value]
        repeated = sorted({d for d in dates if dates.count(d) > 1})
        if repeated:
            listed = ", ".join(d.isoformat() for d in repeated)
            raise ValueError(f"more than one rate effective from {listed}")
        return sorted(value, key=lambda rate: rate.effective_from)

    @field_validator("tax_id_pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"tax_id_pattern does not compile: {exc}") from exc
        return value


class MatchingRules(_Strict):
    """The validated contents of config/rules.yaml."""

    version: Literal[1]
    confidence: ConfidenceRules
    tolerances: Tolerances
    supplier_resolution: SupplierResolutionRules
    duplicates: DuplicateRules
    tax: TaxRules
    severity_overrides: dict[ExceptionType, Severity] = Field(default_factory=dict)

    def confidence_threshold(self, field: str) -> Decimal:
        """The threshold for one header field."""
        if field not in HEADER_FIELDS:
            raise ConfigError("No confidence threshold", detail=field)
        return self.confidence.fields.get(field, self.confidence.default)

    def tolerance(self, name: AbsoluteToleranceName, currency: str | None) -> Decimal:
        """An absolute tolerance, with any override for the currency applied."""
        override = self.tolerances.currency_overrides.get(currency or "")
        if override is not None:
            value: Decimal | None = getattr(override, name)
            if value is not None:
                return value
        default: Decimal = getattr(self.tolerances, name)
        return default

    def tax_rate_on(self, invoice_date: date) -> Decimal | None:
        """The rate in effect on the invoice date.

        None when the date precedes every configured rate, in which case no
        rate reconciles and the caller decides what that means.
        """
        current: Decimal | None = None
        for rate in self.tax.rates:
            if rate.effective_from > invoice_date:
                break
            current = rate.rate
        return current

    def severity(self, exception_type: ExceptionType) -> Severity:
        """Configured severity, falling back to the spec 02 taxonomy."""
        override = self.severity_overrides.get(exception_type)
        if override is not None:
            return override
        return "WARN" if exception_type in _WARN_BY_DEFAULT else "BLOCK"


class _DecimalLoader(yaml.SafeLoader):
    """Reads YAML floats as Decimal, so no tolerance passes through a float."""


def _decimal(loader: yaml.SafeLoader, node: yaml.Node) -> Decimal:
    return Decimal(str(loader.construct_scalar(node)))  # type: ignore[arg-type]


_DecimalLoader.add_constructor("tag:yaml.org,2002:float", _decimal)


def parse_rules(document: str, *, source: str = "rules.yaml") -> MatchingRules:
    """Validate rules from YAML text."""
    try:
        raw: Any = yaml.load(document, Loader=_DecimalLoader)
    except yaml.YAMLError as exc:
        raise ConfigError("Rules file is not valid YAML", detail=source) from exc
    if not isinstance(raw, dict):
        raise ConfigError("Rules file must be a mapping", detail=source)
    try:
        rules = MatchingRules.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError("Rules file is invalid", detail=problems) from exc
    if rules.tax.tax_id_pattern is None:
        log.warning("rules.tax_id_check_disabled", source=source)
    return rules


def load_rules(path: Path) -> MatchingRules:
    """Read and validate the rules file. Called once at startup."""
    try:
        document = path.read_text("utf-8")
    except OSError as exc:
        raise ConfigError("Rules file cannot be read", detail=str(path)) from exc
    return parse_rules(document, source=str(path))
