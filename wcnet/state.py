"""LIFECYCLE STAGE 1 (b) — Thread-safe, persistent duplication guard.

A SQLite-backed state layer (WAL mode + a process-wide lock) that survives
restarts and guarantees the network never processes or publishes the same
fixture / event / content hash twice. SQLite gives us durability for free;
the lock serializes writers across the discovery, monitor and publish threads.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger("wcnet.state")


class StateStore:
    """Persistence-ready, thread-safe tracking of everything processed."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS processed_fixtures (
        fixture_id   INTEGER PRIMARY KEY,
        title        TEXT,
        first_seen   TEXT NOT NULL,
        stream_video_id TEXT
    );
    CREATE TABLE IF NOT EXISTS processed_events (
        event_id     TEXT PRIMARY KEY,
        fixture_id   INTEGER NOT NULL,
        event_type   TEXT,
        created_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS event_hashes (
        event_hash   TEXT PRIMARY KEY,
        fixture_id   INTEGER NOT NULL,
        created_at   TEXT NOT NULL
    );
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + our own RLock = safe shared connection.
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        with self._lock:
            self._conn.executescript(self._SCHEMA)
            self._conn.commit()
        log.info("State store ready at %s", db_path)

    # ── low-level ────────────────────────────────────────────────────────
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── fixtures ──────────────────────────────────────────────────────────
    def is_fixture_processed(self, fixture_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed_fixtures WHERE fixture_id = ?",
                (fixture_id,),
            ).fetchone()
        return row is not None

    def mark_fixture(
        self, fixture_id: int, title: str, stream_video_id: str | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO processed_fixtures
                       (fixture_id, title, first_seen, stream_video_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(fixture_id) DO UPDATE SET
                       stream_video_id = COALESCE(excluded.stream_video_id,
                                                  processed_fixtures.stream_video_id)
                """,
                (fixture_id, title, self._now(), stream_video_id),
            )

    # ── events ────────────────────────────────────────────────────────────
    def is_event_processed(self, event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return row is not None

    def is_hash_seen(self, event_hash: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM event_hashes WHERE event_hash = ?", (event_hash,)
            ).fetchone()
        return row is not None

    def register_event(
        self, event_id: str, event_hash: str, fixture_id: int, event_type: str
    ) -> bool:
        """Atomically claim an event. Returns False if it was already claimed.

        This is the core dedup primitive: the first thread to insert wins, all
        others see ``False`` and skip — no double processing, ever.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "SELECT 1 FROM processed_events WHERE event_id = ? "
                "UNION SELECT 1 FROM event_hashes WHERE event_hash = ?",
                (event_id, event_hash),
            )
            if cur.fetchone() is not None:
                return False
            now = self._now()
            conn.execute(
                "INSERT INTO processed_events "
                "(event_id, fixture_id, event_type, created_at) VALUES (?,?,?,?)",
                (event_id, fixture_id, event_type, now),
            )
            conn.execute(
                "INSERT INTO event_hashes (event_hash, fixture_id, created_at) "
                "VALUES (?,?,?)",
                (event_hash, fixture_id, now),
            )
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()
