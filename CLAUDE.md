# CLAUDE.md

Operating instructions for Claude Code working in this repository.
Read this file fully before making any change.

---

## 1. What this project is

`ap-autopilot` is an accounts payable automation pipeline. Supplier invoices
arrive by email, are extracted by Google Document AI, matched against purchase
orders and goods receipts, routed to a human reviewer when confidence or
business rules demand it, and posted to Xero as bills.

It is a public portfolio project. Code quality, test coverage and readability
matter more than feature count. A small system that demonstrably works beats a
large one that mostly does.

**Domain terms used throughout:**

| Term | Meaning |
| --- | --- |
| Three-way match | Invoice line reconciled against a PO line and a goods receipt line |
| Exception | A line or document that failed a rule and requires human review |
| STP rate | Straight-through-processing rate: share of invoices posted with no human touch |
| Touch time | Elapsed time an exception spends in the review queue |

---

## 2. Stack

- **Language:** Python 3.12. Type hints on all function signatures.
- **API framework:** FastAPI. Pydantic v2 for all request and response models.
- **Database:** PostgreSQL 16 on Neon. SQLAlchemy Core for queries, Alembic for
  migrations. No ORM relationship mapping; write explicit SQL for anything
  non-trivial.
- **Extraction:** Google Document AI, Invoice Parser processor, async
  `batchProcessDocuments`.
- **Messaging:** Google Pub/Sub. Push subscriptions to Cloud Run endpoints.
- **Ingestion:** Gmail API `users.watch()` to Pub/Sub.
- **Destination:** Xero Accounting API, OAuth 2.0, `ACCPAY` invoices.
- **Analytics:** dbt-core against Postgres. Looker Studio for presentation.
- **Reviewer UI:** React 18, Vite, TypeScript, Tailwind.
- **Agent interface:** MCP server exposing read-only pipeline queries.
- **Package management:** `uv`. Do not use pip or poetry.
- **Testing:** pytest, pytest-asyncio, `respx` for HTTP mocking.
- **Linting:** ruff for lint and format. mypy in strict mode on `src/`.

Do not introduce a new dependency without saying why in the commit message.

---

## 3. Repository layout

```
ap-autopilot/
  src/
    ingestion/        Gmail watch handler, GCS upload, job creation
    extraction/       Document AI client, completion handler
    matching/         Three-way match engine, rules loader
    review/           Exception queue API, audit log
    posting/          Xero client, OAuth token store, bill creation
    mcp/              MCP server
    common/           Config, logging, retry wrapper, DB session, errors
  sql/
    README.md         Schema diagram and annotated analytical queries
    schema/           Alembic migrations
    analysis/         Standalone analytical SQL, one file per question
  dbt/
    models/staging/
    models/marts/
  ui/                 React reviewer
  fixtures/           Cached extraction responses, synthetic source documents
  tests/
  docs/
    specs/            Module specs. Read the relevant one before starting work.
    adr/              Architecture decision records
  config/
    rules.yaml        Matching rules, thresholds, tolerances
```

---

## 4. Deployment split

The GCP free trial expires after 90 days. The demo must outlive it.

| Component | Host | Reason |
| --- | --- | --- |
| Document AI, GCS, Pub/Sub | GCP `asia-southeast1` | Only available there |
| Reviewer UI, pipeline API, MCP server | DigitalOcean | Long-lived credit |
| Postgres | Neon free tier | Persistent, no credit burn |
| dbt | Runs locally, commits artefacts | Free |

Never assume a component can reach another over a private network. All
inter-service calls are authenticated HTTPS.

---

## 5. Non-negotiable rules

**5.1 Every outbound call goes through `common.http.request_with_retry`.**
No bare `httpx` calls in feature code. The wrapper owns timeouts, exponential
backoff with jitter, and retry-on status classification. Xero allows 60 calls
per minute and 5 concurrent; the wrapper enforces both.

**5.2 Every inbound webhook acknowledges before it processes.**
Verify the signature, persist the raw payload, publish to Pub/Sub, return 200.
Xero disables webhook endpoints that do not respond within 5 seconds. No
business logic runs inside a webhook handler.

