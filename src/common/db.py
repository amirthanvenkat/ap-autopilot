"""Database engine and transaction helpers.

The pool is deliberately small. Cloud Run scales out horizontally and the
Neon free tier caps connections, so a generous per instance pool turns load
into random connection errors. Point DATABASE_URL at Neon's pooled endpoint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from src.common.config import Settings, get_settings

_engine: AsyncEngine | None = None


def _normalise_url(url: str) -> str:
    """Accept a plain Postgres URL and drive it with psycopg async."""
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build a new engine. Callers usually want get_engine instead."""
    cfg = settings or get_settings()
    return create_async_engine(
        _normalise_url(cfg.database_url),
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_pre_ping=True,
        # Neon suspends idle compute, so a long lived pooled connection can
        # be dead on arrival. Recycle well inside that window.
        pool_recycle=240,
        connect_args={"options": f"-c statement_timeout={cfg.db_statement_timeout_ms}"},
    )


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Process wide engine singleton."""
    global _engine
    if _engine is None:
        _engine = create_engine(settings)
    return _engine


def set_engine(engine: AsyncEngine | None) -> None:
    """Replace the singleton. Tests use this to point at a scratch database."""
    global _engine
    _engine = engine


async def dispose_engine() -> None:
    """Close pooled connections on shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def transaction(
    engine: AsyncEngine | None = None,
) -> AsyncIterator[AsyncConnection]:
    """Run a block in one transaction, committing on clean exit.

    The acceptance path depends on this being a single round trip, so keep
    the body of an acceptance transaction to one statement.
    """
    target = engine or get_engine()
    async with target.begin() as connection:
        yield connection


@asynccontextmanager
async def connection(
    engine: AsyncEngine | None = None,
) -> AsyncIterator[AsyncConnection]:
    """Run a block on a connection without an implicit transaction."""
    target = engine or get_engine()
    async with target.connect() as conn:
        yield conn
