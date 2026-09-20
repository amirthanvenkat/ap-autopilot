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
