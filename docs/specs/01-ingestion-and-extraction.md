# Spec 01: Ingestion and Extraction

**Module:** `src/ingestion`, `src/extraction`
**Week:** 1
**Depends on:** nothing
**Blocks:** Spec 02 (matching engine)

---

## 1. Goal

An invoice sent to a monitored Gmail address ends up as a validated,
schema-conformant extraction row in Postgres, with no manual step, and with
every field's confidence score preserved.

Matching, review and posting are out of scope. This module ends at persisted
extraction data.

---

## 2. Definition of done

All of the following must be true:

1. Forwarding a PDF invoice to the monitored address results in a row in
   `extraction_results` within 3 minutes, unattended.
2. Replaying the same Pub/Sub message three times produces exactly one
   `documents` row, one `extraction_jobs` row and one `extraction_results` row.
3. A Pub/Sub push request without a valid OIDC token returns 401 and creates no
   rows.
4. `REPLAY_FIXTURES=true` runs the full path from document to persisted
   extraction with no network calls, and the test suite passes with networking
   disabled.
5. A Postman collection in `docs/postman/` exercises every endpoint in this
   module plus the Document AI calls, with saved example responses.
6. Every extraction response validates against
   `src/extraction/schemas/invoice_extraction.schema.json`, and a deliberately
   malformed response fails validation with a readable error.
7. At least 15 varied fixtures are committed, including at least one scanned
   document, one multi-page document, one non-English document and one
   deliberately poor quality scan.

---

## 3. Flow

```
Gmail (label: ap-inbox)
  │  users.watch() → Pub/Sub topic gmail-notifications
  ▼
POST /internal/gmail/notify          [Cloud Run]
  │  verify OIDC → fetch history → for each attachment:
  │    write to GCS gs://<bucket>/inbox/{document_id}
  │    insert documents row
  │    start Document AI batch operation
  │    insert extraction_jobs row (status=RUNNING)
  ▼
Document AI batchProcessDocuments (async, long-running operation)
  │  output written to gs://<bucket>/extractions/{job_id}/
  ▼
Cloud Storage finalize event → Eventarc → Pub/Sub topic extraction-complete
  ▼
POST /internal/extraction/complete    [Cloud Run]
     verify OIDC → read output JSON → validate against schema
     → insert extraction_results + extraction_fields
     → mark job SUCCEEDED
```

There is also a manual path for development: `POST /v1/documents` accepts a
file upload directly and joins the flow at the GCS write. This is the endpoint
the Postman collection exercises.

---

## 4. Endpoints

### `POST /v1/documents`

Accepts `multipart/form-data` with a single `file` field.

Returns `202 Accepted` with `Location: /v1/documents/{document_id}`:

```json
{
  "document_id": "01JQ8...",
  "status": "ACCEPTED",
  "received_at": "2026-09-19T04:12:33Z"
}
```

Rejects with `415` for unsupported types, `413` above 20 MB.
Accepted types: `application/pdf`, `image/png`, `image/jpeg`, `image/tiff`.

### `GET /v1/documents/{document_id}`

Returns current status and, once available, the extraction result.
Status is one of `ACCEPTED`, `EXTRACTING`, `EXTRACTED`, `FAILED`.

### `POST /internal/gmail/notify`

Pub/Sub push target. Verifies the OIDC bearer token, audience and issuer before
reading the body. Returns 200 within 1 second in all cases, including failure,
so Pub/Sub does not redeliver on a poison message. Failures are recorded and
routed to the dead letter topic by nack only for transient errors.

### `POST /internal/extraction/complete`

Pub/Sub push target for extraction completion. Same verification rules.

---

## 5. Database schema

```sql
create table documents (
    document_id      text primary key,
    source           text not null check (source in ('GMAIL','UPLOAD')),
    source_ref       text,
    gcs_uri          text not null,
    content_hash     text not null,
    media_type       text not null,
    byte_size        bigint not null,
    received_at      timestamptz not null,
    created_at       timestamptz not null default now()
);

-- Content-level deduplication: the same PDF forwarded twice is one document.
create unique index documents_content_hash_uidx on documents (content_hash);

create table extraction_jobs (
    job_id           text primary key,
    document_id      text not null references documents (document_id),
    processor        text not null,
    operation_name   text,
    status           text not null
                     check (status in ('RUNNING','SUCCEEDED','FAILED')),
    attempt          int  not null default 1,
    error_code       text,
    error_detail     text,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    created_at       timestamptz not null default now()
);

create unique index extraction_jobs_doc_attempt_uidx
    on extraction_jobs (document_id, attempt);

create table extraction_results (
    extraction_id    text primary key,
    job_id           text not null references extraction_jobs (job_id),
    document_id      text not null references documents (document_id),
    supplier_name    text,
    supplier_tax_id  text,
    invoice_number   text,
    invoice_date     date,
    due_date         date,
    currency         char(3),
    net_amount       numeric(18,4),
    tax_amount       numeric(18,4),
    total_amount     numeric(18,4),
    raw_payload      jsonb not null,
    created_at       timestamptz not null default now()
);

create unique index extraction_results_job_uidx
    on extraction_results (job_id);

-- One row per extracted field, including line item fields, so confidence is
-- queryable. This table is what Spec 02 thresholds against and what the
-- week 4 analytics read for per-field exception rates.
create table extraction_fields (
    extraction_field_id bigserial primary key,
    extraction_id    text not null references extraction_results (extraction_id),
    field_path       text not null,
    field_value      text,
    confidence       numeric(5,4),
    page_number      int,
    created_at       timestamptz not null default now()
);

create index extraction_fields_extraction_idx
    on extraction_fields (extraction_id);
create index extraction_fields_confidence_idx
    on extraction_fields (confidence);

-- Message-level idempotency. Insert before processing; conflict means the
-- message has already been handled and the handler returns 200 immediately.
create table processed_events (
    message_id       text primary key,
    handler          text not null,
    processed_at     timestamptz not null default now()
);
```

