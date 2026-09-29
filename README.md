# ap-autopilot

An accounts payable pipeline. Supplier invoices arrive by email. Google
Document AI extracts them. Each invoice is matched against a purchase order
and a goods receipt. Anything that fails a rule goes to a human reviewer.
Clean invoices are posted to Xero as bills.

This is a portfolio project. The aim is a small system that demonstrably
works, with the reasoning behind each decision written down.

## Status

| Module | Spec | State |
| --- | --- | --- |
| Ingestion and extraction | `docs/specs/01-ingestion-and-extraction.md` | Built and tested, including the spec 02 prerequisite in 5.1 |
| Matching engine | `docs/specs/02-matching-engine.md` | Schema (migration `0002`) and rules loader built. Resolver and match query not built |
| Review queue, posting, analytics, MCP server | Specs 03 to 05 | Not started |

Section 5 is mostly design. The parts built so far are 5.1, 5.3 and 5.8.

## Contents

1. What it does
2. Running it
3. Architecture and deployment
4. Spec 01 as built
5. Spec 02 design
6. Challenges to the spec 02 decisions
7. Open questions
8. Glossary

---

## 1. What it does

```
Gmail label ap-inbox ──► Pub/Sub ──► POST /internal/gmail/notify
                                           │
                     POST /v1/documents ───┤  store file, create document
                        (manual upload)    ▼
                               Document AI batch (async)
                                           │  output lands in Cloud Storage
                                           ▼
                            POST /internal/extraction/complete
                                           │  validate, persist fields and confidence
                                           ▼
                          extraction_results, extraction_fields     ◄── spec 01 ends here
                                           │
                                           ▼
                 canonical invoice ► supplier ► three-way match     ◄── spec 02
                                           │
                            MATCHED ───────┴─────── EXCEPTION
                               │                        │
                           Xero bill              review queue      ◄── specs 03, 04
```

Terms used throughout:

| Term | Meaning |
| --- | --- |
| Three-way match | An invoice line reconciled against a PO line and a goods receipt line |
| Exception | A failed rule that needs a human decision |
| STP rate | Share of invoices posted with no human touch |
| Touch time | How long an exception waits in the review queue |

---

## 2. Running it

The demo runs in fixtures mode by default. It makes no external call and
needs no cloud account. This is deliberate: the GCP trial expires after 90
days and the demo has to keep working after that.

Requirements: Python 3.12, `uv`, and PostgreSQL 16.

```bash
uv sync
cp .env.example .env            # REPLAY_FIXTURES=true is the default
# set DATABASE_URL in .env to a local or Neon database
uv run alembic upgrade head
uv run python -m uvicorn src.app:app
```

Tests:

```bash
uv run pytest                   # unit tests, no database needed
TEST_DATABASE_URL=postgresql+psycopg://localhost:5433/ap_test uv run pytest
```

The integration tests need a real Postgres and skip without
`TEST_DATABASE_URL`. There is no substitute: the design relies on data
modifying CTEs, `FOR UPDATE SKIP LOCKED`, advisory locks and `jsonb`.
`scripts/local-postgres.ps1` runs a disposable local server on Windows.

Tests never touch the network. Outbound HTTP is mocked with `respx`.

A Postman collection in `docs/postman/` covers every spec 01 endpoint and
includes saved example responses.

Lint and types:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

---

## 3. Architecture and deployment

| Component | Host | Why |
| --- | --- | --- |
| Document AI, Cloud Storage, Pub/Sub | GCP `asia-southeast1` | Document AI runs there |
| Pipeline API, reviewer UI, MCP server | DigitalOcean | Long-lived credit |
| Postgres | Neon free tier, Singapore | Persistent at no cost. Same region as Cloud Run |
| dbt | Local, artefacts committed | Free |

Nothing assumes a private network. Every call between services is
authenticated HTTPS.

Rules the code holds to (full list in `CLAUDE.md` section 5):

- Every outbound call goes through `common.http.request_with_retry`. It owns
  timeouts, backoff with jitter, and retry classification.
- Every inbound push handler acknowledges before it does work.
- Every handler is idempotent, and Postgres enforces it. In-memory state is
  never relied on.
- Money is `Decimal` in Python and `numeric(18,4)` in Postgres. Currency is
  always an explicit column.
- Errors are typed. The split between `TransientError` and `PermanentError`
  decides whether a worker retries.
