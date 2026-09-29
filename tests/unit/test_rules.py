"""config/rules.yaml: loading, validation and lookups.

Spec 02 section 9: an invalid rules file fails at boot, never at match
time. Every rejection here is one that would otherwise surface as a wrong
match decision.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.common.config import Settings
from src.common.errors import ConfigError
from src.matching.rules import ExceptionType, MatchingRules, load_rules, parse_rules

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = REPO_ROOT / "config" / "rules.yaml"


@pytest.fixture
def rules() -> MatchingRules:
    return load_rules(RULES_FILE)


def _document(**changes: Any) -> str:
    """The committed rules with some top level sections replaced."""
    raw = yaml.safe_load(RULES_FILE.read_text("utf-8"))
    raw.update(changes)
    return yaml.safe_dump(raw)


def _walk(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _walk(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _walk(item)]
    return [value]


def test_the_committed_rules_file_loads(rules: MatchingRules) -> None:
    assert rules.version == 1


def test_the_default_setting_points_at_the_committed_file() -> None:
    assert Settings().rules_path == RULES_FILE


def test_numbers_are_read_as_exact_decimals(rules: MatchingRules) -> None:
    """Rule 5.7. A float 0.02 is not 0.02."""
    assert rules.tolerances.price_pct == Decimal("0.02")
    assert rules.tolerance("line_total_abs", "SGD") == Decimal("0.01")
    leaves = _walk(rules.model_dump())
    assert not [leaf for leaf in leaves if isinstance(leaf, float)]


@pytest.mark.parametrize(
    ("invoice_date", "expected"),
    [
        (date(2022, 12, 31), None),
        (date(2023, 1, 1), Decimal("0.08")),
        (date(2023, 12, 31), Decimal("0.08")),
        (date(2024, 1, 1), Decimal("0.09")),
        (date(2026, 9, 29), Decimal("0.09")),
    ],
)
def test_tax_rate_is_the_one_in_effect_on_the_invoice_date(
    rules: MatchingRules, invoice_date: date, expected: Decimal | None
) -> None:
    assert rules.tax_rate_on(invoice_date) == expected


def test_rates_may_be_listed_in_any_order() -> None:
    document = _document(
        tax={
            "rates": [
                {"code": "SG_GST", "rate": "0.08", "effective_from": "2023-01-01"},
                {"code": "SG_GST", "rate": "0.09", "effective_from": "2024-01-01"},
            ],
            "tax_id_pattern": None,
        }
    )
    assert parse_rules(document).tax_rate_on(date(2025, 1, 1)) == Decimal("0.09")


def test_currency_override_replaces_only_what_it_names() -> None:
    raw = yaml.safe_load(RULES_FILE.read_text("utf-8"))
    raw["tolerances"]["currency_overrides"] = {"JPY": {"price_abs": 50}}
    rules = parse_rules(yaml.safe_dump(raw))
    assert rules.tolerance("price_abs", "JPY") == Decimal(50)
    assert rules.tolerance("tax_abs", "JPY") == Decimal("0.01")
    assert rules.tolerance("price_abs", "SGD") == Decimal("0.50")
    assert rules.tolerance("price_abs", None) == Decimal("0.50")


def test_header_fields_without_an_entry_use_the_default(
    rules: MatchingRules,
) -> None:
    assert rules.confidence_threshold("total_amount") == Decimal("0.98")
    assert rules.confidence_threshold("due_date") == Decimal("0.85")


def test_line_item_fields_have_no_threshold(rules: MatchingRules) -> None:
    """Decided 2026-09-26: confidence applies to header fields only."""
    with pytest.raises(ConfigError):
        rules.confidence_threshold("line_items/0/quantity")


def test_severity_defaults_follow_the_taxonomy(rules: MatchingRules) -> None:
    assert rules.severity(ExceptionType.OVER_ORDER) == "BLOCK"
    assert rules.severity(ExceptionType.DATE_INVALID) == "WARN"


def test_severity_override_wins() -> None:
    raw = yaml.safe_load(RULES_FILE.read_text("utf-8"))
    raw["severity_overrides"] = {"DATE_INVALID": "BLOCK"}
    rules = parse_rules(yaml.safe_dump(raw))
    assert rules.severity(ExceptionType.DATE_INVALID) == "BLOCK"


def _mutated(path: list[str], value: Any) -> str:
    raw = yaml.safe_load(RULES_FILE.read_text("utf-8"))
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return yaml.safe_dump(raw)


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (["version"], 2, "unknown version"),
        (["tolerances", "price_pct"], -0.01, "negative tolerance"),
        (["tolerances", "price_tolerance"], 0.02, "misspelt key"),
        (["confidence", "default"], 1.5, "confidence above one"),
        (["confidence", "fields"], {"line_items": 0.9}, "line item threshold"),
        (
            ["tolerances", "currency_overrides"],
            {"jpy": {"price_abs": 50}},
            "lower case currency",
        ),
        (
            ["tolerances", "currency_overrides"],
            {"JPY": {"price_pct": 0.05}},
            "percentage override",
        ),
        (["tax", "tax_id_pattern"], "([A-Z", "pattern that does not compile"),
        (
            ["tax", "rates"],
            [
                {"code": "SG_GST", "rate": 0.09, "effective_from": "2024-01-01"},
                {"code": "SG_GST", "rate": 0.10, "effective_from": "2024-01-01"},
            ],
            "two rates on one date",
        ),
        (["tax", "rates"], [], "no rates"),
        (["severity_overrides"], {"PRICE_VARIANCES": "WARN"}, "unknown type"),
        (["severity_overrides"], {"PRICE_VARIANCE": "INFO"}, "unknown severity"),
        (["supplier_resolution", "ambiguity_margin"], 0.9, "margin above threshold"),
        (["duplicates", "date_window_days"], 0, "zero day window"),
    ],
)
def test_invalid_rules_fail_at_load(path: list[str], value: Any, reason: str) -> None:
    with pytest.raises(ConfigError, match="Rules file is invalid"):
        parse_rules(_mutated(path, value))


def test_a_missing_file_fails_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be read"):
        load_rules(tmp_path / "absent.yaml")


@pytest.mark.parametrize("document", ["tolerances: [unclosed", "- a list"])
def test_a_malformed_file_fails_at_load(document: str) -> None:
    with pytest.raises(ConfigError):
        parse_rules(document)
