"""Supplier resolution: spec 02 section 4 with README challenge 6.4.

Every refusal here is a case where picking a supplier could pay the wrong
bank account. Names and tax IDs are synthetic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.matching.rules import MatchingRules
from src.matching.suppliers import SupplierResolution, resolve_supplier
from tests.conftest import requires_database

pytestmark = requires_database

SUPPLIERS: list[dict[str, Any]] = [
    {
        "supplier_id": "sup-acme",
        "legal_name": "Acme Office Supplies Pte Ltd",
        "trading_names": ["Acme Stationery"],
        "tax_id": "200812345K",
        "is_active": True,
    },
    {
        "supplier_id": "sup-harbour",
        "legal_name": "Harbourpoint IT Services",
        "trading_names": [],
        "tax_id": "201234567W",
        "is_active": True,
    },
    {
        "supplier_id": "sup-northgate",
        "legal_name": "Northgate Consulting Ltd",
        "trading_names": [],
        "tax_id": None,
        "is_active": True,
    },
    {
        "supplier_id": "sup-old",
        "legal_name": "Westmoor Timber Pte Ltd",
        "trading_names": [],
        "tax_id": "199911111X",
        "is_active": False,
    },
    # Two suppliers sharing a trading name.
    {
        "supplier_id": "sup-quay-a",
        "legal_name": "Quayside Catering East Pte Ltd",
        "trading_names": ["Quayside Kitchen"],
        "tax_id": None,
        "is_active": True,
    },
    {
        "supplier_id": "sup-quay-b",
        "legal_name": "Quayside Catering West Pte Ltd",
        "trading_names": ["Quayside Kitchen"],
        "tax_id": None,
        "is_active": True,
    },
]


@pytest.fixture
async def seeded(engine: AsyncEngine) -> AsyncEngine:
    async with engine.begin() as conn:
        for supplier in SUPPLIERS:
            await conn.execute(
                text(
                    "insert into suppliers (supplier_id, legal_name, trading_names, "
                    "tax_id, currency, is_active) "
                    "values (:supplier_id, :legal_name, :trading_names, :tax_id, "
                    "'SGD', :is_active)"
                ),
                supplier,
            )
    return engine


async def _resolve(
    engine: AsyncEngine,
    rules: MatchingRules,
    *,
    name: str | None,
    tax_id: str | None = None,
) -> SupplierResolution:
    async with engine.begin() as conn:
        return await resolve_supplier(conn, name=name, tax_id=tax_id, rules=rules)


@pytest.fixture
def rules(deps: Any) -> MatchingRules:
    resolved: MatchingRules = deps.rules
    return resolved


# ---------------------------------------------------------------- tier 1


async def test_tax_id_with_a_corroborating_name_resolves(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(
        seeded, rules, name="ACME OFFICE SUPPLES", tax_id="200812345K"
    )
    assert (result.supplier_id, result.method) == ("sup-acme", "TAX_ID")
    assert result.score is not None
    assert result.score >= rules.supplier_resolution.tax_id_name_floor


async def test_tax_id_matches_whatever_its_punctuation(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(
        seeded, rules, name="Acme Office Supplies", tax_id=" 2008-12345 k"
    )
    assert result.supplier_id == "sup-acme"


async def test_tax_id_corroborated_by_a_trading_name(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(seeded, rules, name="Acme Stationery", tax_id="200812345K")
    assert (result.supplier_id, result.method) == ("sup-acme", "TAX_ID")


async def test_misread_tax_id_landing_on_another_supplier_is_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """6.4. The tax ID says Acme; the name is plainly someone else."""
    result = await _resolve(
        seeded, rules, name="Ironbridge Hardware Supply", tax_id="200812345K"
    )
    assert not result.resolved
    assert result.method is None
    assert result.reason is not None
    assert "corroboration floor" in result.reason


async def test_tax_id_without_any_name_is_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(seeded, rules, name=None, tax_id="200812345K")
    assert not result.resolved


async def test_a_refused_tax_id_does_not_fall_through_to_the_name(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """The name alone would resolve Harbourpoint; the tax ID says Acme."""
    result = await _resolve(
        seeded, rules, name="Harbourpoint IT Services", tax_id="200812345K"
    )
    assert not result.resolved


async def test_tax_id_of_an_inactive_supplier_is_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(seeded, rules, name="Westmoor Timber", tax_id="199911111X")
    assert not result.resolved
    assert result.reason is not None
    assert "inactive" in result.reason


# ---------------------------------------------------------------- tier 2


@pytest.mark.parametrize(
    "name",
    ["Harbourpoint IT Services", "HARBOURPOINT I.T. SERVICES PTE. LTD."],
)
async def test_exact_normalised_name_resolves(
    seeded: AsyncEngine, rules: MatchingRules, name: str
) -> None:
    result = await _resolve(seeded, rules, name=name)
    assert (result.supplier_id, result.method) == ("sup-harbour", "NAME_EXACT")
    assert result.score == Decimal("1.0000")


async def test_exact_trading_name_resolves(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(seeded, rules, name="acme stationery")
    assert (result.supplier_id, result.method) == ("sup-acme", "NAME_EXACT")


async def test_a_name_shared_by_two_suppliers_is_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(seeded, rules, name="Quayside Kitchen")
    assert not result.resolved
    assert result.reason is not None
    assert "sup-quay-a" in result.reason
    assert "sup-quay-b" in result.reason


async def test_name_match_with_a_conflicting_tax_id_is_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """The invoice's tax ID belongs to nobody, and Harbourpoint has another."""
    result = await _resolve(
        seeded, rules, name="Harbourpoint IT Services", tax_id="209999999Z"
    )
    assert not result.resolved
    assert result.reason is not None
    assert "differs from its registered" in result.reason


