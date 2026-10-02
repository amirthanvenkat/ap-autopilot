"""Supplier resolution.

The decision is made in SQL (queries/resolve_supplier.sql). This module
supplies its parameters and returns a typed result.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.matching.rules import MatchingRules
from src.matching.sql import query

# One step of the stored score precision. The trigram prefilter compares a
# real, so it is set this far below the lowest score that matters to keep
# a candidate that rounds up to that score.
_SCORE_STEP = Decimal("0.0001")


@dataclass(frozen=True)
class SupplierResolution:
    """Outcome of resolving one invoice's supplier.

    Either supplier_id and method are set, or reason says why no supplier
    could be chosen safely. The reason becomes the SUPPLIER_UNRESOLVED
    detail a reviewer reads.
    """

    supplier_id: str | None
    method: str | None
    score: Decimal | None
    reason: str | None

    @property
    def resolved(self) -> bool:
        return self.supplier_id is not None


async def resolve_supplier(
    conn: AsyncConnection,
    *,
    name: str | None,
    tax_id: str | None,
    rules: MatchingRules,
) -> SupplierResolution:
    """Resolve the supplier an extraction names.

    Must run inside a transaction. The trigram threshold is set with
    set_config(..., true), which lasts only for the transaction and so is
    safe behind Neon's transaction pooler.
    """
    settings = rules.supplier_resolution
    await conn.execute(
        text("select set_config('pg_trgm.similarity_threshold', :limit, true)"),
        {"limit": str(settings.candidate_floor - _SCORE_STEP)},
    )
    row = (
        await conn.execute(
            query("resolve_supplier"),
            {
                "tax_id": tax_id,
                "name": name,
                "trigram_threshold": settings.trigram_threshold,
                "ambiguity_margin": settings.ambiguity_margin,
                "tax_id_name_floor": settings.tax_id_name_floor,
            },
        )
    ).one()
    return SupplierResolution(
        supplier_id=row.supplier_id,
        method=row.method,
        score=row.score,
        reason=row.reason,
    )