**5.3 Every message handler is idempotent.**
Pub/Sub delivery is at-least-once. Duplicate suppression is enforced in
Postgres with a unique constraint and `ON CONFLICT DO NOTHING`, not with
in-memory state. Assume every handler will receive every message twice.

**5.4 `REPLAY_FIXTURES=true` must produce a full working demo with zero
external calls.** Every external client has a fixture-backed implementation
selected by config. This is not a test-only concern; it is how the public demo
survives credit expiry. If a change breaks fixtures mode, the change is
incomplete.

**5.5 No secrets in the repository.** Config comes from environment variables
via `common.config.Settings`. `.env.example` lists every variable with a
placeholder. If you add a variable, update that file in the same commit.

**5.6 No real supplier data.** Everything in `fixtures/` is synthetic. Never
commit a real GST registration number, bank detail or supplier name.

**5.7 Money is `Decimal`, never `float`.** Store as `numeric(18,4)` in
Postgres. Currency is an explicit column, never inferred.

**5.8 Errors are typed.** Raise from the taxonomy in `common.errors`. Never
raise bare `Exception`. Never swallow an exception without logging it with
structured context.

---

## 6. Data and SQL conventions

- Table and column names are `snake_case`, tables plural, primary keys `<table_singular>_id`.
- Every table has `created_at timestamptz not null default now()`.
- Timestamps are stored in UTC. Convert at presentation only.
- Business logic that operates on sets belongs in SQL, not in a Python loop.
  The three-way match is a query.
- Analytical queries live in `sql/analysis/` as standalone files with a comment
  header stating the question they answer. They are part of the portfolio and
  are written to be read.
- Migrations are forward-only. Never edit an applied migration.

---

## 7. Testing

- Every module needs unit tests for its rules and integration tests for its
  handlers.
- Three tests are mandatory and must exist before a module is considered done:
  1. Delivering the same Pub/Sub message three times produces exactly one
     database row and one downstream effect.
  2. A webhook with an invalid signature is rejected with 401 and no side
     effect.
  3. The full pipeline runs end to end in fixtures mode with no network access.
- Use `respx` to mock outbound HTTP. Do not call live APIs in tests.
- Tests must pass with no network connection available.

---

## 8. How to work with me

**Specs come first.** Each module has a spec in `docs/specs/`. Read it before
writing code. If the spec is ambiguous, ask rather than deciding silently. If
you believe the spec is wrong, say so before implementing it.

**Explain before implementing on the hard parts.** For anything touching
webhook acknowledgement timing, idempotency, OAuth token refresh under
concurrency, or Document AI async completion, present the design and its
failure modes first and wait for a response. Do not write the implementation in
the same turn.

**One module per session.** Do not start work on a module the current spec does
not cover.

**Stop when uncertain.** If an API's behaviour is unclear, say so rather than
guessing at field names. Check the live Postgres schema through the database
MCP server rather than assuming column names.

**Do not write the prose.** ADRs, the top-level README, SQL commentary and
anything intended for a human reader are written by me. Leave a TODO marker
instead.

---

## 9. Commits

- One logical change per commit. Do not batch unrelated changes.
- Format: `<area>: <imperative summary>`, for example
  `extraction: handle partial Document AI page failures`.
- The body explains why, not what. The diff already shows what.
- Commit bugs and their fixes as separate commits. The history is evidence of
  how the system was built and is read as part of the portfolio.
- Never force push to `main`.

---

## 10. Cost discipline

- Document AI is the only per-unit cost. Every extraction response is written
  to `fixtures/extractions/` on first call and replayed thereafter.
- Never call Document AI from a test.
- Never call Document AI in a loop over a directory without an explicit
  confirmed count.
- Before adding any GCP resource, state its expected monthly cost.

---

## 11. Out of scope

Do not build, suggest or scaffold: SAP integration of any kind, multi-tenancy,
user authentication beyond a single reviewer, payment execution, any ERP
connector other than Xero, or a mobile application.

---

## 12. Writing style for any documentation you do produce

British English. No em dashes. No puffery or marketing language. Short
sentences. If a docstring needs three paragraphs, the function needs splitting.
