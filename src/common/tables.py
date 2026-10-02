"""SQLAlchemy Core table definitions.

Core only. Rule in CLAUDE.md section 2: no ORM relationship mapping. These
objects exist so queries are typed and so Alembic has a single source of
truth for the schema, not so rows become objects.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

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
    # Migration 0003. Spec 02 resolves the purchase order from it.
    Column("po_number", Text),
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

# --------------------------------------------------------------------------
# Spec 02: matching. Migration 0002 is the source of the DDL, including the
# normalisation functions and partial indexes; these objects mirror its
# columns so queries are typed. tests/integration/test_matching_schema.py
# fails if the two drift apart.
# --------------------------------------------------------------------------

suppliers = Table(
    "suppliers",
    metadata,
    Column("supplier_id", Text, primary_key=True),
    Column("legal_name", Text, nullable=False),
    Column("trading_names", ARRAY(Text), nullable=False, server_default="{}"),
    Column("tax_id", Text),
    Column("currency", String(3), nullable=False),
    Column("payment_terms_days", Integer, nullable=False, server_default="30"),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

purchase_orders = Table(
    "purchase_orders",
    metadata,
    Column("po_id", Text, primary_key=True),
    Column("po_number", Text, nullable=False, unique=True),
    Column(
        "supplier_id",
        Text,
        ForeignKey("suppliers.supplier_id"),
        nullable=False,
    ),
    Column("currency", String(3), nullable=False),
    Column("po_date", Date, nullable=False),
    Column("status", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

po_lines = Table(
    "po_lines",
    metadata,
    Column("po_line_id", Text, primary_key=True),
    Column("po_id", Text, ForeignKey("purchase_orders.po_id"), nullable=False),
    Column("line_number", Integer, nullable=False),
    Column("sku", Text),
    Column("description", Text, nullable=False),
    Column("quantity_ordered", Numeric(18, 4), nullable=False),
    Column("unit_price", Numeric(18, 4), nullable=False),
    Column("unit_of_measure", Text, nullable=False, server_default="EA"),
    Column("match_type", Text, nullable=False, server_default="THREE_WAY"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

goods_receipts = Table(
    "goods_receipts",
    metadata,
    Column("gr_id", Text, primary_key=True),
    Column("gr_number", Text, nullable=False, unique=True),
    Column("po_id", Text, ForeignKey("purchase_orders.po_id"), nullable=False),
    Column("received_date", Date, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

goods_receipt_lines = Table(
    "goods_receipt_lines",
    metadata,
    Column("gr_line_id", Text, primary_key=True),
    Column("gr_id", Text, ForeignKey("goods_receipts.gr_id"), nullable=False),
    Column(
        "po_line_id",
        Text,
        ForeignKey("po_lines.po_line_id"),
        nullable=False,
    ),
    Column("quantity_received", Numeric(18, 4), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

invoices = Table(
    "invoices",
    metadata,
    Column("invoice_id", Text, primary_key=True),
    Column(
        "extraction_id",
        Text,
        ForeignKey("extraction_results.extraction_id"),
        nullable=False,
    ),
    Column("supplier_id", Text, ForeignKey("suppliers.supplier_id")),
    Column("supplier_resolution_method", Text),
    Column("supplier_resolution_score", Numeric(5, 4)),
    # Migration 0005: the detail of SUPPLIER_UNRESOLVED.
    Column("supplier_resolution_note", Text),
    Column("po_number", Text),
    Column("po_id", Text, ForeignKey("purchase_orders.po_id")),
    # Migration 0005: the detail of PO_NOT_FOUND.
    Column("po_resolution_note", Text),
    Column("invoice_number", Text),
    Column("invoice_date", Date),
    Column("due_date", Date),
    Column("currency", String(3)),
    Column("net_amount", Numeric(18, 4)),
    Column("tax_amount", Numeric(18, 4)),
    Column("total_amount", Numeric(18, 4)),
    Column("match_status", Text, nullable=False, server_default="PENDING"),
    Column("matched_at", DateTime(timezone=True)),
    Column("duplicate_of_invoice_id", Text, ForeignKey("invoices.invoice_id")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

invoice_lines = Table(
    "invoice_lines",
    metadata,
    Column("invoice_line_id", Text, primary_key=True),
    Column("invoice_id", Text, ForeignKey("invoices.invoice_id"), nullable=False),
    Column("line_number", Integer, nullable=False),
    Column("description", Text),
    Column("sku", Text),
    Column("quantity", Numeric(18, 4)),
    Column("unit_price", Numeric(18, 4)),
    Column("line_total", Numeric(18, 4)),
    Column("po_line_id", Text, ForeignKey("po_lines.po_line_id")),
    Column("match_method", Text),
    Column("match_score", Numeric(5, 4)),
    Column("suggested_po_line_id", Text, ForeignKey("po_lines.po_line_id")),
    Column("match_status", Text, nullable=False, server_default="PENDING"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

exceptions = Table(
    "exceptions",
    metadata,
    Column("exception_id", BigInteger, primary_key=True, autoincrement=True),
    Column("invoice_id", Text, ForeignKey("invoices.invoice_id"), nullable=False),
    Column("invoice_line_id", Text, ForeignKey("invoice_lines.invoice_line_id")),
    Column("exception_key", Text, nullable=False),
    Column("exception_type", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("source", Text, nullable=False, server_default="MATCHER"),
    Column("field_path", Text),
    Column("expected_value", Text),
    Column("actual_value", Text),
    Column("variance_amount", Numeric(18, 4)),
    Column("variance_pct", Numeric(9, 4)),
    Column("detail", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="OPEN"),
    Column("resolved_by", Text),
    Column("resolved_at", DateTime(timezone=True)),
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
    UniqueConstraint("invoice_id", "exception_key", name="exceptions_invoice_key_uidx"),
)