- Fixtures are synthetic. No real supplier name, tax number or bank detail
  appears anywhere in the repository.

---

## 4. Spec 01 as built

The pipeline follows spec 01 with seven deliberate departures. Each one
exists because the spec, followed literally, would lose or duplicate an
invoice.

### 4.1 Acknowledgement is a committed outbox row

Spec 01 section 4 asked handlers to return 200 in every case, and to send
transient failures to the dead letter topic. Those cannot both hold: a
dead letter topic only receives messages that were not acknowledged.

Section 7 asked for work to happen after the response. Cloud Run throttles
CPU once a response is sent, so that work can stall or vanish.

So a push handler now does four things. It verifies the OIDC token. Then, in
one SQL statement, it inserts the `processed_events` marker and an
`ingestion_outbox` task. Then it returns 200. A worker claims the task
afterwards.

200 therefore means "durably accepted", not "done". Retry classification
moved to the worker (`src/common/outbox.py`, `src/common/runner.py`).

### 4.2 The marker and the work are one statement

Consider the two orderings:

- **Marker committed first.** A crash before the work leaves the marker in
  place. The redelivery sees it and drops the message, so the work is lost.
- **Work committed first.** A crash before the marker lets the redelivery
  do the work a second time.

One statement removes the gap in both directions. It also makes concurrent
redelivery safe without an application lock, because the second insert
waits on the primary key.

### 4.3 Two layers of deduplication

`processed_events` catches Pub/Sub delivering the same message twice.
`documents.content_hash` catches the same file arriving in two different
messages, for example a supplier who emails twice. A third table,
`document_sources`, records every route by which a document arrived, so an
audit trail still shows both emails.

### 4.4 `extraction_fields` has a unique constraint

Spec 01 did not ask for one. Without it, a replay that split its writes
would double every per-field analytic and raise no error. Each field is
keyed by a JSON Pointer that includes array indices. That keeps
`/line_items/3/unit_price` distinct from `/line_items/4/unit_price`.

### 4.5 A reaper for jobs that never finish

A failed Document AI operation writes no output, so no completion event ever
arrives. Without a sweep, such a job stays `RUNNING` forever and nothing
reports it. `POST /internal/extraction/reap` resolves jobs that have been
running longer than `JOB_TIMEOUT_SECONDS`.

A batch also writes several output objects. The completion endpoint
therefore gets several messages for one job and must treat all but the
first as no-ops.

### 4.6 Gmail watch renewal

`users.watch` lapses after seven days without warning.
`POST /internal/gmail/watch/renew` re-arms it and is meant to run daily.

### 4.7 Stored payload and money types

The trimmed Document AI response is stored in Postgres. The full response
stays in Cloud Storage, one fetch away. Money travels as a string in the
extraction schema, because a JSON number is parsed as a float before any
validation runs.

### Decisions from spec 01 section 11

- One email with three attachments creates three documents.
- `POST /v1/documents` deduplicates synchronously. It returns 200 with the
  existing id for known content, and 202 for new content.
- `raw_payload` holds the trimmed response (see 4.7).

### Fixtures

`scripts/generate_fixtures.py` generates all 20 fixtures, and its output is
committed. They cover a scanned document, a poor-quality scan, a two-page
and a three-page invoice, a French invoice in EUR, a PNG, a TIFF, and one
deliberately malformed response. The cached Document AI responses are shaped
by hand to match Invoice Parser output. No fixture came from a paid call.

---

## 5. Spec 02 design

**Goal.** Turn a persisted extraction into a canonical invoice. Resolve its
supplier. Match each line against a PO and goods receipts. Then either clear
the invoice for posting or break it down into typed exceptions. The module
ends when the exception rows exist.

### 5.1 Prerequisite: extraction captures the PO number and SKU

Built on 2026-09-29 as a spec 01 follow-up. Before it, extraction carried
neither a PO number nor a line product code. Every invoice would have
raised `NO_PO_REFERENCE`, and no line could match by SKU.

- `src/extraction/normalise.py` maps the Invoice Parser entities
  `purchase_order` and `line_item/product_code`. The repository owner
  confirmed both names against the processor's field list.
- `po_number` is an optional header field, stored in
  `extraction_results.po_number` (migration `0003`). It counts as a header
  field for the confidence thresholds, because a misread PO number selects
  the wrong purchase order.
