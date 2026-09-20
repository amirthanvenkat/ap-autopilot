"""Client selection, fixture replay, the object store and token checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.common.config import Settings
from src.common.errors import (
    AuthenticationError,
    FixtureMissingError,
    NotFoundError,
)
from src.common.messaging import NullPublisher, PubSubPublisher, build_publisher
from src.common.oidc import (
    GoogleOidcVerifier,
    SharedSecretVerifier,
    build_verifier,
)
from src.common.storage import LocalObjectStore, build_object_store
from src.extraction.docai import (
    FixtureDocumentAIClient,
    build_document_ai_client,
)
from src.ingestion.gmail import FixtureGmailClient, build_gmail_client
from src.ingestion.service import inbox_object_name, output_prefix_for

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
TOKEN = "dev-token-value"


@pytest.fixture
def fixtures_mode() -> Settings:
    return Settings(
        environment="test",
        replay_fixtures=True,
        fixtures_dir=FIXTURES,
        oidc_dev_token=TOKEN,
        gcs_bucket="test-bucket",
    )


@pytest.fixture
def live_mode() -> Settings:
    return Settings(
        environment="test",
        replay_fixtures=False,
        gcp_project_id="proj",
        gcs_bucket="bucket",
        docai_processor_id="proc",
        oidc_audience="https://service.example.test",
        oidc_service_account="pusher@proj.iam.gserviceaccount.com",
    )


def test_fixtures_mode_selects_every_replay_client(fixtures_mode: Settings) -> None:
    """Selection is by configuration, never a branch at the call site."""
    assert isinstance(build_object_store(fixtures_mode), LocalObjectStore)
    assert isinstance(build_publisher(fixtures_mode), NullPublisher)
    assert isinstance(build_gmail_client(fixtures_mode), FixtureGmailClient)
    assert isinstance(build_verifier(fixtures_mode), SharedSecretVerifier)
    assert isinstance(build_document_ai_client(fixtures_mode), FixtureDocumentAIClient)


def test_live_mode_selects_live_clients(live_mode: Settings) -> None:
    assert isinstance(build_publisher(live_mode), PubSubPublisher)
    assert isinstance(build_verifier(live_mode), GoogleOidcVerifier)


async def test_missing_bearer_token_is_rejected(fixtures_mode: Settings) -> None:
    verifier = SharedSecretVerifier(fixtures_mode)
    with pytest.raises(AuthenticationError):
        await verifier.verify(None)


@pytest.mark.parametrize(
    "header",
    ["", "Basic abc", "Bearer", "Bearer ", "token-without-scheme", "Bearer wrong"],
)
async def test_bad_authorization_headers_are_rejected(
    fixtures_mode: Settings, header: str
) -> None:
    verifier = SharedSecretVerifier(fixtures_mode)
    with pytest.raises(AuthenticationError):
        await verifier.verify(header)


async def test_valid_token_is_accepted(fixtures_mode: Settings) -> None:
    verifier = SharedSecretVerifier(fixtures_mode)
    claims = await verifier.verify(f"Bearer {TOKEN}")
    assert claims.issuer == "fixtures"


async def test_google_verifier_rejects_before_certificates_are_primed(
    live_mode: Settings,
) -> None:
    """A cold instance must reject rather than fetch inside the request.

    A JWKS fetch on the critical path would make the latency budget depend
    on Google's availability. Pub/Sub redelivers, so rejecting is safe.
    """
    verifier = GoogleOidcVerifier(live_mode)
    with pytest.raises(AuthenticationError) as caught:
        await verifier.verify("Bearer anything")
    assert "certificates" in str(caught.value)


async def test_object_writes_are_create_if_absent(tmp_path: Path) -> None:
    """A redelivery writes identical bytes to an identical path.

    Keying the path on the content hash makes the second write a no-op
    rather than an orphaned object.
    """
    store = LocalObjectStore(tmp_path / "gcs", bucket="b")
    name = inbox_object_name("abc123", "application/pdf")
    first = await store.put_if_absent(name, b"original", content_type="application/pdf")
    second = await store.put_if_absent(name, b"IGNORED", content_type="application/pdf")
    assert first.uri == second.uri
    assert await store.get(name) == b"original"
    assert len(await store.list_prefix("inbox/")) == 1


async def test_object_name_cannot_escape_the_store(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "gcs", bucket="b")
    with pytest.raises(NotFoundError):
        await store.get("../../etc/passwd")


async def test_listing_a_prefix_finds_only_that_prefix(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "gcs", bucket="b")
    await store.put_if_absent(
        "extractions/job-1/out-0.json", b"{}", content_type="application/json"
    )
    await store.put_if_absent(
        "extractions/job-2/out-0.json", b"{}", content_type="application/json"
    )
    found = await store.list_prefix(output_prefix_for("job-1"))
    assert [item.name for item in found] == ["extractions/job-1/out-0.json"]


async def test_fixture_client_replays_by_content_hash(
    fixtures_mode: Settings,
) -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text("utf-8"))
    digest = str(manifest[0]["content_hash"])
    client = FixtureDocumentAIClient(fixtures_mode)
    handle = await client.start_batch(
        gcs_input_uri="gs://b/inbox/x.pdf",
        mime_type="application/pdf",
        output_prefix="extractions/job-1/",
        content_hash=digest,
    )
    assert handle.immediate is True
    assert handle.operation_name is None
    raw = await client.fetch_raw(
        output_prefix="extractions/job-1/", content_hash=digest
    )
    assert raw["entities"]


async def test_missing_fixture_fails_before_the_job_claims_to_be_running(
    fixtures_mode: Settings,
) -> None:
    """Fail at start_batch, not later.

    A job row that says RUNNING for an extraction that can never happen is
    worse than an immediate, readable error.
    """
    client = FixtureDocumentAIClient(fixtures_mode)
    with pytest.raises(FixtureMissingError) as caught:
        await client.start_batch(
            gcs_input_uri="gs://b/inbox/x.pdf",
            mime_type="application/pdf",
            output_prefix="extractions/job-1/",
            content_hash="0" * 64,
        )
    assert "0" * 64 in str(caught.value)


async def test_null_publisher_records_instead_of_sending(
    fixtures_mode: Settings,
) -> None:
    publisher = NullPublisher()
    assert await publisher.publish("topic", {"task_id": "t-1"}) is None
    assert publisher.published == [("topic", {"task_id": "t-1"})]


async def test_gmail_fixture_client_reads_history_and_attachments(
    fixtures_mode: Settings,
) -> None:
    client = FixtureGmailClient(fixtures_mode)
    page = await client.list_added_messages(
        start_history_id="1000", label_id="ap-inbox"
    )
    assert page.message_ids
    assert page.new_history_id == "1400"
    attachments = await client.list_attachments(page.message_ids[0])
    data = await client.get_attachment(
        message_id=page.message_ids[0], attachment_id=attachments[0].attachment_id
    )
    assert data.startswith(b"%PDF-")


async def test_gmail_resync_fixture_is_available(fixtures_mode: Settings) -> None:
    client = FixtureGmailClient(fixtures_mode)
    page = await client.resync(label_id="ap-inbox")
    assert page.message_ids
    assert page.new_history_id == "1500"
