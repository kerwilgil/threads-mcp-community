"""Small client for the official Threads Graph API."""

from __future__ import annotations

import re

import httpx

from .errors import ProviderRejectedError, UnknownOutcomeError
from .models import Profile, ThreadPost
from .parsing import post_from_payload
from .transport import GraphTransport

_FIELDS = "id,text,media_type,media_url,thumbnail_url,permalink,timestamp"


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


class ThreadsClient:
    def __init__(
        self,
        bearer_token: str,
        *,
        api_base: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = GraphTransport(
            bearer_token,
            api_base=api_base,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ThreadsClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def get_profile(self) -> Profile:
        data = self._http.request_json(
            "GET",
            "/me",
            params={"fields": "id,username,name,threads_profile_picture_url,threads_biography"},
        )
        if not _valid_id(data.get("id")):
            raise ProviderRejectedError("Threads returned no valid account id.")
        return Profile(
            id=str(data.get("id") or ""),
            username=str(data.get("username") or ""),
            name=str(data.get("name") or ""),
            profile_picture_url=str(data.get("threads_profile_picture_url") or ""),
            biography=str(data.get("threads_biography") or ""),
        )

    def get_post(self, post_id: str) -> ThreadPost:
        if not _valid_id(post_id):
            raise ValueError("Invalid Threads post id.")
        data = self._http.request_json("GET", f"/{post_id}", params={"fields": _FIELDS})
        return post_from_payload(data)

    def list_recent_posts(self, limit: int = 10) -> list[ThreadPost]:
        data = self._http.request_json(
            "GET", "/me/threads", params={"fields": _FIELDS, "limit": max(1, min(int(limit), 100))}
        )
        rows = data.get("data")
        return [post_from_payload(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def create_text_container(self, user_id: str, text: str, *, topic_tag: str = "") -> str:
        if not _valid_id(user_id):
            raise ValueError("Invalid Threads account id.")
        payload = {"media_type": "TEXT", "text": text, "auto_publish_text": "false"}
        if topic_tag:
            payload["topic_tag"] = topic_tag
        return self._create_container(user_id, payload)

    def create_image_container(self, user_id: str, text: str, image_url: str, *, topic_tag: str = "") -> str:
        """Create an unpublished IMAGE container. link_attachment is not valid for images and is never sent."""
        if not _valid_id(user_id):
            raise ValueError("Invalid Threads account id.")
        payload = {"media_type": "IMAGE", "image_url": image_url, "text": text}
        if topic_tag:
            payload["topic_tag"] = topic_tag
        return self._create_container(user_id, payload)

    def _create_container(self, user_id: str, payload: dict[str, str]) -> str:
        data = self._http.request_json("POST", f"/{user_id}/threads", data=payload)
        creation_id = data.get("id")
        if not _valid_id(creation_id):
            raise ProviderRejectedError("Threads did not return a container id.")
        return creation_id

    def get_container_status(self, creation_id: str) -> str:
        """Read-only container status: IN_PROGRESS, FINISHED, ERROR, EXPIRED or PUBLISHED ('' if absent)."""
        if not _valid_id(creation_id):
            raise ValueError("Invalid Threads container id.")
        data = self._http.request_json("GET", f"/{creation_id}", params={"fields": "id,status"})
        status = data.get("status")
        return status if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,32}", status) else ""

    def publish_container(self, user_id: str, creation_id: str) -> str:
        if not _valid_id(user_id) or not _valid_id(creation_id):
            raise ValueError("Invalid Threads publication identifier.")
        data = self._http.request_json(
            "POST",
            f"/{user_id}/threads_publish",
            data={"creation_id": creation_id},
            may_publish=True,
        )
        post_id = data.get("id")
        if not _valid_id(post_id):
            raise UnknownOutcomeError("Threads returned no post id after publication; outcome is unknown.")
        return post_id