- `sku` is optional on a line. It is absent unless the parser reports a
  product code. Spec 01 lists four required line properties, and absent
  means "never reported", which the schema keeps distinct from null.
- `scripts/generate_fixtures.py` prints and extracts both:
  - Goods lines carry a supplier part number. Service lines carry none,
    which exercises the lower rungs of the line ladder.
  - The utility bill quotes no PO, which demonstrates `NO_PO_REFERENCE`.
  - The poor-quality scan prints a PO number the extractor cannot read.

  Changing a source document changes its content hash, so the generator
  now clears the cached responses before writing them.

### 5.2 Flow and trigger

1. **Enqueue.** The transaction that stores an extraction also inserts a
   `match_invoice` outbox task. It uses the mechanism from 4.1 and needs no
   new infrastructure.
2. **Canonicalise.** `extraction_results` becomes `invoices` and
   `invoice_lines`. The invoice id is derived from `extraction_id`, and the
   insert uses `on conflict (extraction_id) do nothing`. Spec 01 keeps what
   the model said. Spec 02 records what the system believes.
3. **Resolve the supplier** (5.4).
4. **Match.** Lock the PO, run the line match, the header checks and
   duplicate detection, write the exceptions, and set `match_status`. All of
   this happens in one transaction.

Delivering the same task three times gives one invoice and one set of
exceptions.

### 5.3 Schema additions beyond spec 02 section 3

These go in migration `0002`. Migrations are forward-only.

| Column or object | Why |
| --- | --- |
| `invoices.supplier_resolution_method`, `supplier_resolution_score` | Spec section 4 asks for them. The DDL omits them |
| `invoices.po_id` | The resolved PO, and the slot for manual PO assignment in spec 03 |
| `invoices.duplicate_of_invoice_id` | Makes `DUPLICATE_EXACT` storable (5.7) |
| `invoices.invoice_number` becomes nullable | Extraction can report no invoice number. A not-null column would make the canonical insert fail |
| `invoice_lines.match_method`, `match_score`, `suggested_po_line_id` | Record which rung of the line ladder matched (5.5) |
| `po_lines.match_type` (`THREE_WAY` or `TWO_WAY`, default `THREE_WAY`) | Service lines have no goods receipt (6.1, adopted) |
| `exceptions.source` (`MATCHER` or `REVIEWER`) | Lets a re-run touch only the rows the matcher owns |
| `exceptions.exception_key`, unique with `invoice_id` | Stable identity across re-runs (6.8, adopted) |
| `exceptions.resolved_by`, `resolved_at` | Tells a matcher auto-close apart from a reviewer decision |
| `pg_trgm` extension and a GIN trigram index on normalised supplier names | Supplier resolution. Neon supports `pg_trgm`. Confirm when the migration runs |

### 5.4 Supplier resolution

One SQL statement tries three tiers in order and stops at the first hit:

1. Exact tax ID.
2. Exact normalised legal or trading name. Normalisation lowercases the
   name, strips punctuation, and removes entity suffixes such as `pte ltd`
   and `llp`. It is a SQL function, so the index and the lookup cannot
   drift apart.
3. Trigram similarity. The best candidate is accepted only if its score is
   at least `trigram_threshold` and the runner-up is more than
   `ambiguity_margin` behind.

A miss raises `SUPPLIER_UNRESOLVED` at `BLOCK`. Matching still runs on the
lines, so a reviewer sees every problem in one pass.

### 5.5 The line match

The match is one statement at `src/matching/queries/three_way_match.sql`,
parameterised on an array of invoice ids (6.9, adopted). Matching one
invoice is the one-element case. The decisions of 2026-09-25 and
2026-09-29 change the spec's version in five ways.

**Line ladder.** Each invoice line is paired with a PO line by the first rung
that succeeds:

1. SKU equality. A null SKU never matches a null SKU, so the spec's
   `is not distinct from` is dropped.
2. The PO has exactly one line.
3. Description similarity. It must clear a threshold and beat the runner-up
   by a margin. Both values live in `rules.yaml`.
4. Otherwise `NO_PO_MATCH`, with the best rejected candidate stored as
   `suggested_po_line_id` for the reviewer.

Price never decides which PO line a line matches. If it did, a price
variance could be hidden by matching to a line with a closer price.

**One row per failed check.** The spec's single `CASE` reports only the first
failure. It is replaced with a lateral `values` list, so a line that is both
over-received and mispriced produces two exception rows. There are two
gates:

