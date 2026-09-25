# Spec 02: Matching Engine

**Module:** `src/matching`, `sql/`
**Week:** 2
**Depends on:** Spec 01 (ingestion and extraction)
**Blocks:** Spec 03 (review queue), Spec 05 (analytics)

---

## 1. Goal

A persisted extraction becomes a canonical invoice, resolved to a supplier,
matched line by line against purchase orders and goods receipts, and either
cleared for posting or decomposed into typed exceptions.

This is the module that carries the SQL work. The match itself is a set-based
query, not a Python loop. That is a deliberate design decision, it is defensible
on performance grounds, and it is a large part of why this project exists.

Human review is out of scope. This module ends when exceptions exist in the
database.

---

## 2. Definition of done

1. A clean invoice with a matching PO and full goods receipt produces
   `match_status = 'MATCHED'` on every line and zero blocking exceptions.
2. Seeded scenarios for every exception type in section 7 each produce exactly
   the expected exception rows, no more and no fewer.
3. The three-way match runs as a single SQL statement, committed as
   `src/matching/queries/three_way_match.sql`, reviewed and commented.
4. A Python reference implementation exists in
   `src/matching/reference_match.py`, produces identical results on the full
   seed set, and is benchmarked against the SQL version at 10,000 invoice lines.
   Both timings are recorded in `sql/README.md`.
5. Partial receipt and partial invoicing work correctly. An invoice for 40 of
   100 ordered units, where 50 have been received, matches. A second invoice for
   another 40 also matches. A third for 40 raises `OVER_RECEIPT`.
6. All thresholds, tolerances and severities come from `config/rules.yaml`. No
   numeric literal in the matching code. Changing a tolerance requires no code
   change and is covered by a test.
7. Duplicate detection catches both exact and near duplicates, demonstrated by
   seeded cases.
8. `dbt build` succeeds against the seeded database with all tests passing.

---

## 3. Canonical model

Extraction output is untrusted and messy. It is normalised into a canonical
invoice before matching. Keep the two separate: `extraction_results` is what the
model said, `invoices` is what the system believes.

