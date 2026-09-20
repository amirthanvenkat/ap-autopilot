"""Authentication, acknowledgement timing and the upload endpoint."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from src.common.deps import Dependencies
from tests.conftest import requires_database
from tests.integration.conftest import count, seed_watch_cursor

pytestmark = requires_database

EMAIL = "ap@example.test"
PUSH_ENDPOINTS = ("/internal/gmail/notify", "/internal/extraction/complete")


def body(history_id: str = "2000") -> dict[str, Any]:
    return {
        "message": {
            "messageId": "msg-auth",
            "data": base64.b64encode(
                json.dumps({"emailAddress": EMAIL, "historyId": history_id}).encode()
            ).decode(),
            "attributes": {},
        },
        "subscription": "projects/p/subscriptions/s",
    }


@pytest.mark.parametrize("path", PUSH_ENDPOINTS)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer"},
    ],
    ids=["missing", "wrong", "not-bearer", "empty"],
)
async def test_bad_credentials_give_401_and_write_nothing(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    path: str,
    headers: dict[str, str],
) -> None:
    """Mandatory test. Verification happens before the body is read."""
    response = await client.post(path, json=body(), headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "AUTHENTICATION_ERROR"
    assert await count(engine, "processed_events") == 0
    assert await count(engine, "ingestion_outbox") == 0
    assert await count(engine, "documents") == 0


@pytest.mark.parametrize("path", PUSH_ENDPOINTS)
async def test_a_valid_token_is_accepted(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    auth_header: dict[str, str],
    path: str,
) -> None:
    await seed_watch_cursor(engine, EMAIL, "2000")
    response = await client.post(path, json=body(), headers=auth_header)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


async def test_handler_returns_quickly_when_the_publish_hangs(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    deps: Dependencies,
    auth_header: dict[str, str],
) -> None:
    """Mandatory test: under one second with a slow downstream publish.

    The publish sits outside the acceptance transaction and is bounded by
    its own timeout. The outbox row is already committed, so losing the
    nudge costs the sweep interval and nothing else.
    """
    await seed_watch_cursor(engine, EMAIL, "2000")

    async def hangs(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(30)

    object.__setattr__(deps.publisher, "publish", hangs)

    started = time.perf_counter()
    response = await client.post(
        "/internal/gmail/notify", json=body(), headers=auth_header
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 1.0
    # The work is durable even though nothing was ever published.
    assert await count(engine, "ingestion_outbox", "status = 'PENDING'") == 1


async def test_upload_returns_202_for_a_new_document(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    sample_document: tuple[bytes, str, str],
) -> None:
    data, media_type, digest = sample_document
    response = await client.post(
        "/v1/documents",
        files={"file": ("invoice.pdf", data, media_type)},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["duplicate"] is False
    assert payload["content_hash"] == digest
    assert response.headers["Location"] == f"/v1/documents/{payload['document_id']}"
    assert await count(engine, "documents") == 1


async def test_upload_returns_200_and_the_existing_id_for_a_duplicate(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    sample_document: tuple[bytes, str, str],
) -> None:
    """Section 11 question 2, decided as synchronous deduplication.

    202 asserts that something new was created. For a duplicate that is
    false, and this is the endpoint a human drives from Postman while
    watching, so the status code should not mislead.
    """
    data, media_type, _digest = sample_document
    first = await client.post(
        "/v1/documents", files={"file": ("invoice.pdf", data, media_type)}
    )
    second = await client.post(
        "/v1/documents", files={"file": ("same-invoice.pdf", data, media_type)}
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["document_id"] == first.json()["document_id"]
    assert await count(engine, "documents") == 1


async def test_upload_rejects_an_unsupported_type(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    response = await client.post(
        "/v1/documents",
        files={"file": ("notes.txt", b"just some text", "text/plain")},
    )
    assert response.status_code == 415
    assert await count(engine, "documents") == 0


async def test_upload_rejects_a_file_above_the_size_cap(
    client: httpx.AsyncClient, engine: AsyncEngine, deps: Dependencies
) -> None:
    oversized = b"%PDF-1.4\n" + b"0" * (deps.settings.max_upload_bytes + 1)
    response = await client.post(
        "/v1/documents",
        files={"file": ("huge.pdf", oversized, "application/pdf")},
    )
    assert response.status_code == 413
    assert await count(engine, "documents") == 0


async def test_document_status_is_derived_from_the_latest_job(
    client: httpx.AsyncClient,
    deps: Dependencies,
    sample_document: tuple[bytes, str, str],
) -> None:
    """There is no status column, so the two can never disagree."""
    from src.common.runner import sweep

    data, media_type, _digest = sample_document
    created = await client.post(
        "/v1/documents", files={"file": ("invoice.pdf", data, media_type)}
    )
    document_id = created.json()["document_id"]

    before = await client.get(f"/v1/documents/{document_id}")
    assert before.json()["status"] == "EXTRACTING"

    await sweep(deps)

    after = await client.get(f"/v1/documents/{document_id}")
    payload = after.json()
    assert payload["status"] == "EXTRACTED"
    assert payload["extraction"]["invoice_number"] == "INV-1001"
    assert payload["extraction"]["currency"] == "SGD"
    assert len(payload["extraction"]["fields"]) > 10


async def test_unknown_document_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/documents/does-not-exist")
    assert response.status_code == 404


async def test_health_reports_the_database_and_the_mode(
    client: httpx.AsyncClient,
) -> None:
    """Also the Neon keepalive target, so it must touch the database."""
    response = await client.get("/health")
    payload = response.json()
    assert response.status_code == 200
    assert payload["database"] == "ok"
    assert payload["fixtures_mode"] is True


@pytest.mark.parametrize(
    "path", ["/internal/sweep", "/internal/outbox", "/internal/deadletter"]
)
async def test_operational_endpoints_require_a_token(
    client: httpx.AsyncClient, path: str
) -> None:
    method = client.post if path == "/internal/sweep" else client.get
    response = await method(path)
    assert response.status_code == 401