- `NO_PO_MATCH` suppresses every other check on that line.
- `NO_GOODS_RECEIPT` suppresses `OVER_RECEIPT`.

All other checks are independent.

**Two-way lines** (6.1, adopted). A PO line with `match_type = 'TWO_WAY'`
skips `NO_GOODS_RECEIPT` and `OVER_RECEIPT`. It still gets the price, line
total and `OVER_ORDER` checks. The default is `THREE_WAY`, so a PO line is
only exempt from the receipt checks when someone marks it.

**Quantities.**

- Split lines on one invoice that point at the same PO line are summed
  before any quantity check.
- `billed_before` counts only `MATCHED` or `POSTED` invoices whose
  `matched_at` is earlier than the current invoice's. Excluding the current
  invoice is not enough. Without the time bound, re-running invoice A after
  invoice B has matched would count B against A and turn A into
  `OVER_RECEIPT`.
- `matched_at` is set on the first match and never rewritten.
- `OVER_RECEIPT`: cumulative billed quantity exceeds received quantity,
  plus tolerance.
- `OVER_ORDER` is a new type at `BLOCK`. It fires when cumulative billed
  quantity exceeds ordered quantity, plus tolerance. Goods can be received
  beyond the order, so the two checks are independent and both can fire.

**Tolerance comparisons.** A value fails only when it strictly exceeds its
limit, so a value exactly at the threshold passes. Boundary tests pin this at
the threshold and one cent either side.

**Concurrency.** The match first takes `select ... for update` on every
`purchase_orders` row in the batch, in `po_id` order so two batches cannot
deadlock. Two matches touching one PO run one after the other, so neither
can miss the other's billing. Batches on different POs do not block each
other. An invoice that references two POs is out of scope.

**Batches** (6.9, adopted). A batch holds at most one invoice per PO. The reason: whether an earlier invoice uses up receipt capacity depends
on whether it matched. That depends on its own checks, so a window sum
inside one statement cannot know it. The caller therefore builds rounds.
Each round takes the oldest pending invoice per PO, ordered by
`(created_at, invoice_id)`, and matches the round in one statement. It
repeats until nothing is pending. The results are identical to matching
invoices one at a time in that order, which is what lets the SQL and the
Python reference be compared exactly. Invoices with no PO do not compete
for capacity, so they all go in the first round.

**Re-runs.**

- A `POSTED` invoice is never re-matched. The attempt raises a typed
  `PermanentError`.
- Otherwise exceptions are upserted, not replaced (6.8, adopted). Each row
  carries an `exception_key` built from what it is about. Examples:
  `line:<invoice_line_id>:PRICE_VARIANCE`,
  `field:/invoice_date:LOW_CONFIDENCE`,
  `duplicate:<other_invoice_id>:DUPLICATE_SUSPECTED`.
  `(invoice_id, exception_key)` is unique.
- A re-run that raises the same key updates the variance on the existing
  row, so its `exception_id` never changes and spec 03 references stay
  valid.
- A matcher row that is `OPEN` but no longer raised becomes `RESOLVED` with
  `resolved_by = 'MATCHER'`.
- A `WAIVED` row is never reopened. Whether a reviewer's `RESOLVED` row
  reopens when the matcher raises it again is open question 7.2.

**Worked example: definition of done 5.** A PO line orders 100 units.

| Step | Received | Billed before | This invoice | Result |
| --- | --- | --- | --- | --- |
| GR1 receives 50, invoice 1 bills 40 | 50 | 0 | 40 | `MATCHED` |
| GR2 receives 50, invoice 2 bills 40 | 100 | 40 | 40 | `MATCHED` |
| Invoice 3 bills 40 | 100 | 80 | 40 | `OVER_RECEIPT`, `OVER_ORDER` |

The spec's own sequence keeps receipts at 50. On that basis invoice 2 would
already be over-received (80 billed against 50 received). The test therefore
adds GR2.

### 5.6 Header checks

A second statement in the same transaction runs these:

- `HEADER_TOTAL_MISMATCH`: the line sum differs from `net_amount` by more
  than `header_total_abs`.
- `TAX_MISMATCH`: tax is non-zero and differs from net times the rate by
  more than the new `tax_abs` tolerance. The rate is the one in effect on
  the invoice date, decided on 2026-09-29. A rate that was valid at some
  other date does not reconcile. Zero tax is allowed, because zero-rated and
  exempt supplies exist. An invoice with no date cannot select a rate, so
  the check is skipped. `LOW_CONFIDENCE` already blocks that invoice.
