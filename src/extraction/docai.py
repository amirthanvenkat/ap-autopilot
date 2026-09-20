"""Document AI access behind one protocol.

Two implementations, chosen by REPLAY_FIXTURES and never by a branch at the
call site. The live one caches every successful response under
fixtures/extractions/{content_hash}.json before returning it, so the first
live call for a document is the only one that is ever paid for.

Batch processing is asynchronous, so the protocol has three parts: start the
operation, ask whether it finished, and read what it wrote.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.common.config import Settings, get_settings
from src.common.errors import FixtureMissingError, NotFoundError, UpstreamRejectedError
from src.common.gcp import ApplicationDefaultTokenProvider, TokenProvider, auth_headers
from src.common.http import request_with_retry
from src.common.logging import get_logger
from src.common.storage import ObjectStore

log = get_logger(__name__)


@dataclass(frozen=True)
class BatchHandle:
    """The result of asking Document AI to start work."""

    operation_name: str | None
    output_prefix: str
    # True when the result is already available, which is how fixtures mode
    # works: there is no operation to wait for.
    immediate: bool


@dataclass(frozen=True)
class OperationStatus:
    """Terminal state of a long running operation."""

    done: bool
    error_code: str | None = None
    error_message: str | None = None

    @property
    def failed(self) -> bool:
        return self.done and self.error_code is not None


class DocumentAIClient(Protocol):
    """Starts extractions and reads their output."""

    async def start_batch(
        self,
        *,
        gcs_input_uri: str,
        mime_type: str,
        output_prefix: str,
        content_hash: str,
    ) -> BatchHandle: ...

    async def operation_status(self, operation_name: str) -> OperationStatus: ...

    async def fetch_raw(
        self, *, output_prefix: str, content_hash: str
    ) -> dict[str, Any]: ...

    async def find_operation_for_prefix(self, output_prefix: str) -> str | None: ...


def cache_path(settings: Settings, content_hash: str) -> Path:
    """Where a cached raw response lives."""
    return settings.fixtures_dir / "extractions" / f"{content_hash}.json"


class LiveDocumentAIClient:
    """Calls Document AI over its REST API and caches what comes back."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        object_store: ObjectStore,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._store = object_store
        self._tokens = token_provider or ApplicationDefaultTokenProvider()

    async def start_batch(
        self,
        *,
        gcs_input_uri: str,
        mime_type: str,
        output_prefix: str,
        content_hash: str,
    ) -> BatchHandle:
        del content_hash
        url = (
            f"{self._settings.docai_endpoint}/v1/"
            f"{self._settings.docai_processor_path}:batchProcess"
        )
        body = {
            "inputDocuments": {
                "gcsDocuments": {
                    "documents": [{"gcsUri": gcs_input_uri, "mimeType": mime_type}]
                }
            },
            "documentOutputConfig": {
                "gcsOutputConfig": {"gcsUri": self._prefix_uri(output_prefix)}
            },
        }
        response = await request_with_retry(
            "POST",
            url,
            settings=self._settings,
            headers=await auth_headers(self._tokens),
            json=body,
        )
        operation_name = str(response.json().get("name", ""))
        log.info(
            "docai.batch_started",
            operation=operation_name,
            output_prefix=output_prefix,
        )
        return BatchHandle(
            operation_name=operation_name,
            output_prefix=output_prefix,
            immediate=False,
        )

    async def operation_status(self, operation_name: str) -> OperationStatus:
        url = f"{self._settings.docai_endpoint}/v1/{operation_name}"
        response = await request_with_retry(
            "GET",
            url,
            settings=self._settings,
            headers=await auth_headers(self._tokens),
        )
        payload = response.json()
        if not payload.get("done", False):
            return OperationStatus(done=False)
        error = payload.get("error")
        if error:
            return OperationStatus(
                done=True,
                error_code=str(error.get("code", "UNKNOWN")),
                error_message=str(error.get("message", ""))[:2000],
            )
        return OperationStatus(done=True)

    async def find_operation_for_prefix(self, output_prefix: str) -> str | None:
        """Look for an operation already writing to this prefix.

        Guards the one genuinely non idempotent effect in the pipeline. A
        worker that crashed after starting a batch but before recording the
        operation name asks this before starting a second paid call.
        """
        url = (
            f"{self._settings.docai_endpoint}/v1/"
            f"projects/{self._settings.gcp_project_id}"
            f"/locations/{self._settings.gcp_location}/operations"
        )
        try:
            response = await request_with_retry(
                "GET",
                url,
                settings=self._settings,
                headers=await auth_headers(self._tokens),
            )
        except UpstreamRejectedError as exc:
            log.warning("docai.operation_lookup_failed", error=str(exc))
            return None
        for operation in response.json().get("operations", []):
            metadata = json.dumps(operation.get("metadata", {}))
            if output_prefix in metadata:
                name = str(operation.get("name", ""))
                log.warning(
                    "docai.reusing_operation",
                    operation=name,
                    output_prefix=output_prefix,
                )
                return name
        return None

    async def fetch_raw(
        self, *, output_prefix: str, content_hash: str
    ) -> dict[str, Any]:
        objects = await self._store.list_prefix(output_prefix)
        shards = [item for item in objects if item.name.endswith(".json")]
        if not shards:
            raise NotFoundError("Batch output is not present", detail=output_prefix)
        documents: list[dict[str, Any]] = []
        for shard in shards:
            raw = await self._store.get(shard.name)
            documents.append(json.loads(raw.decode("utf-8")))
        merged = merge_shards(documents)
        await self._write_cache(content_hash, merged)
        return merged

    async def _write_cache(self, content_hash: str, document: dict[str, Any]) -> None:
        """Cache before returning, so the next run costs nothing."""
        path = cache_path(self._settings, content_hash)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            scratch = path.with_suffix(".json.partial")
            scratch.write_text(
                json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
            )
            scratch.replace(path)

        await asyncio.to_thread(_write)
        log.info("docai.cached", content_hash=content_hash, path=str(path))

    def _prefix_uri(self, output_prefix: str) -> str:
        return f"gs://{self._settings.gcs_bucket}/{output_prefix}"