Two deduplication layers are deliberate and both are required. `processed_events`
catches Pub/Sub redelivery of the same message. `documents.content_hash`
catches the same invoice arriving as two different messages, for example when a
supplier emails it twice or copies two addresses.

---

## 6. Extraction schema

Write `invoice_extraction.schema.json` as JSON Schema draft 2020-12. It must:

- Require `invoice_number`, `total_amount`, `currency`, `line_items`.
- Type `line_items` as an array of objects with `description`, `quantity`,
  `unit_price`, `line_total`, each with an accompanying `confidence`.
- Constrain `currency` to a three-letter uppercase pattern.
- Constrain every `confidence` to `0 <= x <= 1`.
- Allow `null` explicitly where Document AI may omit a field. Do not conflate a
  missing key with a null value; the distinction matters in Spec 02.

Validation failures raise `ExtractionSchemaError` with the JSON Pointer of the
offending node, and the job is marked `FAILED` with the pointer in
`error_detail`. Do not partially persist a failed extraction.

---

## 7. Behaviours to get right

**Acknowledgement timing.** Both internal handlers must return within one
second. All work beyond token verification, the `processed_events` insert and
the raw payload write happens after the response is committed or in a separate
handler.

**Retries.** Document AI calls retry on 429, 500, 502, 503 and 504 with
exponential backoff and full jitter, five attempts, first delay 1 second, cap
32 seconds. Never retry on 400 or 403.

**Dead letter.** Configure a dead letter topic on both subscriptions with
`maxDeliveryAttempts: 5`. Add an endpoint that lists dead-lettered messages so
they are visible rather than silently lost.

**Fixtures.** `DocumentAIClient` has two implementations behind one protocol.
The live one writes every successful response to
`fixtures/extractions/{content_hash}.json` before returning. The fixture one
reads by content hash and raises a clear error when a fixture is missing.
Selection is by `REPLAY_FIXTURES`, never by code branch at the call site.

---

## 8. Tests

Mandatory:

- Same message ID delivered three times: one document, one job, one result.
- Same file content arriving under two message IDs: one document row.
- Missing or invalid OIDC token: 401, no rows.
- Malformed extraction payload: job `FAILED`, pointer recorded, no
  `extraction_results` row.
- Document AI returning 503 twice then 200: one result, three attempts logged.
- Full pipeline in fixtures mode with networking disabled.
- Handler returns in under one second when the downstream publish is slow.

---

## 9. Manual setup, done by me before you start

Claude Code cannot do any of these. They are browser work.

- [ ] Create GCP project, region `asia-southeast1`
- [ ] Set a budget alert at USD 50
- [ ] Enable Document AI, Cloud Storage, Pub/Sub, Eventarc, Cloud Run APIs
- [ ] Create an Invoice Parser processor, record its ID
- [ ] Create the GCS bucket with `inbox/` and `extractions/` prefixes
- [ ] Configure the OAuth consent screen, add Gmail scope
  `gmail.readonly`
- [ ] Create a Gmail label `ap-inbox` and a filter routing invoices to it
- [ ] Create a Neon project, record the connection string
- [ ] Create the Pub/Sub topics and dead letter topics
- [ ] Install ngrok for local webhook testing
- [ ] Populate `.env` from `.env.example`

---

## 10. Deliverables

- Working code for both packages
- Alembic migration creating the schema above
- `invoice_extraction.schema.json` plus a validator with tests
- Postman collection with saved examples, committed
- 15+ fixtures
- `docs/specs/01-notes.md` recording what went wrong, written by me

---

## 11. Decisions I have not made yet

Raise these rather than assuming. Each changes the design.

1. Should a Gmail message with three attachments create three documents or one
   document with three parts? Leaning towards three, since AP invoices are
   independent, but line item grouping may argue otherwise.
2. Should `POST /v1/documents` deduplicate by content hash synchronously and
   return the existing `document_id` with 200, or always return 202 and
   deduplicate downstream?
3. Whether to store the full Document AI response in `raw_payload` or a trimmed
   version. Full is better for debugging and worse for row size. Default to
   full until it causes a problem.
