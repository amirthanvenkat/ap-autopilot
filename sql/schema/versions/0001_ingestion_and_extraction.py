"""Ingestion and extraction schema.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table documents (
            document_id      text primary key,
            source           text not null
                             check (source in ('GMAIL','UPLOAD')),
            source_ref       text,
            gcs_uri          text not null,
            content_hash     text not null,
            media_type       text not null,
            byte_size        bigint not null,
            received_at      timestamptz not null,
            created_at       timestamptz not null default now()
        )
        """
    )
    # Content level deduplication: the same PDF forwarded twice is one
    # document.
    op.execute(
        "create unique index documents_content_hash_uidx "
        "on documents (content_hash)"
    )

    # Provenance for a deduplicated document. documents.source_ref holds one
    # value, so without this the second email to carry the same invoice is
    # lost, and an AP audit trail is expected to show every route in.
    op.execute(
        """
        create table document_sources (
            document_source_id bigserial primary key,
            document_id      text not null references documents (document_id),
            source           text not null
                             check (source in ('GMAIL','UPLOAD')),
            source_ref       text not null,
            received_at      timestamptz not null,
            created_at       timestamptz not null default now(),
            constraint document_sources_uidx
                unique (document_id, source, source_ref)
        )
        """
    )

    op.execute(
        """
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
        )
        """
    )
    op.execute(
        "create unique index extraction_jobs_doc_attempt_uidx "
        "on extraction_jobs (document_id, attempt)"
    )
    # The reaper scans for jobs stuck in RUNNING past their timeout. A failed
    # Document AI operation writes no output, so no finalize event fires and
    # nothing else would notice them.
    op.execute(
        "create index extraction_jobs_status_started_idx "
        "on extraction_jobs (status, started_at)"
    )

    op.execute(
        """
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
            raw_gcs_uri      text not null,
            created_at       timestamptz not null default now()
        )
        """
    )
    op.execute(
        "create unique index extraction_results_job_uidx "
        "on extraction_results (job_id)"
    )

    # One row per extracted field, including line item fields, so confidence
    # is queryable. Spec 02 thresholds against this table and the week 4
    # analytics read it for per field exception rates.
    #
    # field_path is a JSON Pointer including array indices, so
    # /line_items/3/unit_price is distinct from /line_items/4/unit_price and
    # the unique constraint below is meaningful. Results and fields are
    # written in one transaction, which makes that constraint redundant; it
    # is here anyway, because without it a replay that ever did split the
    # writes would double every per field analytic in silence.
    op.execute(
        """
        create table extraction_fields (
            extraction_field_id bigserial primary key,
            extraction_id    text not null
                             references extraction_results (extraction_id),
            field_path       text not null,
            field_value      text,
            confidence       numeric(5,4),
            page_number      int,
            created_at       timestamptz not null default now(),
            constraint extraction_fields_path_uidx
                unique (extraction_id, field_path)
        )
        """
    )
    op.execute(
        "create index extraction_fields_extraction_idx "
        "on extraction_fields (extraction_id)"
    )
    op.execute(
        "create index extraction_fields_confidence_idx "
        "on extraction_fields (confidence)"
    )

    # Message level idempotency. Never committed on its own: the acceptance
    # statement writes this row and the outbox row together or neither.
    op.execute(
        """
        create table processed_events (
            message_id       text primary key,
            handler          text not null,
            processed_at     timestamptz not null default now()
        )
        """
    )

    # The unit of work a push handler commits before returning 200, so that
    # no work depends on CPU time after the response. Cloud Run throttles
    # CPU once a response is returned.
    op.execute(
        """
        create table ingestion_outbox (
            task_id          text primary key,
            handler          text not null,
            message_id       text not null
                             references processed_events (message_id),
            payload          jsonb not null,
            status           text not null default 'PENDING'
                             check (status in ('PENDING','LEASED','DONE','FAILED')),
            attempts         int  not null default 0,
            leased_until     timestamptz,
            last_error       text,
            created_at       timestamptz not null default now(),
            updated_at       timestamptz not null default now()
        )
        """
    )
    op.execute(
        """
        create index ingestion_outbox_claimable_idx
            on ingestion_outbox (status, created_at)
         where status in ('PENDING','LEASED')
        """
    )

    # history.list needs a stored cursor, and users.watch lapses after seven
    # days without announcing it.
    op.execute(
        """
        create table gmail_watch_state (
            email_address    text primary key,
            history_id       bigint not null,
            watch_expires_at timestamptz,
            updated_at       timestamptz not null default now()
        )
        """
    )


def downgrade() -> None:
    for table in (
        "gmail_watch_state",
        "ingestion_outbox",
        "processed_events",
        "extraction_fields",
        "extraction_results",
        "extraction_jobs",
        "document_sources",
        "documents",
    ):
        op.execute(f"drop table if exists {table} cascade")
