"""Event loop selection.

psycopg's async driver refuses to run on the proactor event loop, which is
the default on Windows. Production is Cloud Run and so Linux, where this is
a no-op, but local development and the integration tests both need the
selector loop or every database call fails at connect time.
"""

from __future__ import annotations

import asyncio
import sys

from src.common.logging import get_logger

log = get_logger(__name__)


def _is_windows() -> bool:
    # mypy narrows direct sys.platform checks to the platform it runs on,
    # which would mark the Windows branch unreachable, and so unchecked, on
    # Linux. A function call is not narrowed, so both branches are checked.
    return sys.platform == "win32"


def configure_event_loop() -> None:
    """Select an event loop the database driver can use.

    Called before the server creates its loop. On any platform other than
    Windows this does nothing at all.
    """
    if not _is_windows():
        return
    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is None:
        return
    if isinstance(asyncio.get_event_loop_policy(), policy):
        return
    asyncio.set_event_loop_policy(policy())
    log.info("runtime.selector_event_loop_selected", platform=sys.platform)
