"""The matching engine's SQL, kept in src/matching/queries as .sql files.

The queries are part of the portfolio and are written to be read, so they
live as standalone files rather than as strings inside Python.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

_QUERIES = Path(__file__).resolve().parent / "queries"


@cache
def query(name: str) -> TextClause:
    """A named query, read from disk once."""
    return text((_QUERIES / f"{name}.sql").read_text("utf-8"))
