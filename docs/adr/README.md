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
