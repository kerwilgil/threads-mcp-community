from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .errors import ClaimLostError, IdempotencyConflictError
from .ledger_models import PublicationRecord, content_hash, row_to_record

Connect = Callable[[], AbstractContextManager[sqlite3.Connection]]


def reserve(
    connect: Connect, key: str, text: str, *, image_url: str = "", topic_tag: str = ""
) -> PublicationRecord:
    digest = content_hash(text, image_url, topic_tag)
    now = datetime.now(UTC).isoformat()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM publications WHERE idempotency_key = ?", (key,)).fetchone()
        if row and row["text_sha256"] != digest:
            raise IdempotencyConflictError("The idempotency key was already used for different Threads content.")
        if not row:
            conn.execute(
                "INSERT INTO publications "
                "(idempotency_key,text,text_sha256,status,started_at,updated_at,image_url,topic_tag) "
                "VALUES (?,?,?,'reserved',?,?,?,?)",
                (key, text, digest, now, now, image_url, topic_tag),
            )
        result = row_to_record(conn.execute("SELECT * FROM publications WHERE idempotency_key = ?", (key,)).fetchone())
        conn.commit()
        return result


def claim(connect: Connect, key: str, stale_after_seconds: int) -> PublicationRecord | None:
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=stale_after_seconds)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM publications WHERE idempotency_key = ?", (key,)).fetchone()
        if not row:
            conn.rollback()
            return None
        status = row["status"]
        updated = datetime.fromisoformat(row["updated_at"])
        allowed = status in {"reserved", "retryable"} or (
            status in {"creating", "container_created"} and updated <= stale_before
        )
        if not allowed:
            conn.rollback()
            return None
        conn.execute(
            "UPDATE publications SET status='creating',updated_at=?,owner=?,error='' WHERE idempotency_key=?",
            (now.isoformat(), uuid4().hex, key),
        )
        result = row_to_record(conn.execute("SELECT * FROM publications WHERE idempotency_key = ?", (key,)).fetchone())
        conn.commit()
        return result


def set_status(
    connect: Connect, key: str, status: str, *, owner: str, **fields: str | None
) -> PublicationRecord:
    assignments = ["status = ?", "updated_at = ?"]
    values: list[str] = [status, datetime.now(UTC).isoformat()]
    for column in ("creation_id", "post_id", "permalink", "error", "account_id"):
        value = fields.get(column)
        if value is not None:
            assignments.append(f"{column} = ?")
            values.append(value)
    values.append(key)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM publications WHERE idempotency_key = ?", (key,)).fetchone()
        if row is None:
            raise ClaimLostError("The publication record is unavailable; no write was sent.")
        allowed = {
            "reserved": {"retryable", "failed", "unknown"},
            "creating": {"creating", "container_created", "retryable", "failed"},
            # `unknown` covers a media container that reports PUBLISHED before our publish call.
            "container_created": {"publish_started", "retryable", "failed", "unknown"},
            "publish_started": {"published", "unknown", "retryable", "failed"},
            "published": {"published"},
        }
        if row["owner"] != owner or status not in allowed.get(row["status"], set()):
            raise ClaimLostError("The publication claim changed; no duplicate write was sent.")
        if row["status"] == "published" and fields.get("post_id", row["post_id"]) != row["post_id"]:
            raise ClaimLostError("A confirmed publication id cannot be replaced.")
        conn.execute(f"UPDATE publications SET {', '.join(assignments)} WHERE idempotency_key = ?", values)
        result = row_to_record(conn.execute("SELECT * FROM publications WHERE idempotency_key = ?", (key,)).fetchone())
        conn.commit()
        return result
