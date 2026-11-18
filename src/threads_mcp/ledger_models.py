from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PublicationRecord:
    idempotency_key: str
    text: str
    text_sha256: str
    status: str
    creation_id: str
    post_id: str
    permalink: str
    started_at: datetime
    updated_at: datetime
    error: str
    owner: str
    account_id: str
    image_url: str = ""
    topic_tag: str = ""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(text: str, image_url: str = "", topic_tag: str = "") -> str:
    """Digest binding everything that becomes public.

    Plain text posts keep the historical sha256(text) so existing ledger rows stay valid.
    Any media or topic tag is bound through a canonical JSON document instead.
    """
    if not image_url and not topic_tag:
        return text_hash(text)
    document = json.dumps(
        {"image_url": image_url, "text": text, "topic_tag": topic_tag, "v": 2},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def row_to_record(row: sqlite3.Row) -> PublicationRecord:
    return PublicationRecord(
        idempotency_key=row["idempotency_key"],
        text=row["text"],
        text_sha256=row["text_sha256"],
        status=row["status"],
        creation_id=row["creation_id"],
        post_id=row["post_id"],
        permalink=row["permalink"],
        started_at=datetime.fromisoformat(row["started_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error=row["error"],
        owner=row["owner"],
        account_id=row["account_id"],
        image_url=row["image_url"] if "image_url" in row.keys() else "",
        topic_tag=row["topic_tag"] if "topic_tag" in row.keys() else "",
    )