```sql
create table suppliers (
    supplier_id       text primary key,
    legal_name        text not null,
    trading_names     text[] not null default '{}',
    tax_id            text,
    currency          char(3) not null,
    payment_terms_days int not null default 30,
    is_active         boolean not null default true,
    created_at        timestamptz not null default now()
);

create table purchase_orders (
    po_id             text primary key,
    po_number         text not null unique,
    supplier_id       text not null references suppliers (supplier_id),
    currency          char(3) not null,
    po_date           date not null,
    status            text not null
                      check (status in ('OPEN','CLOSED','CANCELLED')),
    created_at        timestamptz not null default now()
);

create table po_lines (
    po_line_id        text primary key,
    po_id             text not null references purchase_orders (po_id),
    line_number       int not null,
    sku               text,
    description       text not null,
    quantity_ordered  numeric(18,4) not null check (quantity_ordered > 0),
    unit_price        numeric(18,4) not null check (unit_price >= 0),
    unit_of_measure   text not null default 'EA',
    created_at        timestamptz not null default now()
);

create unique index po_lines_po_line_uidx on po_lines (po_id, line_number);

create table goods_receipts (
    gr_id             text primary key,
    gr_number         text not null unique,
    po_id             text not null references purchase_orders (po_id),
    received_date     date not null,
    created_at        timestamptz not null default now()
);

create table goods_receipt_lines (
    gr_line_id        text primary key,
    gr_id             text not null references goods_receipts (gr_id),
    po_line_id        text not null references po_lines (po_line_id),
    quantity_received numeric(18,4) not null,
    created_at        timestamptz not null default now()
);

create index gr_lines_po_line_idx on goods_receipt_lines (po_line_id);

create table invoices (
    invoice_id        text primary key,
    extraction_id     text not null references extraction_results (extraction_id),
    supplier_id       text references suppliers (supplier_id),
    po_number         text,
    invoice_number    text not null,
    invoice_date      date,
    due_date          date,
    currency          char(3),
    net_amount        numeric(18,4),
    tax_amount        numeric(18,4),
    total_amount      numeric(18,4),
    match_status      text not null default 'PENDING'
                      check (match_status in
                            ('PENDING','MATCHED','EXCEPTION','REJECTED','POSTED')),
    matched_at        timestamptz,
    created_at        timestamptz not null default now()
);

create unique index invoices_extraction_uidx on invoices (extraction_id);

-- The duplicate control. A supplier cannot invoice the same number twice.
-- Enforced here rather than in application code on purpose.
create unique index invoices_supplier_number_uidx
    on invoices (supplier_id, invoice_number)
    where supplier_id is not null;

create table invoice_lines (
    invoice_line_id   text primary key,
    invoice_id        text not null references invoices (invoice_id),
    line_number       int not null,
    description       text,
    sku               text,
    quantity          numeric(18,4),
    unit_price        numeric(18,4),
    line_total        numeric(18,4),
    po_line_id        text references po_lines (po_line_id),
    match_status      text not null default 'PENDING',
    created_at        timestamptz not null default now()
);

create unique index invoice_lines_invoice_line_uidx
    on invoice_lines (invoice_id, line_number);

create table exceptions (
    exception_id      bigserial primary key,
    invoice_id        text not null references invoices (invoice_id),
    invoice_line_id   text references invoice_lines (invoice_line_id),
    exception_type    text not null,
    severity          text not null check (severity in ('BLOCK','WARN')),
    field_path        text,
    expected_value    text,
    actual_value      text,
    variance_amount   numeric(18,4),
    variance_pct      numeric(9,4),
    detail            text not null,
    status            text not null default 'OPEN'
                      check (status in ('OPEN','RESOLVED','WAIVED')),
    created_at        timestamptz not null default now()
);

create index exceptions_invoice_idx on exceptions (invoice_id);
create index exceptions_open_type_idx
    on exceptions (exception_type) where status = 'OPEN';
```

---

## 4. Supplier resolution

Extraction gives a supplier name and possibly a tax ID. Neither is reliable.
Resolve in this order and stop at the first hit:

1. Exact match on `tax_id`.
2. Exact match on normalised `legal_name` or any `trading_names` entry.
   Normalisation lowercases, strips punctuation and removes entity suffixes
   such as `pte ltd`, `private limited`, `llp`, `inc`.
3. Trigram similarity above the configured threshold using `pg_trgm`, with
   `similarity()` and a `%` operator index. Ambiguity, meaning two candidates
   within 0.05 of each other, is treated as no match.

Failure raises `SUPPLIER_UNRESOLVED` at severity `BLOCK`. Do not guess. A
wrongly resolved supplier pays the wrong bank account, which is the single most
expensive failure this system can produce.

Record the resolution method and score on the invoice so a reviewer can see why
a supplier was chosen.

---

## 5. The match query

Lives at `src/matching/queries/three_way_match.sql`. Single statement, CTEs,
parameterised on `invoice_id`.

Structure:

