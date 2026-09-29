"""Compare tax IDs in a normalised form.

Migration 0002 made suppliers.tax_id unique as a raw string, so
"200812345K" and "200812345-K" could belong to two suppliers. Supplier
resolution stops at the first tax ID hit, which is only safe if one
normalised tax ID names one supplier.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Uppercase, letters and digits only. Separators and spacing vary
    # between documents, and OCR adds its own.
    op.execute(
        """
        create function normalise_tax_id(tax_id text)
        returns text
        language sql
        immutable
        parallel safe
        return nullif(upper(regexp_replace(tax_id, '[^[:alnum:]]+', '', 'g')), '')
        """
    )
    op.execute("drop index suppliers_tax_id_uidx")
    op.execute(
        "create unique index suppliers_tax_id_uidx on suppliers "
        "(normalise_tax_id(tax_id)) where tax_id is not null"
    )


def downgrade() -> None:
    op.execute("drop index suppliers_tax_id_uidx")
    op.execute(
        "create unique index suppliers_tax_id_uidx on suppliers (tax_id) "
        "where tax_id is not null"
    )
    op.execute("drop function normalise_tax_id(text)")
