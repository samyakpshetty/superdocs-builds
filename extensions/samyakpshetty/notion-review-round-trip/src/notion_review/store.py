"""Durable persistence for review rounds.

A review round outlives the process: it is exported to Word, waits days for a reviewer, then
resumes. So it lives in a store, not memory. The :class:`Store` protocol has two real backends —
SQLite (zero-infra, the keyless test suite and a single-tenant deployment) and Postgres (the
multi-tenant/traffic substrate) — chosen by ``DATABASE_URL``. Swapping is configuration, not a
rewrite. Each round is its own row, so concurrent rounds are isolated by construction; writes are
transactional and last-writer-wins per round (the write-back worker adds a claim in F5).
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Protocol, runtime_checkable

from notion_review.domain import ReviewRound

_DDL = """
CREATE TABLE IF NOT EXISTS review_rounds (
    id             TEXT PRIMARY KEY,
    notion_page_id TEXT NOT NULL,
    status         TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    data           {json_type} NOT NULL
)
"""


@runtime_checkable
class Store(Protocol):
    def save(self, round_: ReviewRound) -> None:
        """Insert or update a round (upsert), keyed by id."""
        ...

    def get(self, round_id: str) -> ReviewRound | None: ...

    def list_ids(self) -> list[str]: ...

    def delete(self, round_id: str) -> None: ...

    def close(self) -> None: ...


class SQLiteStore:
    """Thread-safe SQLite backend (WAL for file paths). Default for keyless tests and demo."""

    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_DDL.format(json_type="TEXT"))
        self._conn.commit()

    def save(self, round_: ReviewRound) -> None:
        round_.touch()
        with self._lock:
            self._conn.execute(
                "INSERT INTO review_rounds (id, notion_page_id, status, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, updated_at=excluded.updated_at, data=excluded.data",
                (
                    round_.id,
                    round_.notion_page_id,
                    round_.status.value,
                    round_.updated_at.isoformat(),
                    round_.model_dump_json(),
                ),
            )
            self._conn.commit()

    def get(self, round_id: str) -> ReviewRound | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM review_rounds WHERE id = ?", (round_id,)
            ).fetchone()
        return ReviewRound.model_validate_json(row["data"]) if row else None

    def list_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM review_rounds ORDER BY updated_at").fetchall()
        return [r["id"] for r in rows]

    def delete(self, round_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM review_rounds WHERE id = ?", (round_id,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PostgresStore:
    """Postgres backend — the multi-tenant/traffic substrate, pooled for concurrent workers."""

    def __init__(self, dsn: str) -> None:
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(dsn, min_size=1, max_size=8, open=True)
        with self._pool.connection() as conn:
            conn.execute(_DDL.format(json_type="JSONB"))

    def save(self, round_: ReviewRound) -> None:
        round_.touch()
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO review_rounds (id, notion_page_id, status, updated_at, data) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "status = EXCLUDED.status, updated_at = EXCLUDED.updated_at, data = EXCLUDED.data",
                (
                    round_.id,
                    round_.notion_page_id,
                    round_.status.value,
                    round_.updated_at.isoformat(),
                    round_.model_dump_json(),
                ),
            )

    def get(self, round_id: str) -> ReviewRound | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT data FROM review_rounds WHERE id = %s", (round_id,)
            ).fetchone()
        if row is None:
            return None
        return ReviewRound.model_validate(row[0])

    def list_ids(self) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT id FROM review_rounds ORDER BY updated_at").fetchall()
        return [r[0] for r in rows]

    def delete(self, round_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM review_rounds WHERE id = %s", (round_id,))

    def close(self) -> None:
        self._pool.close()


def open_store(database_url: str | None) -> Store:
    """Pick a backend from config: Postgres when a DSN is given, else in-memory SQLite."""
    if database_url and database_url.startswith(("postgres://", "postgresql://")):
        return PostgresStore(database_url)
    if database_url:  # a sqlite file path
        return SQLiteStore(database_url)
    return SQLiteStore(":memory:")