async def test_unknown_tax_id_does_not_block_a_supplier_without_one(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """Northgate has no tax ID on file, so there is nothing to conflict with."""
    result = await _resolve(
        seeded, rules, name="Northgate Consulting", tax_id="GB443221190"
    )
    assert result.supplier_id == "sup-northgate"


async def test_name_of_an_inactive_supplier_is_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(seeded, rules, name="Westmoor Timber Pte Ltd")
    assert not result.resolved


# ---------------------------------------------------------------- tier 3


async def test_close_name_above_the_threshold_resolves(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(seeded, rules, name="Harbour Point IT Services")
    assert (result.supplier_id, result.method) == ("sup-harbour", "NAME_TRIGRAM")
    assert result.score is not None
    assert result.score >= rules.supplier_resolution.trigram_threshold


async def test_close_name_below_the_threshold_is_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """A one letter misread scores 0.78 against a threshold of 0.82."""
    result = await _resolve(seeded, rules, name="ACME OFFICE SUPPLES")
    assert not result.resolved
    assert result.reason is not None
    assert "closest supplier sup-acme" in result.reason
    assert "below the threshold" in result.reason


async def test_a_distant_name_is_not_even_a_candidate(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """Below threshold minus margin, a name cannot affect the outcome, so the
    trigram prefilter drops it and no closest supplier is reported."""
    result = await _resolve(seeded, rules, name="Northgate Consultancy")
    assert not result.resolved
    assert result.reason == "no supplier matches the tax ID or the name"


async def test_two_close_candidates_within_the_margin_are_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """Scores 0.85 against both East and West: above the threshold, but a tie."""
    result = await _resolve(seeded, rules, name="Quayside Catering East West")
    assert not result.resolved
    assert result.reason is not None
    assert "ambiguity margin" in result.reason


async def test_no_name_and_no_tax_id_is_refused(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    result = await _resolve(seeded, rules, name=None)
    assert not result.resolved
    assert result.reason == "no supplier matches the tax ID or the name"


# ---------------------------------------------------------------- rules


async def test_the_threshold_comes_from_the_rules_file(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """Spec 02 done 6: a rules change alters the outcome, no code change."""
    name = "Harbour Point IT Services"
    accepted = await _resolve(seeded, rules, name=name)
    assert accepted.resolved
    assert accepted.score is not None

    stricter = rules.model_copy(
        update={
            "supplier_resolution": rules.supplier_resolution.model_copy(
                update={"trigram_threshold": accepted.score + Decimal("0.0001")}
            )
        }
    )
    refused = await _resolve(seeded, stricter, name=name)
    assert not refused.resolved


async def test_a_score_exactly_at_the_threshold_passes(
    seeded: AsyncEngine, rules: MatchingRules
) -> None:
    """At the threshold is a pass. A real similarity must not round it away."""
    name = "Harbour Point IT Services"
    first = await _resolve(seeded, rules, name=name)
    assert first.score is not None
    at_threshold = rules.model_copy(
        update={
            "supplier_resolution": rules.supplier_resolution.model_copy(
                update={"trigram_threshold": first.score}
            )
        }
    )
    again = await _resolve(seeded, at_threshold, name=name)
    assert again.supplier_id == "sup-harbour"
