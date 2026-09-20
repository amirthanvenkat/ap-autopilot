"""Gmail access behind one protocol.

A Gmail push notification is not a message event. It carries an email
address and a history id, nothing else. One notification can cover several
messages, consecutive notifications can cover overlapping history ranges,
and Gmail may coalesce them. So message level deduplication on the Pub/Sub
message id protects against Pub/Sub redelivery only. The content hash on
documents is the layer that actually stops the same attachment being
processed twice.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from src.common import media
from src.common.config import Settings, get_settings
from src.common.errors import (
    FixtureMissingError,
    GmailHistoryExpiredError,
    UpstreamRejectedError,
)
from src.common.gcp import ApplicationDefaultTokenProvider, TokenProvider, auth_headers
from src.common.http import request_with_retry
from src.common.logging import get_logger

log = get_logger(__name__)

_GMAIL_HOST = "https://gmail.googleapis.com/gmail/v1"


@dataclass(frozen=True)
class Attachment:
    """One qualifying attachment on a message."""

    message_id: str
    attachment_id: str
    filename: str
    media_type: str
    size: int
    received_at: datetime


@dataclass(frozen=True)
class HistoryPage:
    """Messages added since a history id."""

    message_ids: list[str]
    new_history_id: str | None


class GmailClient(Protocol):
    """Reads the monitored mailbox."""

    async def list_added_messages(
        self, *, start_history_id: str, label_id: str | None
    ) -> HistoryPage: ...

    async def list_attachments(self, message_id: str) -> list[Attachment]: ...

    async def get_attachment(self, *, message_id: str, attachment_id: str) -> bytes: ...

    async def renew_watch(
        self, *, topic: str, label_ids: list[str]
    ) -> dict[str, Any]: ...

    async def resync(self, *, label_id: str | None) -> HistoryPage: ...


def _decode_b64url(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _headers_map(part: dict[str, Any]) -> dict[str, str]:
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in part.get("headers") or []
    }


def _is_inline(part: dict[str, Any]) -> bool:
    """Is this part embedded in the message body rather than attached?

    Signature logos and embedded images arrive as attachments in the API
    shape. Without this check every one of them becomes a document row and
    a paid Document AI call.
    """
    headers = _headers_map(part)
    if "content-id" in headers:
        return True
    disposition = headers.get("content-disposition", "")
    return disposition.strip().lower().startswith("inline")


def iter_attachment_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk a message payload into its attachment parts."""
    found: list[dict[str, Any]] = []

    def walk(part: dict[str, Any]) -> None:
        body = part.get("body") or {}
        if body.get("attachmentId") and part.get("filename"):
            found.append(part)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return found


def qualifying_attachments(
    message: dict[str, Any], *, min_bytes: int
) -> list[Attachment]:
    """Filter a message's parts down to plausible invoices."""
    message_id = str(message.get("id", ""))
    internal_ms = message.get("internalDate")
    received_at = (
        datetime.fromtimestamp(int(internal_ms) / 1000, tz=UTC)
        if internal_ms
        else datetime.now(tz=UTC)
    )
    payload = message.get("payload") or {}

    attachments: list[Attachment] = []
    for part in iter_attachment_parts(payload):
        filename = str(part.get("filename", ""))
        declared = media.normalise_media_type(str(part.get("mimeType", "")))
        size = int((part.get("body") or {}).get("size", 0) or 0)

        if _is_inline(part):
            log.info("gmail.skipped_inline", message_id=message_id, filename=filename)
            continue
        if not media.is_accepted(declared):
            log.info(
                "gmail.skipped_media_type",
                message_id=message_id,
                filename=filename,
                media_type=declared,
            )
            continue
        if size < min_bytes:
            log.info(
                "gmail.skipped_small",
                message_id=message_id,
                filename=filename,
                size=size,
            )
            continue

        attachments.append(
            Attachment(
                message_id=message_id,
                attachment_id=str((part.get("body") or {}).get("attachmentId")),
                filename=filename,
                media_type=declared or media.PDF,
                size=size,
                received_at=received_at,
            )
        )
    return attachments


