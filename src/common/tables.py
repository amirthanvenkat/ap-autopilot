"""SQLAlchemy Core table definitions.

Core only. Rule in CLAUDE.md section 2: no ORM relationship mapping. These
objects exist so queries are typed and so Alembic has a single source of
truth for the schema, not so rows become objects.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

DOCUMENT_SOURCES = ("GMAIL", "UPLOAD")
JOB_STATUSES = ("RUNNING", "SUCCEEDED", "FAILED")
OUTBOX_STATUSES = ("PENDING", "LEASED", "DONE", "FAILED")

documents = Table(
    "documents",
    metadata,
    Column("document_id", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("source_ref", Text),
    Column("gcs_uri", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "source in ('GMAIL','UPLOAD')",
        name="documents_source_check",
    ),
)

# Content level deduplication: the same PDF forwarded twice is one document.
Index("documents_content_hash_uidx", documents.c.content_hash, unique=True)

# Provenance for a deduplicated document.
#
# Not one of the nine accepted changes. Added because documents.source_ref
# holds a single value, so when the same invoice arrives from two emails the
# second sender is lost, and an AP audit trail is expected to show every
# route by which a document reached the system.
document_sources = Table(
    "document_sources",
    metadata,
    Column("document_source_id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "document_id",
        Text,
        ForeignKey("documents.document_id"),
        nullable=False,
    ),
    Column("source", Text, nullable=False),
    Column("source_ref", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "source in ('GMAIL','UPLOAD')",
        name="document_sources_source_check",
    ),
    UniqueConstraint(
        "document_id",
        "source",
        "source_ref",
        name="document_sources_uidx",
    ),
)

extraction_jobs = Table(
    "extraction_jobs",
    metadata,
    Column("job_id", Text, primary_key=True),
    Column(
        "document_id",
        Text,
        ForeignKey("documents.document_id"),
        nullable=False,
    ),
    Column("processor", Text, nullable=False),
    Column("operation_name", Text),
    Column("status", Text, nullable=False),
    Column("attempt", Integer, nullable=False, server_default="1"),
    Column("error_code", Text),
    Column("error_detail", Text),
    Column(
        "started_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("finished_at", DateTime(timezone=True)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "status in ('RUNNING','SUCCEEDED','FAILED')",
        name="extraction_jobs_status_check",
    ),
)

Index(
    "extraction_jobs_doc_attempt_uidx",
    extraction_jobs.c.document_id,
    extraction_jobs.c.attempt,
    unique=True,
)
# The reaper scans for jobs stuck in RUNNING past their timeout.
Index(
    "extraction_jobs_status_started_idx",
    extraction_jobs.c.status,
    extraction_jobs.c.started_at,
)

extraction_results = Table(
    "extraction_results",
    metadata,
    Column("extraction_id", Text, primary_key=True),
    Column("job_id", Text, ForeignKey("extraction_jobs.job_id"), nullable=False),
    Column(
        "document_id",
        Text,
        ForeignKey("documents.document_id"),
        nullable=False,
    ),
    Column("supplier_name", Text),
    Column("supplier_tax_id", Text),
    Column("invoice_number", Text),
    Column("invoice_date", Date),
    Column("due_date", Date),
    Column("currency", String(3)),
    Column("net_amount", Numeric(18, 4)),
    Column("tax_amount", Numeric(18, 4)),
    Column("total_amount", Numeric(18, 4)),
    # Accepted change 6: the trimmed entity level payload lives here and the
    # untrimmed Document AI response stays in Cloud Storage.
    Column("raw_payload", JSONB, nullable=False),
    Column("raw_gcs_uri", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

Index(
    "extraction_results_job_uidx",
    extraction_results.c.job_id,
    unique=True,
)

# One row per extracted field, including line item fields, so confidence is
# queryable. Spec 02 thresholds against this table and the week 4 analytics
# read it for per field exception rates.
extraction_fields = Table(
    "extraction_fields",
    metadata,
    Column("extraction_field_id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "extraction_id",
        Text,
        ForeignKey("extraction_results.extraction_id"),
        nullable=False,
    ),
    # Accepted change 4: a JSON Pointer including array indices, so
    # /line_items/3/unit_price is distinct from /line_items/4/unit_price.
    Column("field_path", Text, nullable=False),
    Column("field_value", Text),
    Column("confidence", Numeric(5, 4)),
    Column("page_number", Integer),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Redundant given that results and fields are written in one transaction.
    # Present anyway: it turns a silent doubling of every per field analytic
    # into a loud constraint violation.
    UniqueConstraint(
        "extraction_id",
        "field_path",
        name="extraction_fields_path_uidx",
    ),
)

Index("extraction_fields_extraction_idx", extraction_fields.c.extraction_id)
Index("extraction_fields_confidence_idx", extraction_fields.c.confidence)

# Message level idempotency. Never committed on its own: the acceptance
# transaction writes this row and the outbox row together or neither.
processed_events = Table(
    "processed_events",
    metadata,
    Column("message_id", Text, primary_key=True),
    Column("handler", Text, nullable=False),
    Column(
        "processed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

# Accepted change 3. The unit of work a push handler commits before it
# returns 200, so no work depends on CPU time after the response.
ingestion_outbox = Table(
    "ingestion_outbox",
    metadata,
    Column("task_id", Text, primary_key=True),
    Column("handler", Text, nullable=False),
    Column(
        "message_id",
        Text,
        ForeignKey("processed_events.message_id"),
        nullable=False,
    ),
    Column("payload", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default="PENDING"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("leased_until", DateTime(timezone=True)),
    Column("last_error", Text),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "status in ('PENDING','LEASED','DONE','FAILED')",
        name="ingestion_outbox_status_check",
    ),
)

Index(
    "ingestion_outbox_claimable_idx",
    ingestion_outbox.c.status,
    ingestion_outbox.c.created_at,
    postgresql_where=ingestion_outbox.c.status.in_(("PENDING", "LEASED")),
)

# Gmail watch cursor and expiry.
#
# Required to implement the Gmail path at all: history.list needs a stored
# start id. The expiry column supports accepted change 8, the daily renewal
# job, since users.watch lapses after seven days without a word.
gmail_watch_state = Table(
    "gmail_watch_state",
    metadata,
    Column("email_address", Text, primary_key=True),
    Column("history_id", BigInteger, nullable=False),
    Column("watch_expires_at", DateTime(timezone=True)),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
