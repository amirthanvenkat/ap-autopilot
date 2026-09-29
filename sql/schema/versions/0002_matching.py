"""Matching engine schema.

Spec 02 section 3, plus the additions recorded in README section 5.3.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("create extension if not exists pg_trgm")

    # One definition of a normalised supplier name, used by the indexes and
    # by the resolver, so the two cannot drift apart. Punctuation becomes a
    # space, whitespace collapses, and a trailing run of entity suffixes is
    # removed, so "Acme Pte. Ltd." and "ACME PTE LTD" both become "acme".
    # Dotted initials are joined first, so "S.A.S." is the suffix "sas"
    # rather than three stray letters. [:alnum:] rather than a-z keeps
    # accented letters.
    op.execute(
        r"""
        create function normalise_supplier_name(name text)
        returns text
        language sql
        immutable
        parallel safe
        return nullif(
            regexp_replace(
                btrim(regexp_replace(
                    regexp_replace(
                        regexp_replace(lower(name), '\m([[:alnum:]])\.', '\1', 'g'),
                        '[^[:alnum:]]+', ' ', 'g'
                    ),
                    '\s+', ' ', 'g'
                )),
                '(\s+(pte|private|limited|ltd|llp|llc|inc|corp|co|sarl|sas|sa|gmbh))+$',
                ''
            ),
            ''
        )
        """
    )
    op.execute(
        """
        create function normalise_supplier_names(names text[])
        returns text[]
        language sql
        immutable
        parallel safe
        return array(
            select normalise_supplier_name(n) from unnest(names) as n
        )
        """
    )

    op.execute(
        """
        create table suppliers (
            supplier_id        text primary key,
            legal_name         text not null,
            trading_names      text[] not null default '{}',
            tax_id             text,
            currency           char(3) not null,
            payment_terms_days int not null default 30,
            is_active          boolean not null default true,
            created_at         timestamptz not null default now()
        )
        """
    )
    # Tier 1 of supplier resolution stops at the first tax ID hit, which is
    # only safe if a tax ID names one supplier.
    op.execute(
        "create unique index suppliers_tax_id_uidx on suppliers (tax_id) "
        "where tax_id is not null"
    )
    # Tier 2, exact normalised name.
    op.execute(
        "create index suppliers_legal_name_norm_idx "
        "on suppliers (normalise_supplier_name(legal_name))"
    )
    op.execute(
        "create index suppliers_trading_names_norm_idx on suppliers "
        "using gin (normalise_supplier_names(trading_names))"
    )
    # Tier 3, trigram similarity. Legal names only: pg_trgm cannot index the
    # elements of an array, so trading names are scanned. At the supplier
    # counts this project runs, that scan is negligible.
    op.execute(
        "create index suppliers_legal_name_trgm_idx on suppliers "
        "using gin (normalise_supplier_name(legal_name) gin_trgm_ops)"
    )

    op.execute(
        """
        create table purchase_orders (
            po_id            text primary key,
            po_number        text not null unique,
            supplier_id      text not null references suppliers (supplier_id),
            currency         char(3) not null,
            po_date          date not null,
            status           text not null
                             check (status in ('OPEN','CLOSED','CANCELLED')),
            created_at       timestamptz not null default now()
        )
        """
    )

    # sku holds the supplier's part number, because that is what appears on
    # the invoice. A buyer's internal item code would never match it.
    #
    # match_type: a TWO_WAY line, typically a service, has no goods receipt.
    # It skips the receipt checks and keeps the price and ordered quantity
    # checks.
    op.execute(
        """
        create table po_lines (
            po_line_id       text primary key,
            po_id            text not null references purchase_orders (po_id),
            line_number      int not null,
            sku              text,
            description      text not null,
            quantity_ordered numeric(18,4) not null check (quantity_ordered > 0),
            unit_price       numeric(18,4) not null check (unit_price >= 0),
            unit_of_measure  text not null default 'EA',
            match_type       text not null default 'THREE_WAY'
                             check (match_type in ('THREE_WAY','TWO_WAY')),
            created_at       timestamptz not null default now()
        )
        """
    )
    op.execute(
        "create unique index po_lines_po_line_uidx on po_lines (po_id, line_number)"
    )

    op.execute(
        """
        create table goods_receipts (
            gr_id            text primary key,
            gr_number        text not null unique,
            po_id            text not null references purchase_orders (po_id),
            received_date    date not null,
            created_at       timestamptz not null default now()
        )
        """
    )
    op.execute(
        """
        create table goods_receipt_lines (
            gr_line_id        text primary key,
            gr_id             text not null references goods_receipts (gr_id),
            po_line_id        text not null references po_lines (po_line_id),
            quantity_received numeric(18,4) not null,
            created_at        timestamptz not null default now()
        )
        """
    )
    op.execute("create index gr_lines_po_line_idx on goods_receipt_lines (po_line_id)")

    # invoice_number is nullable, unlike the spec: extraction can report
    # none, and the canonical insert must still succeed so LOW_CONFIDENCE
    # has an invoice to attach to.
    op.execute(
        """
        create table invoices (
            invoice_id        text primary key,
            extraction_id     text not null
                              references extraction_results (extraction_id),
            supplier_id       text references suppliers (supplier_id),
            supplier_resolution_method text
                              check (supplier_resolution_method in
                                    ('TAX_ID','NAME_EXACT','NAME_TRIGRAM')),
            supplier_resolution_score  numeric(5,4),
            po_number         text,
            po_id             text references purchase_orders (po_id),
            invoice_number    text,
            invoice_date      date,
            due_date          date,
            currency          char(3),
            net_amount        numeric(18,4),
            tax_amount        numeric(18,4),
            total_amount      numeric(18,4),
            match_status      text not null default 'PENDING'
                              check (match_status in
                                    ('PENDING','MATCHED','EXCEPTION',
                                     'REJECTED','POSTED')),
            -- Set on the first match and never rewritten. Prior billing is
            -- counted in this order, which is what keeps a re-run stable.
            matched_at        timestamptz,
            duplicate_of_invoice_id text references invoices (invoice_id),
            created_at        timestamptz not null default now(),
            constraint invoices_not_own_duplicate
                check (duplicate_of_invoice_id <> invoice_id),
            constraint invoices_resolution_complete
                check ((supplier_id is null) = (supplier_resolution_method is null))
        )
        """
    )
    op.execute(
        "create unique index invoices_extraction_uidx on invoices (extraction_id)"
    )
    # The duplicate control. A supplier cannot invoice the same number twice.
    # A row already recorded as a duplicate is excluded, so it can be stored
    # and carry its DUPLICATE_EXACT exception.
    op.execute(
        """
        create unique index invoices_supplier_number_uidx
            on invoices (supplier_id, invoice_number)
         where supplier_id is not null
           and duplicate_of_invoice_id is null
        """
    )
    op.execute(
        "create index invoices_supplier_date_idx "
        "on invoices (supplier_id, invoice_date)"
    )
    op.execute("create index invoices_po_idx on invoices (po_id)")

    op.execute(
        """
        create table invoice_lines (
            invoice_line_id  text primary key,
            invoice_id       text not null references invoices (invoice_id),
            line_number      int not null,
            description      text,
            sku              text,
            quantity         numeric(18,4),
            unit_price       numeric(18,4),
            line_total       numeric(18,4),
            po_line_id       text references po_lines (po_line_id),
            match_method     text
                             check (match_method in
                                   ('SKU','SINGLE_LINE','DESCRIPTION')),
            match_score      numeric(5,4),
            -- The best candidate that failed the ladder, for the reviewer.
            suggested_po_line_id text references po_lines (po_line_id),
            match_status     text not null default 'PENDING'
                             check (match_status in
                                   ('PENDING','MATCHED','EXCEPTION')),
            created_at       timestamptz not null default now(),
            constraint invoice_lines_method_matches_line
                check ((po_line_id is null) = (match_method is null))
        )
        """
    )
    op.execute(
        "create unique index invoice_lines_invoice_line_uidx "
        "on invoice_lines (invoice_id, line_number)"
    )
    # Prior billing sums invoice lines per PO line.
    op.execute("create index invoice_lines_po_line_idx on invoice_lines (po_line_id)")

    # exception_key names what the exception is about, for example
    # line:<invoice_line_id>:PRICE_VARIANCE. A re-run upserts on it, so
    # exception_id survives and spec 03 references stay valid.
    op.execute(
        """
        create table exceptions (
            exception_id     bigserial primary key,
            invoice_id       text not null references invoices (invoice_id),
            invoice_line_id  text references invoice_lines (invoice_line_id),
            exception_key    text not null,
            exception_type   text not null
                             check (exception_type in (
                                 'LOW_CONFIDENCE','SUPPLIER_UNRESOLVED',
                                 'NO_PO_REFERENCE','PO_NOT_FOUND','NO_PO_MATCH',
                                 'NO_GOODS_RECEIPT','OVER_RECEIPT','OVER_ORDER',
                                 'PRICE_VARIANCE','QUANTITY_VARIANCE',
                                 'LINE_TOTAL_MISMATCH','HEADER_TOTAL_MISMATCH',
                                 'TAX_MISMATCH','TAX_ID_INVALID',
                                 'CURRENCY_MISMATCH','DATE_INVALID',
                                 'DUPLICATE_EXACT','DUPLICATE_SUSPECTED')),
            severity         text not null check (severity in ('BLOCK','WARN')),
            source           text not null default 'MATCHER'
                             check (source in ('MATCHER','REVIEWER')),
            field_path       text,
            expected_value   text,
            actual_value     text,
            variance_amount  numeric(18,4),
            variance_pct     numeric(9,4),
            detail           text not null,
            status           text not null default 'OPEN'
                             check (status in ('OPEN','RESOLVED','WAIVED')),
            resolved_by      text check (resolved_by in ('MATCHER','REVIEWER')),
            resolved_at      timestamptz,
            created_at       timestamptz not null default now(),
            updated_at       timestamptz not null default now(),
            constraint exceptions_resolution_consistent check (
                (status = 'OPEN') = (resolved_at is null)
                and (resolved_at is null) = (resolved_by is null)
            )
        )
        """
    )
    op.execute(
        "create unique index exceptions_invoice_key_uidx "
        "on exceptions (invoice_id, exception_key)"
    )
    op.execute("create index exceptions_invoice_idx on exceptions (invoice_id)")
    op.execute(
        "create index exceptions_open_type_idx "
        "on exceptions (exception_type) where status = 'OPEN'"
    )


def downgrade() -> None:
    for table in (
        "exceptions",
        "invoice_lines",
        "invoices",
        "goods_receipt_lines",
        "goods_receipts",
        "po_lines",
        "purchase_orders",
        "suppliers",
    ):
        op.execute(f"drop table if exists {table} cascade")
    op.execute("drop function if exists normalise_supplier_names(text[])")
    op.execute("drop function if exists normalise_supplier_name(text)")