- `CURRENCY_MISMATCH`: the invoice, PO and supplier currencies disagree.
- `DATE_INVALID` (`WARN`): the invoice is dated before its PO, or in the
  future.
- `TAX_ID_INVALID` (`WARN`): disabled until a verified pattern exists (7.1).
- `LOW_CONFIDENCE`: applies to **header fields only**, decided on
  2026-09-26. A field that is present but has null confidence counts as low.
  A header field with a configured threshold that is absent, or present with
  a null value, raises `LOW_CONFIDENCE` with a null actual value. This is
  where spec 01's distinction between a missing key and a null value is
  used. The detail text records which of the two it was.

A `BLOCK` sets the invoice to `EXCEPTION`. `WARN` rows still allow `MATCHED`
and are shown to the reviewer afterwards. No path sets `REJECTED`, because an
invoice with no PO goes to review rather than being rejected. `REJECTED`
becomes reachable only through a reviewer action in spec 03.

### 5.7 Duplicate detection

**Exact.** As written, the spec cannot record this. The unique index on
`(supplier_id, invoice_number)` stops the second invoice from being
inserted, but `exceptions.invoice_id` is not null. There is nothing for the
exception to reference.

The agreed fix:

- Add `duplicate_of_invoice_id`.
- Narrow the index to rows where `supplier_id is not null and
  duplicate_of_invoice_id is null`.
- Insert the duplicate with its pointer set, and attach `DUPLICATE_EXACT`
  at `BLOCK`.

The constraint still lives in Postgres, and the reviewer sees both
documents.

**Near.** Same supplier, a total within `amount_tolerance_abs`, dates within
`date_window_days`, and a different invoice number. The result is
`DUPLICATE_SUSPECTED` at `WARN`. The query lives in
`sql/analysis/duplicate_detection.sql`. Section 6.5 shows that the spec's
window query needs changing.

### 5.8 `config/rules.yaml`

A Pydantic model validates the file at startup, and an invalid file stops
the service booting. No numeric literal appears in matching code. Additions
to the spec's file:

```yaml
tolerances:
  price_pct:        0.02
  price_abs:        0.50
  quantity_pct:     0.00
  line_total_abs:   0.01
  header_total_abs: 0.02
  tax_abs:          0.01          # new, decided 2026-09-26
  currency_overrides:             # new, decided 2026-09-26, values to be set
    JPY: { price_abs: 50, line_total_abs: 1, header_total_abs: 2, tax_abs: 1 }

line_matching:                    # new, for ladder rung 3
  description_threshold: <to be set>
  description_margin:    <to be set>

tax:
  tax_id_pattern: null            # null disables TAX_ID_INVALID (7.1)
```

The loader rejects three things: a tax rate schedule with duplicate or
overlapping effective dates, an override for a currency code that is not
three uppercase letters, and a negative tolerance. The JPY line shows the
shape only. Its values are placeholders until they are chosen.

### 5.9 Proving the SQL

- `src/matching/reference_match.py` implements the same ladder and checks
  in plain Python.
- A test compares the SQL and Python results across the whole seed set, as
  sets of `(invoice_line_id, exception_type, variance)`. Variances are
  quantised to four decimal places on both sides first, because Postgres
  `numeric` division and Python's `Decimal` context round differently.
- `scripts/benchmark.py` times both versions at 10,000 invoice lines. The
  timings go into `sql/README.md`. Both sides are timed per round over the
  same batches (5.5), so the comparison measures set-based execution, not
  round trips.
- `scripts/seed.py` is deterministic under a fixed seed. It produces one
  scenario per exception type and at least twenty clean invoices. Its
  supplier names match the fixture suppliers so the demo resolves them.

### 5.10 Failure modes

| Failure | What happens |
| --- | --- |
| Task delivered twice | Derived id and extraction unique index make the second a no-op |
| Worker crashes mid-match | One transaction rolls back. The outbox lease expires and the task retries |
| Two invoices on one PO at once | The PO row lock serialises them |
| Neon statement timeout (5 s) while the lock is held | `TransientError`, retried. The benchmark connects directly, not through the pooler |
| `rules.yaml` edited while running | No effect until restart. Rules load once at boot |
| Invoice in `EXCEPTION` while a later one matches | The later one uses the receipt capacity first. See 6.7 |

