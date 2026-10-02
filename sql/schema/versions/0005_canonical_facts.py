"""Record why canonicalisation resolved nothing, and look POs up loosely.

The match step derives every exception from the invoice's facts on every
run, so the reason a supplier or PO was not resolved must be stored with
the invoice. It is the detail of SUPPLIER_UNRESOLVED or PO_NOT_FOUND.

PO numbers are compared ignoring case and punctuation, so "PO 2026-0101"
on an invoice finds "PO-2026-0101". The unique index makes that lookup
return at most one PO.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create function normalise_reference(reference text)
        returns text
        language sql
        immutable
        parallel safe
        return nullif(upper(regexp_replace(reference, '[^[:alnum:]]+', '', 'g')), '')
        """
    )
    op.execute(
        "create unique index purchase_orders_po_number_norm_uidx "
        "on purchase_orders (normalise_reference(po_number))"
    )
    op.execute(
        """
        alter table invoices
            add column supplier_resolution_note text,
            add column po_resolution_note text,
            add constraint invoices_unresolved_supplier_explained
                check (supplier_id is not null
                       or supplier_resolution_note is not null),
            add constraint invoices_unresolved_po_explained
                check (po_id is not null
                       or po_number is null
                       or po_resolution_note is not null)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        alter table invoices
            drop constraint invoices_unresolved_po_explained,
            drop constraint invoices_unresolved_supplier_explained,
            drop column po_resolution_note,
            drop column supplier_resolution_note
        """
    )
    op.execute("drop index purchase_orders_po_number_norm_uidx")
    op.execute("drop function normalise_reference(text)")