```sql
with receipts as (
    -- Cumulative quantity received per PO line.
    select po_line_id, sum(quantity_received) as qty_received
    from goods_receipt_lines
    group by po_line_id
),
billed_before as (
    -- Quantity already invoiced against each PO line by OTHER invoices that
    -- are matched or posted. Excludes the invoice under evaluation, so the
    -- query is safe to re-run.
    select il.po_line_id, sum(il.quantity) as qty_billed_prior
    from invoice_lines il
    join invoices i on i.invoice_id = il.invoice_id
    where i.match_status in ('MATCHED','POSTED')
      and i.invoice_id <> :invoice_id
      and il.po_line_id is not null
    group by il.po_line_id
),
candidate as (
    select
        il.invoice_line_id,
        il.quantity,
        il.unit_price,
        il.line_total,
        pol.po_line_id,
        pol.unit_price      as po_unit_price,
        pol.quantity_ordered,
        coalesce(r.qty_received, 0)        as qty_received,
        coalesce(bb.qty_billed_prior, 0)   as qty_billed_prior
    from invoice_lines il
    join invoices i          on i.invoice_id = il.invoice_id
    left join purchase_orders po on po.po_number = i.po_number
    left join po_lines pol   on pol.po_id = po.po_id
                            and pol.sku is not distinct from il.sku
    left join receipts r     on r.po_line_id = pol.po_line_id
    left join billed_before bb on bb.po_line_id = pol.po_line_id
    where il.invoice_id = :invoice_id
)
select
    invoice_line_id,
    po_line_id,
    case
        when po_line_id is null then 'NO_PO_MATCH'
        when qty_received = 0   then 'NO_GOODS_RECEIPT'
        when quantity + qty_billed_prior > qty_received
             * (1 + :qty_tolerance_pct) then 'OVER_RECEIPT'
        when abs(unit_price - po_unit_price)
             > greatest(po_unit_price * :price_tolerance_pct,
                        :price_tolerance_abs) then 'PRICE_VARIANCE'
        when abs(line_total - (quantity * unit_price))
             > :line_total_tolerance_abs then 'LINE_TOTAL_MISMATCH'
        else 'MATCHED'
    end as match_status,
    unit_price - po_unit_price as price_variance,
    case when po_unit_price > 0
         then (unit_price - po_unit_price) / po_unit_price end as price_variance_pct
from candidate;
```

Two details that matter and will be asked about:

- `is not distinct from` rather than `=` on the SKU join, so a null SKU on both
  sides is treated as a match rather than dropping the row silently.
- `billed_before` excludes the current invoice, so re-running the match is
  idempotent. Getting this wrong means a re-run flags every invoice as an
  over-receipt.

Header-level checks run separately: line sum against `net_amount`, tax
recalculation, currency agreement between invoice, PO and supplier.

---

## 6. Duplicate detection

Two passes, both SQL, in `sql/analysis/duplicate_detection.sql`.

**Exact.** The unique index on `(supplier_id, invoice_number)` handles this at
write time. Catch the `IntegrityError` and raise `DUPLICATE_EXACT` at `BLOCK`.

**Near.** Same supplier, same total within a small absolute tolerance, invoice
dates within a configured window, different invoice numbers. This catches
resubmission with a modified reference, which is a genuine AP leakage pattern.

```sql
select
    i.invoice_id,
    lag(i.invoice_id) over w  as prior_invoice_id,
    lag(i.invoice_date) over w as prior_date,
    i.total_amount
from invoices i
where i.supplier_id = :supplier_id
window w as (
    partition by i.supplier_id, i.total_amount
    order by i.invoice_date
)
```

Flag where the gap between consecutive dates is under the configured window.
Severity `WARN`, since false positives are common and blocking on them annoys
the AP team more than it saves.

---

## 7. Exception taxonomy

| Type | Default severity | Trigger |
| --- | --- | --- |
| `LOW_CONFIDENCE` | BLOCK | Field confidence below its threshold |
| `SUPPLIER_UNRESOLVED` | BLOCK | No confident supplier match |
| `NO_PO_REFERENCE` | BLOCK | No PO number extracted |
| `PO_NOT_FOUND` | BLOCK | PO number does not exist or is cancelled |
| `NO_PO_MATCH` | BLOCK | Line has no corresponding PO line |
| `NO_GOODS_RECEIPT` | BLOCK | Nothing received against the PO line |
| `OVER_RECEIPT` | BLOCK | Cumulative billed exceeds received |
| `PRICE_VARIANCE` | BLOCK | Unit price outside tolerance |
| `QUANTITY_VARIANCE` | WARN | Quantity differs from ordered within receipt |
| `LINE_TOTAL_MISMATCH` | BLOCK | Line total not equal to quantity times price |
| `HEADER_TOTAL_MISMATCH` | BLOCK | Lines do not sum to net amount |
| `TAX_MISMATCH` | BLOCK | Tax amount not equal to net times rate |
| `TAX_ID_INVALID` | WARN | Supplier tax ID fails format validation |
| `CURRENCY_MISMATCH` | BLOCK | Invoice currency differs from PO |
| `DATE_INVALID` | WARN | Invoice dated before the PO or in the future |
| `DUPLICATE_EXACT` | BLOCK | Unique constraint violated |
| `DUPLICATE_SUSPECTED` | WARN | Near-duplicate heuristic fired |

