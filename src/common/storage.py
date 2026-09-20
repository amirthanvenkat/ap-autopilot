"""Object storage behind one protocol.

Two implementations, selected by REPLAY_FIXTURES and never by a branch at
the call site. The local one writes under fixtures/gcs/ so the demo runs
with no network at all.

Writes are create-if-absent. Accepted change 5 keys the inbox path on the
content hash, so a redelivery writes identical bytes to an identical path
and the precondition turns the second write into a no-op instead of an
orphaned object.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from src.common.config import Settings, get_settings
from src.common.errors import NotFoundError, UpstreamRejectedError
from src.common.gcp import ApplicationDefaultTokenProvider, TokenProvider, auth_headers
from src.common.http import request_with_retry
from src.common.logging import get_logger

log = get_logger(__name__)

_UPLOAD_HOST = "https://storage.googleapis.com/upload/storage/v1"
_API_HOST = "https://storage.googleapis.com/storage/v1"


@dataclass(frozen=True)
class StoredObject:
    """One object in the bucket."""

    name: str
    size: int
    uri: str


class ObjectStore(Protocol):
    """Reads and writes document bytes."""

    async def put_if_absent(
        self, name: str, data: bytes, *, content_type: str
    ) -> StoredObject: ...

    async def get(self, name: str) -> bytes: ...

    async def list_prefix(self, prefix: str) -> list[StoredObject]: ...

    def uri(self, name: str) -> str: ...


class GcsObjectStore:
    """Cloud Storage over its JSON REST API."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._tokens = token_provider or ApplicationDefaultTokenProvider()
        self._bucket = self._settings.gcs_bucket

    def uri(self, name: str) -> str:
        return f"gs://{self._bucket}/{name}"

    async def put_if_absent(
        self, name: str, data: bytes, *, content_type: str
    ) -> StoredObject:
        url = (
            f"{_UPLOAD_HOST}/b/{self._bucket}/o"
            f"?uploadType=media&name={quote(name, safe='')}&ifGenerationMatch=0"
        )
        headers = await auth_headers(self._tokens)
        headers["Content-Type"] = content_type
        try:
            response = await request_with_retry(
                "POST", url, settings=self._settings, headers=headers, content=data
            )
        except UpstreamRejectedError as exc:
            # 412 means the object is already there with these exact bytes,
            # which is the whole point of the precondition.
            if exc.status_code == 412:
                log.info("storage.already_present", object=name)
                return StoredObject(name=name, size=len(data), uri=self.uri(name))
            raise
        payload = response.json()
        return StoredObject(
            name=name,
            size=int(payload.get("size", len(data))),
            uri=self.uri(name),
        )

    async def get(self, name: str) -> bytes:
        url = f"{_API_HOST}/b/{self._bucket}/o/{quote(name, safe='')}?alt=media"
        headers = await auth_headers(self._tokens)
        try:
            response = await request_with_retry(
                "GET", url, settings=self._settings, headers=headers
            )
        except UpstreamRejectedError as exc:
            if exc.status_code == 404:
                raise NotFoundError("Object not found", detail=self.uri(name)) from exc
            raise
        return response.content

    async def list_prefix(self, prefix: str) -> list[StoredObject]:
        objects: list[StoredObject] = []
        page_token: str | None = None
        headers = await auth_headers(self._tokens)
        while True:
            url = (
                f"{_API_HOST}/b/{self._bucket}/o"
                f"?prefix={quote(prefix, safe='')}&maxResults=1000"
            )
            if page_token:
                url = f"{url}&pageToken={quote(page_token, safe='')}"
            response = await request_with_retry(
                "GET", url, settings=self._settings, headers=headers
            )
            payload = response.json()
            for item in payload.get("items", []):
                objects.append(
                    StoredObject(
                        name=item["name"],
                        size=int(item.get("size", 0)),
                        uri=self.uri(item["name"]),
                    )
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return sorted(objects, key=lambda item: item.name)


class LocalObjectStore:
    """Filesystem stand-in used by fixtures mode and tests."""

    def __init__(self, root: Path, bucket: str = "local") -> None:
        self._root = root
        self._bucket = bucket
        self._root.mkdir(parents=True, exist_ok=True)

    def uri(self, name: str) -> str:
        return f"gs://{self._bucket}/{name}"

    def _path(self, name: str) -> Path:
        target = (self._root / name).resolve()
        root = self._root.resolve()
        if not target.is_relative_to(root):
            raise NotFoundError("Object name escapes the store root", detail=name)
        return target

    async def put_if_absent(
        self, name: str, data: bytes, *, content_type: str
    ) -> StoredObject:
        del content_type
        path = self._path(name)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                return
            # Write then move, so a crash cannot leave a half written object.
            scratch = path.with_suffix(path.suffix + ".partial")
            scratch.write_bytes(data)
            scratch.replace(path)

        await asyncio.to_thread(_write)
        return StoredObject(name=name, size=len(data), uri=self.uri(name))

    async def get(self, name: str) -> bytes:
        path = self._path(name)
        if not path.exists():
            raise NotFoundError("Object not found", detail=self.uri(name))
        return await asyncio.to_thread(path.read_bytes)

    async def list_prefix(self, prefix: str) -> list[StoredObject]:
        def _list() -> list[StoredObject]:
            base = self._root
            found: list[StoredObject] = []
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.suffix == ".partial":
                    continue
                name = path.relative_to(base).as_posix()
                if name.startswith(prefix):
                    found.append(
                        StoredObject(
                            name=name,
                            size=path.stat().st_size,
                            uri=self.uri(name),
                        )
                    )
            return found

        return await asyncio.to_thread(_list)


def build_object_store(settings: Settings | None = None) -> ObjectStore:
    """Choose an implementation by configuration, not by call site."""
    cfg = settings or get_settings()
    if cfg.replay_fixtures:
        return LocalObjectStore(
            cfg.fixtures_dir / "gcs", bucket=cfg.gcs_bucket or "local"
        )
    return GcsObjectStore(cfg)
