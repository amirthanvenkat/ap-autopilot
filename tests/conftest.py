"""Shared test fixtures.

Unit tests run with sockets disabled, so a test that quietly reaches the
network fails rather than passing slowly. Integration tests need a real
Postgres, because the design leans on data modifying CTEs, FOR UPDATE SKIP
LOCKED, advisory locks and jsonb, none of which have a useful stand-in.
Point TEST_DATABASE_URL at a scratch database to run them.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.common.config import Settings
from src.common.deps import Dependencies
from src.common.messaging import NullPublisher
from src.common.oidc import SharedSecretVerifier
from src.common.storage import LocalObjectStore
from src.extraction.docai import FixtureDocumentAIClient
from src.ingestion.gmail import FixtureGmailClient

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "fixtures"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
DEV_TOKEN = "test-dev-token"

_TABLES = (
    "extraction_fields",
    "extraction_results",
    "extraction_jobs",
    "document_sources",
    "documents",
    "ingestion_outbox",
    "processed_events",
    "gmail_watch_state",
)

requires_database = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason=(
        "set TEST_DATABASE_URL to a scratch Postgres database to run the "
        "integration tests"
    ),
)


class NetworkBlockedError(RuntimeError):
    """Raised when a test tries to open a socket."""


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable outbound sockets for the duration of a test."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise NetworkBlockedError("network access is disabled in this test")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def fixtures_settings(tmp_path: Path) -> Settings:
    """Settings for fixtures mode, writing objects into a scratch directory."""
    # Cached responses and Gmail fixtures are read from the committed set.
    # Object writes go to a scratch store, so tests never dirty fixtures/.
    del tmp_path
    return Settings(
        environment="test",
        replay_fixtures=True,
        fixtures_dir=FIXTURES_DIR,
        database_url=TEST_DATABASE_URL or "postgresql+psycopg://localhost/unused",
        oidc_dev_token=DEV_TOKEN,
        gcs_bucket="test-bucket",
        gmail_label="ap-inbox",
        outbox_lease_seconds=60,
        publish_timeout_seconds=0.25,
    )


@pytest.fixture
def object_store(tmp_path: Path) -> LocalObjectStore:
    """A scratch object store, so tests never write into fixtures/."""
    return LocalObjectStore(tmp_path / "gcs", bucket="test-bucket")


@pytest_asyncio.fixture
async def engine(fixtures_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """A connected engine against the scratch database, schema applied."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    url = TEST_DATABASE_URL
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    created = create_async_engine(url, poolclass=None)
    await _apply_schema(created)
    try:
        yield created
    finally:
        await created.dispose()


async def _apply_schema(engine: AsyncEngine) -> None:
    """Apply the Alembic migration, then clear every table."""
    from alembic import command
    from alembic.config import Config

    def _migrate() -> None:
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "sql" / "schema"))
        command.upgrade(config, "head")

    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text(
                    "select count(*) from information_schema.tables "
                    "where table_schema = 'public' and table_name = 'documents'"
                )
            )
        ).scalar_one()
    if not existing:
        import asyncio

        await asyncio.to_thread(_migrate)

    async with engine.begin() as conn:
        await conn.execute(
            text(f"truncate {', '.join(_TABLES)} restart identity cascade")
        )


@pytest.fixture
def deps(
    fixtures_settings: Settings,
    engine: AsyncEngine,
    object_store: LocalObjectStore,
) -> Dependencies:
    """Fixtures mode dependencies wired to the scratch database."""
    return Dependencies(
        settings=fixtures_settings,
        engine=engine,
        object_store=object_store,
        documentai=FixtureDocumentAIClient(fixtures_settings),
        gmail=FixtureGmailClient(fixtures_settings),
        publisher=NullPublisher(),
        verifier=SharedSecretVerifier(fixtures_settings),
    )


@pytest.fixture
def auth_header() -> dict[str, str]:
    """A valid bearer header for fixtures mode."""
    return {"Authorization": f"Bearer {DEV_TOKEN}"}


@pytest.fixture
def source_documents() -> list[dict[str, Any]]:
    """The fixture manifest, as a list of records."""
    import json

    return list(json.loads((FIXTURES_DIR / "manifest.json").read_text("utf-8")))


@pytest.fixture
def sample_document(source_documents: list[dict[str, Any]]) -> tuple[bytes, str, str]:
    """One PDF invoice: its bytes, media type and content hash."""
    record = next(
        item for item in source_documents if item["slug"] == "acme-office-supplies"
    )
    data = (FIXTURES_DIR / "source" / str(record["file"])).read_bytes()
    return data, str(record["media_type"]), str(record["content_hash"])


@pytest.fixture(autouse=True)
def _reset_engine_singleton() -> Iterator[None]:
    """Keep the process wide engine out of the tests' way."""
    from src.common import db

    db.set_engine(None)
    yield
    db.set_engine(None)