Any `BLOCK` sets the invoice to `EXCEPTION`. Only `WARN` exceptions allow
`MATCHED`, carried forward as flags for the reviewer to see post hoc.

---

## 8. Tax validation

Configure the rate in `rules.yaml` rather than hardcoding it. Singapore GST is
currently 9%, but rates change and invoices may be dated before a change, so
the rule takes a rate schedule with effective dates and selects by invoice date.

Zero-rated and exempt supplies exist, so a zero tax amount is not automatically
an exception. Treat a mismatch as `TAX_MISMATCH` only when tax is non-zero and
does not reconcile to any configured rate.

For the tax ID format check, write the pattern into `rules.yaml` and verify it
against current IRAS guidance yourself before relying on it. Do not accept a
generated regex for this without checking the source.

---

## 9. `config/rules.yaml`

```yaml
version: 1

confidence:
  default: 0.85
  fields:
    invoice_number: 0.95
    total_amount:   0.98
    tax_amount:     0.95
    invoice_date:   0.90
    supplier_name:  0.90

tolerances:
  price_pct:            0.02
  price_abs:            0.50
  quantity_pct:         0.00
  line_total_abs:       0.01
  header_total_abs:     0.02

supplier_resolution:
  trigram_threshold:    0.82
  ambiguity_margin:     0.05

duplicates:
  amount_tolerance_abs: 0.01
  date_window_days:     45

tax:
  rates:
    - { code: SG_GST, rate: 0.09, effective_from: 2024-01-01 }
    - { code: SG_GST, rate: 0.08, effective_from: 2023-01-01 }
  tax_id_pattern: "TO BE VERIFIED AGAINST IRAS"

severity_overrides:
  QUANTITY_VARIANCE: WARN
  DUPLICATE_SUSPECTED: WARN
```

Load and validate with Pydantic at startup. An invalid rules file fails fast at
boot, never at match time.

---

## 10. Seed data

Write `scripts/seed.py` generating synthetic suppliers, POs and goods receipts,
plus a scenario set with one invoice per exception type and at least twenty
clean invoices. Seeding is deterministic under a fixed seed so tests are
repeatable.

Scale the generator to 10,000 invoice lines for the benchmark in section 2.4.

No real supplier names, tax IDs or bank details, in line with `CLAUDE.md` 5.6.

---

## 11. Tests

- One test per exception type asserting exactly the expected rows.
- Partial receipt sequence: three invoices against one PO line, third fails.
- Re-running the match on an already matched invoice changes nothing.
- Tolerance boundary tests at exactly the threshold and one cent either side.
- Rules file change alters outcome with no code change.
- SQL and Python implementations agree on the full seed set.
- Currency mismatch between invoice and PO blocks.

---

## 12. Decisions I have not made yet

1. When an invoice references no PO at all, should it be rejected outright or
   routed to review for manual PO assignment? Manual assignment is more
   realistic and more work.
2. Should `PRICE_VARIANCE` be evaluated per line or on the invoice total? Per
   line is stricter and catches offsetting errors; total is how some AP teams
   actually operate.
3. Whether to match invoice lines to PO lines on description similarity when
   SKU is absent, or to raise `NO_PO_MATCH`. Similarity matching is a
   meaningful accuracy gain and a meaningful source of wrong matches.

Raise these before implementing. Each changes the schema or the query.
