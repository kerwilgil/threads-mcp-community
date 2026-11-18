"""Persistent publication ledger used to prevent duplicate writes."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .ledger_models import PublicationRecord, row_to_record
from .ledger_ops import claim, reserve, set_status


class PublicationLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS publications ("
                "idempotency_key TEXT PRIMARY KEY,text TEXT NOT NULL,text_sha256 TEXT NOT NULL,"
                "status TEXT NOT NULL,creation_id TEXT NOT NULL DEFAULT '',post_id TEXT NOT NULL DEFAULT '',"
                "permalink TEXT NOT NULL DEFAULT '',started_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
                "error TEXT NOT NULL DEFAULT '')"
            )
            # Upgrade existing v1 ledgers without deleting publication evidence.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(publications)")}
            for name in ("owner", "account_id", "image_url", "topic_tag"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE publications ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            deadline = time.monotonic() + 10
            while True:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    # SQLite may bypass its busy handler when concurrent first
                    # connections upgrade journal mode. Retry only lock contention.
                    if (
                        getattr(exc, "sqlite_errorcode", 0) & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.01)
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self, key: str) -> PublicationRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM publications WHERE idempotency_key = ?", (key,)).fetchone()
        return row_to_record(row) if row else None

    def reserve(self, key: str, text: str, *, image_url: str = "", topic_tag: str = "") -> PublicationRecord:
        return reserve(self._connect, key, text, image_url=image_url, topic_tag=topic_tag)

    def claim(self, key: str, *, stale_after_seconds: int = 120) -> PublicationRecord | None:
        return claim(self._connect, key, stale_after_seconds)

    def set_status(self, key: str, status: str, *, owner: str, **fields: str | None) -> PublicationRecord:
        return set_status(self._connect, key, status, owner=owner, **fields)