---

## 6. Challenges to the spec 02 decisions

These are places where the design as first agreed is weaker than it looks.
Each ends with a recommendation and its status.

| Challenge | Status |
| --- | --- |
| 6.1 Two-way lines for services | Adopted 2026-09-29, see 5.5 |
| 6.8 Upsert exceptions on a stable key | Adopted 2026-09-29, see 5.5 |
| 6.9 Batch the match query | Adopted 2026-09-29, see 5.5 |
| 6.2 to 6.7, 6.10, 6.11 | Open. The design in section 5 does not include them |

### 6.1 Services have no goods receipt (adopted)

Several fixture suppliers sell services: consulting, marketing, security,
software. A service PO normally has no goods receipt, so every such line
would raise `NO_GOODS_RECEIPT`. The STP rate would then fall for a reason
that has nothing to do with invoice quality.

*Recommendation:* add a `match_type` on `po_lines` (`TWO_WAY` or
`THREE_WAY`). A two-way line skips the receipt checks and still gets the
price and ordered-quantity checks.

### 6.2 `greatest()` makes the absolute price tolerance a floor

The spec allows `greatest(po_price * price_pct, price_abs)`. For a unit
price of 0.20, that tolerance is 0.50, so a billed price of 0.70 passes, a
250% variance.

The tolerance also applies per unit. At 10,000 units, 0.50 per unit is 5,000
of accepted variance on one line. The per-currency map fixes the currency
problem but not this one.

*Recommendation:* either use `least()`, so the stricter limit wins, or add a
line-level cap on `abs(price_variance) * quantity`. The second is closer to
how AP teams think about leakage.

### 6.3 The single-line PO rung can mis-pair a surcharge

Freight, fuel surcharge and handling lines are common. If the PO has one
line and the invoice has a delivery charge, rung 2 pairs the delivery charge
with the goods line. The quantity checks then run against the wrong thing.

*Recommendation:* use rung 2 only when the invoice also has exactly one
unmatched line, or when description similarity clears a low floor.

### 6.4 An exact tax ID match can still be wrong

A misread tax ID that happens to equal another supplier's ID wins tier 1
outright. The spec names paying the wrong supplier as the most expensive
failure this system can produce.

*Recommendation:* accept a tax ID hit only when the name similarity also
clears a low floor. Otherwise raise `SUPPLIER_UNRESOLVED`.

### 6.5 The near-duplicate rule and query disagree

- The spec's window partitions by exact `total_amount`, which ignores
  `amount_tolerance_abs`.
- `lag()` compares only neighbouring rows.
- The query does not check that the invoice numbers differ.
- A 45-day window flags every monthly fixed-fee invoice, since a month is
  shorter than 45 days. Subscriptions and retainers would trigger
  `DUPLICATE_SUSPECTED` every month.

*Recommendation:* write the query as a self-join with a tolerance band. Set
the window below 28 days, or exclude pairs whose invoice numbers are
consecutive. It stays at `WARN` either way.

### 6.6 Exact duplicate detection is defeated by formatting

`INV-1001`, `INV 1001` and `inv1001` are three different keys to the unique
index. Resubmitting with changed punctuation is the cheapest way round the
control.

*Recommendation:* add a generated `invoice_number_normalised` column
(uppercase, alphanumeric only) and put the unique index on that.

There is a related problem for spec 03. An invoice whose supplier is
unresolved sits outside the index. When a reviewer later assigns the
supplier, the update can violate the index. Spec 03 has to catch that and
raise `DUPLICATE_EXACT`, not return a 500.

### 6.7 Invoices in `EXCEPTION` hold no receipt capacity

Only `MATCHED` and `POSTED` invoices count as billed. Suppose invoice A
blocks on a price variance, invoice B for the same goods then matches, and a
reviewer clears A. A is now over-received. This is first matched, first
served. It is defensible, but it should be stated in the ADR, and the
reviewer UI should show why A's status changed.

### 6.8 Replacing open exceptions changes their ids (adopted)

`exception_id` is a `bigserial`. If a re-run deletes and reinserts its open
rows, every id changes. Any spec 03 audit entry or reviewer note pointing at
an old id then refers to nothing.

*Recommendation:* give each exception a stable key, upsert on it, and
resolve the rows a re-run no longer raises.

