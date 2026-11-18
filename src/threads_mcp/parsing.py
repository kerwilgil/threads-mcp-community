from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import ThreadPost


def post_from_payload(payload: dict[str, Any]) -> ThreadPost:
    timestamp = None
    if payload.get("timestamp"):
        try:
            timestamp = datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))
        except ValueError:
            pass
    return ThreadPost(
        id=str(payload.get("id") or ""),
        text=str(payload.get("text") or ""),
        media_type=str(payload.get("media_type") or ""),
        media_url=str(payload.get("media_url") or ""),
        thumbnail_url=str(payload.get("thumbnail_url") or ""),
        permalink=str(payload.get("permalink") or ""),
        timestamp=timestamp,
    )
