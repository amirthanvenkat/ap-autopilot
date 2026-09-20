"""Identifier generation.

Two kinds of identifier appear in this codebase and the difference matters
for idempotency.

Random identifiers are for rows created once, where a replay must not create
a second row for some other reason. Derived identifiers are for rows a worker
may attempt to create repeatedly after a crash: deriving the id from stable
inputs turns a replay into a primary key conflict instead of a duplicate.
"""

from __future__ import annotations

import hashlib
import uuid

from ulid import ULID

# Stable namespace for derived identifiers. Changing this value orphans every
# derived id already in the database, so it is a constant, not a setting.
_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def new_ulid() -> str:
    """A fresh, time ordered identifier."""
    return str(ULID())


def derived_id(*parts: str) -> str:
    """A deterministic identifier from stable inputs.

    The same inputs always give the same id, so a worker retry collides on the
    primary key rather than inserting a second row.
    """
    if not parts:
        raise ValueError("derived_id requires at least one part")
    return str(uuid.uuid5(_NAMESPACE, "\x1f".join(parts)))


def content_hash(data: bytes) -> str:
    """Content address for a document's bytes.

    This is the deduplication key for documents and the fixture lookup key
    for cached Document AI responses.
    """
    return hashlib.sha256(data).hexdigest()
