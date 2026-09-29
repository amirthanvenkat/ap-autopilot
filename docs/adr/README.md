# Architecture decision records

TODO: written by the repository owner. CLAUDE.md section 8 reserves ADRs for
a human author.

Decisions from spec 01 that warrant a record:

- Acknowledge by committing a transactional outbox, rather than doing work
  after the response.
- Write the message marker and the unit of work in one statement.
- Three attachments on one email become three documents.
- `POST /v1/documents` deduplicates synchronously and returns 200 for known
  content.
- Store a trimmed extraction payload in Postgres and keep the full response
  in Cloud Storage.

Decisions from spec 02 section 12, taken on 2026-09-25:

- An invoice with no PO reference raises `NO_PO_REFERENCE` at `BLOCK` and
  goes to review for manual PO assignment. It is never rejected: rejection
  is final, and the resubmission would collide with the unique index on
  supplier and invoice number.
- `PRICE_VARIANCE` is evaluated per line, with no option to evaluate on the
  invoice total. Offsetting line errors net to zero on a total.
- Invoice lines match PO lines through a ladder: SKU, then a single-line PO,
  then guarded description similarity, then `NO_PO_MATCH` with a suggested
  candidate. Null SKU never matches null SKU. Price never chooses the line.
- Each failed check on an invoice line produces its own exception row. The
  spec's single CASE, where the first failure wins, is replaced.
- The definition of done 5 test adds a second goods receipt that takes the
  received total to 100 between the first and second invoices. As written,
  with 50 received, the second invoice is already an over-receipt.
- The match takes a lock per purchase order, sums split lines within an
  invoice, counts prior billing in match order, replaces rather than appends
  its own open exceptions, refuses to re-match a posted invoice, and checks
  against ordered quantity.

Further spec 02 decisions, taken on 2026-09-26:

- Extraction gains `po_number` and line `sku`, a spec 01 follow-up. Without
  them no fixture invoice can match.
- An exact duplicate is stored with `duplicate_of_invoice_id` set, and the
  unique index excludes such rows, so `DUPLICATE_EXACT` has an invoice to
  reference.
- Billing beyond the ordered quantity raises a new `OVER_ORDER` at `BLOCK`.
- Tax reconciles within a new `tax_abs` tolerance.
- Tolerances take per-currency overrides.
- `LOW_CONFIDENCE` applies to header fields only.

Spec 02 decisions taken on 2026-09-29:

- Tax reconciles against the rate in effect on the invoice date only.
- A PO line can be marked `TWO_WAY`. That skips the goods receipt checks
  for services and keeps the price and ordered quantity checks.
- Exceptions are upserted on `(invoice_id, exception_key)` and never
  deleted, so `exception_id` survives a re-run. Rows no longer raised are
  resolved by the matcher. `WAIVED` rows never reopen.
- The match query takes a batch of invoice ids, with at most one invoice
  per PO per batch, and the caller runs rounds. That keeps set-based
  execution equivalent to matching one invoice at a time.
- A tax ID hit resolves a supplier only when the extracted name scores at
  least 0.30 against that supplier's names, a floor chosen from measured
  scores. A refused tier never falls through to a weaker one.
- A name match is refused when the invoice's tax ID matches nobody and the
  named supplier is registered under another. Inactive suppliers are never
  resolved.
- Tax IDs are unique and compared in normalised form (migration 0004).