class LiveGmailClient:
    """Gmail over its REST API."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._tokens = token_provider or ApplicationDefaultTokenProvider()
        self._user = self._settings.gmail_user_id

    async def list_added_messages(
        self, *, start_history_id: str, label_id: str | None
    ) -> HistoryPage:
        message_ids: list[str] = []
        page_token: str | None = None
        new_history_id: str | None = None
        headers = await auth_headers(self._tokens)

        while True:
            url = (
                f"{_GMAIL_HOST}/users/{self._user}/history"
                f"?startHistoryId={quote(start_history_id)}"
                "&historyTypes=messageAdded"
            )
            if label_id:
                url = f"{url}&labelId={quote(label_id)}"
            if page_token:
                url = f"{url}&pageToken={quote(page_token)}"

            try:
                response = await request_with_retry(
                    "GET", url, settings=self._settings, headers=headers
                )
            except UpstreamRejectedError as exc:
                if exc.status_code == 404:
                    # The stored cursor has aged out. Retrying this range
                    # will never work; the caller must resync instead.
                    raise GmailHistoryExpiredError(
                        "Stored Gmail history id is too old",
                        detail=start_history_id,
                    ) from exc
                raise

            payload = response.json()
            new_history_id = str(payload.get("historyId") or new_history_id or "")
            for record in payload.get("history") or []:
                for added in record.get("messagesAdded") or []:
                    identifier = str((added.get("message") or {}).get("id", ""))
                    if identifier and identifier not in message_ids:
                        message_ids.append(identifier)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return HistoryPage(
            message_ids=message_ids, new_history_id=new_history_id or None
        )

    async def list_attachments(self, message_id: str) -> list[Attachment]:
        url = (
            f"{_GMAIL_HOST}/users/{self._user}/messages/{quote(message_id)}?format=full"
        )
        response = await request_with_retry(
            "GET",
            url,
            settings=self._settings,
            headers=await auth_headers(self._tokens),
        )
        return qualifying_attachments(
            response.json(), min_bytes=self._settings.gmail_min_attachment_bytes
        )

    async def get_attachment(self, *, message_id: str, attachment_id: str) -> bytes:
        url = (
            f"{_GMAIL_HOST}/users/{self._user}/messages/{quote(message_id)}"
            f"/attachments/{quote(attachment_id)}"
        )
        response = await request_with_retry(
            "GET",
            url,
            settings=self._settings,
            headers=await auth_headers(self._tokens),
        )
        return _decode_b64url(str(response.json().get("data", "")))

    async def resync(self, *, label_id: str | None) -> HistoryPage:
        """List the label from scratch when the history cursor has expired.

        A history diff is impossible once the stored id ages out, so the
        only way back is to enumerate the label and take a fresh cursor.
        Content hash deduplication makes re-reading old messages cheap: they
        resolve to documents that already exist.
        """
        headers = await auth_headers(self._tokens)
        message_ids: list[str] = []
        page_token: str | None = None
        while True:
            url = f"{_GMAIL_HOST}/users/{self._user}/messages?maxResults=100"
            if label_id:
                url = f"{url}&labelIds={quote(label_id)}"
            if page_token:
                url = f"{url}&pageToken={quote(page_token)}"
            response = await request_with_retry(
                "GET", url, settings=self._settings, headers=headers
            )
            payload = response.json()
            for item in payload.get("messages") or []:
                identifier = str(item.get("id", ""))
                if identifier and identifier not in message_ids:
                    message_ids.append(identifier)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        profile = await request_with_retry(
            "GET",
            f"{_GMAIL_HOST}/users/{self._user}/profile",
            settings=self._settings,
            headers=headers,
        )
        history_id = str(profile.json().get("historyId") or "")
        log.warning("gmail.resynced", messages=len(message_ids), history_id=history_id)
        return HistoryPage(message_ids=message_ids, new_history_id=history_id or None)

    async def renew_watch(self, *, topic: str, label_ids: list[str]) -> dict[str, Any]:
        """Re-arm users.watch.

        The subscription lapses after seven days. Nothing reports the lapse,
        so without a scheduled call here ingestion stops silently about a
        week after setup.
        """
        url = f"{_GMAIL_HOST}/users/{self._user}/watch"
        response = await request_with_retry(
            "POST",
            url,
            settings=self._settings,
            headers=await auth_headers(self._tokens),
            json={"topicName": topic, "labelIds": label_ids},
        )
        payload: dict[str, Any] = response.json()
        log.info(
            "gmail.watch_renewed",
            expiration=payload.get("expiration"),
            history_id=payload.get("historyId"),
        )
        return payload


class FixtureGmailClient:
    """Replays a committed mailbox. Makes no network call.

    Reads fixtures/gmail/messages/{message_id}.json and
    fixtures/gmail/history/{start_history_id}.json, with attachment bytes in
    fixtures/gmail/attachments/{attachment_id}.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._root = self._settings.fixtures_dir / "gmail"

    async def list_added_messages(
        self, *, start_history_id: str, label_id: str | None
    ) -> HistoryPage:
        del label_id
        path = self._root / "history" / f"{start_history_id}.json"
        if not path.exists():
            raise FixtureMissingError("No Gmail history fixture", detail=str(path))
        payload = json.loads(await asyncio.to_thread(path.read_text, "utf-8"))
        return HistoryPage(
            message_ids=[str(item) for item in payload.get("message_ids", [])],
            new_history_id=str(payload.get("new_history_id") or "") or None,
        )

    async def list_attachments(self, message_id: str) -> list[Attachment]:
        path = self._root / "messages" / f"{message_id}.json"
        if not path.exists():
            raise FixtureMissingError("No Gmail message fixture", detail=str(path))
        message = json.loads(await asyncio.to_thread(path.read_text, "utf-8"))
        return qualifying_attachments(
            message, min_bytes=self._settings.gmail_min_attachment_bytes
        )

    async def get_attachment(self, *, message_id: str, attachment_id: str) -> bytes:
        del message_id
        path = self._attachment_path(attachment_id)
        if path is None:
            raise FixtureMissingError(
                "No Gmail attachment fixture", detail=attachment_id
            )
        return await asyncio.to_thread(path.read_bytes)

    def _attachment_path(self, attachment_id: str) -> Path | None:
        base = self._root / "attachments"
        if not base.exists():
            return None
        for candidate in sorted(base.iterdir()):
            if candidate.stem == attachment_id or candidate.name == attachment_id:
                return candidate
        return None

    async def resync(self, *, label_id: str | None) -> HistoryPage:
        del label_id
        path = self._root / "history" / "resync.json"
        if not path.exists():
            raise FixtureMissingError("No Gmail resync fixture", detail=str(path))
        payload = json.loads(await asyncio.to_thread(path.read_text, "utf-8"))
        return HistoryPage(
            message_ids=[str(item) for item in payload.get("message_ids", [])],
            new_history_id=str(payload.get("new_history_id") or "") or None,
        )

    async def renew_watch(self, *, topic: str, label_ids: list[str]) -> dict[str, Any]:
        del topic, label_ids
        return {"expiration": "0", "historyId": "1"}


def build_gmail_client(settings: Settings | None = None) -> GmailClient:
    """Choose an implementation by configuration, not by call site."""
    cfg = settings or get_settings()
    if cfg.replay_fixtures:
        return FixtureGmailClient(cfg)
    return LiveGmailClient(cfg)
