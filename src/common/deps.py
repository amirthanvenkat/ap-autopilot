"""Dependency container.

Built once at application startup. Every external client is chosen here by
configuration, so no call site ever branches on REPLAY_FIXTURES.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from src.common.config import Settings, get_settings
from src.common.db import get_engine
from src.common.messaging import Publisher, build_publisher
from src.common.oidc import TokenVerifier, build_verifier
from src.common.storage import ObjectStore, build_object_store
from src.extraction.docai import DocumentAIClient, build_document_ai_client
from src.ingestion.gmail import GmailClient, build_gmail_client


@dataclass(frozen=True)
class Dependencies:
    """Everything a handler or worker needs, resolved once."""

    settings: Settings
    engine: AsyncEngine
    object_store: ObjectStore
    documentai: DocumentAIClient
    gmail: GmailClient
    publisher: Publisher
    verifier: TokenVerifier


def build_dependencies(settings: Settings | None = None) -> Dependencies:
    """Resolve every client for the configured mode."""
    cfg = settings or get_settings()
    cfg.require_live()
    object_store = build_object_store(cfg)
    return Dependencies(
        settings=cfg,
        engine=get_engine(cfg),
        object_store=object_store,
        documentai=build_document_ai_client(cfg, object_store=object_store),
        gmail=build_gmail_client(cfg),
        publisher=build_publisher(cfg),
        verifier=build_verifier(cfg),
    )