class FixtureDocumentAIClient:
    """Replays cached responses. Makes no network call of any kind."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def start_batch(
        self,
        *,
        gcs_input_uri: str,
        mime_type: str,
        output_prefix: str,
        content_hash: str,
    ) -> BatchHandle:
        del gcs_input_uri, mime_type
        # Fail now rather than after the job row claims to be RUNNING.
        path = cache_path(self._settings, content_hash)
        if not path.exists():
            raise FixtureMissingError(
                "No cached Document AI response for this document",
                detail=(
                    f"expected {path}. Run once against the live processor, "
                    "or add the fixture by hand."
                ),
            )
        return BatchHandle(
            operation_name=None, output_prefix=output_prefix, immediate=True
        )

    async def operation_status(self, operation_name: str) -> OperationStatus:
        del operation_name
        return OperationStatus(done=True)

    async def find_operation_for_prefix(self, output_prefix: str) -> str | None:
        del output_prefix
        return None

    async def fetch_raw(
        self, *, output_prefix: str, content_hash: str
    ) -> dict[str, Any]:
        del output_prefix
        path = cache_path(self._settings, content_hash)
        if not path.exists():
            raise FixtureMissingError(
                "No cached Document AI response for this document",
                detail=f"expected {path}",
            )
        raw = await asyncio.to_thread(path.read_text, "utf-8")
        document: dict[str, Any] = json.loads(raw)
        return document


def merge_shards(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine the shards of one batch output into a single document.

    Document AI splits output across several files for a multi page
    document. Each shard carries its own slice of pages and entities, so a
    handler that read only the first shard would silently drop line items
    from later pages.
    """
    if not documents:
        return {}
    if len(documents) == 1:
        return documents[0]

    ordered = sorted(documents, key=_first_page_number)
    merged: dict[str, Any] = {
        key: value
        for key, value in ordered[0].items()
        if key not in {"pages", "entities", "text"}
    }
    merged["text"] = "".join(str(doc.get("text", "")) for doc in ordered)
    merged["pages"] = [page for doc in ordered for page in doc.get("pages", [])]
    merged["entities"] = [
        entity for doc in ordered for entity in doc.get("entities", [])
    ]
    return merged


def _first_page_number(document: dict[str, Any]) -> int:
    pages = document.get("pages") or []
    if not pages:
        return 0
    return int(pages[0].get("pageNumber", 0) or 0)


def build_document_ai_client(
    settings: Settings | None = None,
    *,
    object_store: ObjectStore | None = None,
) -> DocumentAIClient:
    """Choose an implementation by configuration, not by call site."""
    cfg = settings or get_settings()
    if cfg.replay_fixtures:
        return FixtureDocumentAIClient(cfg)
    if object_store is None:
        raise ValueError("The live Document AI client needs an object store")
    return LiveDocumentAIClient(cfg, object_store=object_store)