*As adopted* (5.5): the key first proposed,
`(invoice_id, invoice_line_id, exception_type, source)`, is not unique
enough. One invoice can have several header `LOW_CONFIDENCE` rows, one per
field, and several `DUPLICATE_SUSPECTED` rows, one per earlier invoice.
The adopted key is an explicit `exception_key` column that names the line,
field or other invoice the exception is about.

### 6.9 The benchmark compares per-invoice calls (adopted)

The query is parameterised on one `invoice_id`. At 10,000 lines, the
benchmark mostly measures round trips on both sides. It does not measure
set-based execution, which is the claim the spec wants to demonstrate.

*Recommendation:* make the statement take an array of invoice ids. Compute
cumulative billing inside the batch with a window sum ordered by match
order, so invoices in one batch see each other. Matching a single invoice
becomes the one-element case. This is the harder query to write, and it is
the one that proves the point.

### 6.10 The SKU on the invoice is the supplier's code

An invoice carries the supplier's part number. A PO often carries the
buyer's internal item code. If `po_lines.sku` is the internal code, rung 1
rarely fires in practice.

*Recommendation:* state in the schema comment that `po_lines.sku` holds the
supplier part number, and make the seed data follow that.

### 6.11 Header-only confidence relies on the arithmetic checks

Decided: `LOW_CONFIDENCE` applies to header fields only. That is sound,
because a misread quantity or price usually breaks `LINE_TOTAL_MISMATCH` or
a variance check anyway. The gap is a misread that stays self-consistent,
for example quantity and line total both read low. It passes silently.
Accept this and record it in the ADR.

---

## 7. Open questions

Settled on 2026-09-29: tax reconciles against the rate in effect on the
invoice date, and the Invoice Parser field names in 5.1 are confirmed.

1. **Tax ID pattern.** Spec 02 section 8 requires the GST registration
   format to be checked against current IRAS guidance by a person, not
   generated. Until then, `tax_id_pattern` is `null` and `TAX_ID_INVALID`
   is off. Boot logs a warning.
2. **Reopening resolved exceptions.** `WAIVED` rows never reopen. Should a
   row a reviewer marked `RESOLVED` reopen when a re-run raises the same
   key again? The proposed answer is yes, because the underlying problem is
   evidently still there.
3. **Values still to set:** the `currency_overrides` entries, and the
   `line_matching` threshold and margin.
4. **Remaining challenges:** 6.2 to 6.7, 6.10 and 6.11.

---

## 8. Glossary of exception types

| Type | Severity | Trigger |
| --- | --- | --- |
| `LOW_CONFIDENCE` | BLOCK | Header field confidence below threshold, or field missing |
| `SUPPLIER_UNRESOLVED` | BLOCK | No confident supplier match |
| `NO_PO_REFERENCE` | BLOCK | No PO number. Goes to review for manual PO assignment |
| `PO_NOT_FOUND` | BLOCK | PO number unknown or cancelled |
| `NO_PO_MATCH` | BLOCK | No PO line matched by the ladder |
| `NO_GOODS_RECEIPT` | BLOCK | Nothing received against the PO line |
| `OVER_RECEIPT` | BLOCK | Cumulative billed exceeds received |
| `OVER_ORDER` | BLOCK | Cumulative billed exceeds ordered. New |
| `PRICE_VARIANCE` | BLOCK | Unit price outside tolerance, evaluated per line |
| `QUANTITY_VARIANCE` | WARN | Quantity differs from ordered but is within receipt |
| `LINE_TOTAL_MISMATCH` | BLOCK | Line total differs from quantity times price |
| `HEADER_TOTAL_MISMATCH` | BLOCK | Lines do not sum to net amount |
| `TAX_MISMATCH` | BLOCK | Non-zero tax does not reconcile to the rate |
| `TAX_ID_INVALID` | WARN | Tax ID fails the format check. Off until 7.1 is settled |
| `CURRENCY_MISMATCH` | BLOCK | Invoice, PO and supplier currencies disagree |
| `DATE_INVALID` | WARN | Dated before the PO or in the future |
| `DUPLICATE_EXACT` | BLOCK | Same supplier and invoice number as an existing invoice |
| `DUPLICATE_SUSPECTED` | WARN | Near-duplicate rule fired |

Severities can be overridden in `rules.yaml` under `severity_overrides`.
