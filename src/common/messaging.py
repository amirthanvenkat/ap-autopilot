"""Pub/Sub publishing and dead letter inspection.

Publishing sits outside the acceptance transaction and is best effort by
design. If it fails, the outbox row is already committed and the sweeper
picks it up, so a failed publish costs latency and nothing else. That is why
the publish timeout is a quarter of a second rather than the usual ten.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.common.config import Settings, get_settings
from src.common.gcp import ApplicationDefaultTokenProvider, TokenProvider, auth_headers
from src.common.http import request_with_retry
from src.common.logging import get_logger

log = get_logger(__name__)

_PUBSUB_HOST = "https://pubsub.googleapis.com/v1"


@dataclass(frozen=True)
class DeadLetterMessage:
    """One message sitting on the dead letter subscription."""

    message_id: str
    publish_time: str
    delivery_attempt: int
    attributes: dict[str, str]
    data: str


class Publisher(Protocol):
    """Publishes a work item."""

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        attributes: dict[str, str] | None = None,
    ) -> str | None: ...


class PubSubPublisher:
    """Publishes over the Pub/Sub REST API."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._tokens = token_provider or ApplicationDefaultTokenProvider()

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        attributes: dict[str, str] | None = None,
    ) -> str | None:
        url = (
            f"{_PUBSUB_HOST}/projects/{self._settings.gcp_project_id}"
            f"/topics/{topic}:publish"
        )
        body = {
            "messages": [
                {
                    "data": base64.b64encode(
                        json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    ).decode("ascii"),
                    "attributes": attributes or {},
                }
            ]
        }
        response = await request_with_retry(
            "POST",
            url,
            settings=self._settings,
            headers=await auth_headers(self._tokens),
            json=body,
            # One attempt. The sweeper is the retry, not this call.
            max_attempts=1,
            timeout=self._settings.publish_timeout_seconds,
        )
        ids = response.json().get("messageIds") or []
        return str(ids[0]) if ids else None

    async def pull_dead_letters(
        self, max_messages: int = 50
    ) -> list[DeadLetterMessage]:
        """Read without acknowledging, so inspection does not consume."""
        url = (
            f"{_PUBSUB_HOST}/projects/{self._settings.gcp_project_id}"
            f"/subscriptions/{self._settings.pubsub_deadletter_subscription}:pull"
        )
        response = await request_with_retry(
            "POST",
            url,
            settings=self._settings,
            headers=await auth_headers(self._tokens),
            json={"maxMessages": max_messages, "returnImmediately": True},
        )
        received = response.json().get("receivedMessages") or []
        messages: list[DeadLetterMessage] = []
        for item in received:
            message = item.get("message", {})
            messages.append(
                DeadLetterMessage(
                    message_id=str(message.get("messageId", "")),
                    publish_time=str(message.get("publishTime", "")),
                    delivery_attempt=int(item.get("deliveryAttempt", 0)),
                    attributes=dict(message.get("attributes") or {}),
                    data=str(message.get("data", "")),
                )
            )
        return messages


@dataclass
class NullPublisher:
    """Fixtures mode publisher. Records calls and sends nothing.

    Work still reaches the worker, because the outbox sweeper claims PENDING
    rows regardless of whether a publish ever happened. Fixtures mode
    therefore exercises the same recovery path that a production publish
    failure would take.
    """

    published: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        attributes: dict[str, str] | None = None,
    ) -> str | None:
        del attributes
        self.published.append((topic, payload))
        log.info("pubsub.suppressed", topic=topic, reason="fixtures_mode")
        return None

    async def pull_dead_letters(
        self, max_messages: int = 50
    ) -> list[DeadLetterMessage]:
        del max_messages
        return []


def build_publisher(settings: Settings | None = None) -> Publisher:
    """Choose an implementation by configuration, not by call site."""
    cfg = settings or get_settings()
    if cfg.replay_fixtures:
        return NullPublisher()
    return PubSubPublisher(cfg)
