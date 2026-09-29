"""Record the purchase order number an invoice quotes.

Spec 02 resolves the purchase order from it. Spec 01 never extracted it,
so without this column every invoice would raise NO_PO_REFERENCE.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table extraction_results add column po_number text")


def downgrade() -> None:
    op.execute("alter table extraction_results drop column po_number")
