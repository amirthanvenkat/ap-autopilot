"""The push handler critical path.

Everything a push endpoint does before returning 200 lives here: verify the
token, read the body, commit the marker and the work in one statement, and
try to publish. Nothing else. No Gmail call, no Cloud Storage write, no
Document AI call, and above all no work scheduled to run after the response.

Cloud Run throttles CPU once a response is returned, so work started in a
handler and left to finish afterwards may be frozen or lost with the
instance. That is why the unit of work is committed to Postgres instead.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.common.db import transaction
from src.common.deps import Dependencies
from src.common.errors import PermanentError
from src.common.logging import get_logger
from src.common.outbox import Acceptance, OutboxTask, accept_message

log = get_logger(__name__)

# Cap what is stored from an unparseable body. Enough to diagnose it,
# bounded so a hostile payload cannot bloat the table.
_RAW_LIMIT = 8192


@dataclass(frozen=True)
class AcceptedMessage:
    """Result of accepting one push request."""

    message_id: str
    task_id: str
    duplicate: bool


def parse_envelope(body: bytes) -> tuple[str, dict[str, Any], dict[str, str], str]:
    """Pull the message id, decoded data and attributes out of a push body.

    A body that cannot be parsed still yields a stable message id, derived
    from the bytes, so redelivery of the same broken message deduplicates
    rather than piling up rows.
    """
    raw_text = body.decode("utf-8", errors="replace")[:_RAW_LIMIT]
    try:
        envelope = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _fallback_id(body), {}, {}, raw_text

    if not isinstance(envelope, dict):
        return _fallback_id(body), {}, {}, raw_text

    message = envelope.get("message")
    if not isinstance(message, dict):
        return _fallback_id(body), {}, {}, raw_text

    message_id = str(message.get("messageId") or message.get("message_id") or "")
    if not message_id:
        message_id = _fallback_id(body)

    attributes = {
        str(key): str(value) for key, value in (message.get("attributes") or {}).items()
    }

    data: dict[str, Any] = {}
    encoded = message.get("data")
    if isinstance(encoded, str) and encoded:
        try:
            decoded = base64.b64decode(encoded)
            parsed = json.loads(decoded.decode("utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (ValueError, UnicodeDecodeError):
            data = {}

    # Eventarc carries the object name in attributes when the body is a
    # Cloud Storage notification rather than a JSON payload.
    if not data and attributes.get("objectId"):
        data = {
            "name": attributes["objectId"],
            "bucket": attributes.get("bucketId", ""),
        }

    return message_id, data, attributes, raw_text


def _fallback_id(body: bytes) -> str:
    """A deterministic id for a body Pub/Sub did not label."""
    return "sha256:" + hashlib.sha256(body).hexdigest()


async def accept_push(
    deps: Dependencies,
    *,
    handler: str,
    authorization: str | None,
    body: bytes,
) -> AcceptedMessage:
    """Verify, record and queue. The whole of the one second path."""
    await deps.verifier.verify(authorization)

    message_id, data, attributes, raw_text = parse_envelope(body)
    payload: dict[str, Any] = {
        "data": data,
        "attributes": attributes,
        "raw": raw_text if not data else None,
    }

    async with transaction(deps.engine) as conn:
        acceptance: Acceptance = await accept_message(
            conn,
            handler=handler,
            message_id=message_id,
            payload=payload,
        )

    if acceptance.duplicate:
        log.info("accept.duplicate", handler=handler, message_id=message_id)
    else:
        log.info("accept.queued", handler=handler, message_id=message_id)
        await publish_best_effort(deps, task_id=acceptance.task_id)

    return AcceptedMessage(
        message_id=message_id,
        task_id=acceptance.task_id,
        duplicate=acceptance.duplicate,
    )


async def publish_best_effort(deps: Dependencies, *, task_id: str) -> bool:
    """Nudge the worker, but never let the nudge delay the response.

    The outbox row is already committed, so a failed publish costs the
    sweep interval and nothing else. Bounding it here is what keeps a slow
    or unreachable Pub/Sub from pushing the handler past its budget.
    """
    try:
        await asyncio.wait_for(
            deps.publisher.publish(
                deps.settings.pubsub_work_topic, {"task_id": task_id}
            ),
            timeout=deps.settings.publish_timeout_seconds,
        )
    except TimeoutError:
        log.warning("accept.publish_timeout", task_id=task_id)
        return False
    except Exception as exc:
        log.warning("accept.publish_failed", task_id=task_id, error=str(exc))
        return False
    return True


def task_data(task: OutboxTask) -> dict[str, Any]:
    """The decoded message body a worker should act on.

    Raises when the body carried nothing readable, so a poison message is
    recorded as a failure with its raw bytes rather than quietly succeeding
    with no work done.
    """
    data = task.payload.get("data")
    if not isinstance(data, dict) or not data:
        raw = task.payload.get("raw")
        raise PermanentError(
            "Message carried no readable JSON payload",
            detail=str(raw)[:500] if raw else "empty data field",
        )
    return data
