"""Gmail watch cursor.

history.list needs a stored start id, and users.watch lapses after seven
days without announcing it. Both facts live in one row per mailbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class WatchState:
    """Where the last history read finished, and when the watch expires."""

    email_address: str
    history_id: str
    watch_expires_at: datetime | None


_SELECT_SQL = text(
    """
    select email_address, history_id, watch_expires_at
      from gmail_watch_state
     where email_address = :email_address
    """
)

_UPSERT_SQL = text(
    """
    insert into gmail_watch_state (email_address, history_id, watch_expires_at)
    values (:email_address, :history_id, :watch_expires_at)
    on conflict (email_address) do update
       set history_id = excluded.history_id,
           watch_expires_at = coalesce(
               excluded.watch_expires_at, gmail_watch_state.watch_expires_at
           ),
           updated_at = now()
     where excluded.history_id >= gmail_watch_state.history_id
    """
)


async def get_state(conn: AsyncConnection, email_address: str) -> WatchState | None:
    row = (await conn.execute(_SELECT_SQL, {"email_address": email_address})).first()
    if row is None:
        return None
    return WatchState(
        email_address=row.email_address,
        history_id=str(row.history_id),
        watch_expires_at=row.watch_expires_at,
    )


async def save_state(
    conn: AsyncConnection,
    *,
    email_address: str,
    history_id: str,
    watch_expires_at: datetime | None = None,
) -> None:
    """Advance the cursor.

    The guard on the update refuses to move the cursor backwards. Gmail
    notifications arrive out of order often enough that a late one carrying
    an older history id would otherwise rewind the cursor and cause the same
    range to be read repeatedly.
    """
    await conn.execute(
        _UPSERT_SQL,
        {
            "email_address": email_address,
            "history_id": int(history_id),
            "watch_expires_at": watch_expires_at,
        },
    )
