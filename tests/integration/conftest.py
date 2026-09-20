"""Integration test helpers.

Every test here needs a real Postgres. The design leans on data modifying
CTEs, FOR UPDATE SKIP LOCKED, advisory locks and jsonb, so there is no
useful substitute.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.app import create_app
from src.common.config import Settings
from src.common.deps import Dependencies


@pytest_asyncio.fixture
async def client(
    fixtures_settings: Settings, deps: Dependencies
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client bound to the app, with test dependencies injected.

    The lifespan is not run: it would build its own clients and its own
    engine, and these tests need the same engine they truncate between
    cases.
    """
    app = create_app(fixtures_settings)
    app.state.deps = deps
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        yield http


async def count(engine: AsyncEngine, table: str, where: str = "") -> int:
    """Row count for a table, optionally filtered."""
    clause = f" where {where}" if where else ""
    async with engine.begin() as conn:
        result = await conn.execute(text(f"select count(*) from {table}{clause}"))
        return int(result.scalar_one())


async def fetch_all(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    """Run a query and return every row."""
    async with engine.begin() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def fetch_one(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    """Run a query and return the first row, or None."""
    async with engine.begin() as conn:
        return (await conn.execute(text(sql), params)).first()


async def seed_watch_cursor(engine: AsyncEngine, email: str, history_id: str) -> None:
    """Point the Gmail cursor at a known history range."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "insert into gmail_watch_state (email_address, history_id) "
                "values (:email, :history_id) "
                "on conflict (email_address) do update "
                "set history_id = excluded.history_id"
            ),
            {"email": email, "history_id": int(history_id)},
        )
