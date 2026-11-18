"""Image + topic_tag publication: same idempotency and no-blind-retry guarantees as text posts."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from test_service import FakeClient, MemoryTokenStore, settings

from threads_mcp import server
from threads_mcp.client import ThreadsClient
from threads_mcp.errors import IdempotencyConflictError, UnknownOutcomeError
from threads_mcp.ledger import PublicationLedger
from threads_mcp.ledger_models import content_hash, text_hash
from threads_mcp.service import CONTAINER_POLL_ATTEMPTS, ThreadsService

IMG = "https://example.com/media/demo.jpg"
KEY = "demo-2026-02-03-test-v1"


class ImageFakeClient(FakeClient):
    def __init__(self, statuses: list[str] | None = None) -> None:
        super().__init__()
        self.statuses = list(statuses or ["FINISHED"])
        self.image_calls: list[tuple[str, str, str]] = []
        self.text_tags: list[str] = []
        self.status_calls = 0
        self.next_container = 0

    def create_text_container(self, user_id: str, text: str, *, topic_tag: str = "") -> str:
        self.text_tags.append(topic_tag)
        return super().create_text_container(user_id, text)

    def create_image_container(self, user_id: str, text: str, image_url: str, *, topic_tag: str = "") -> str:
        assert user_id == "u1"
        self.create_calls += 1
        self.next_container += 1
        self.image_calls.append((text, image_url, topic_tag))
        return f"c{self.next_container}"

    def get_container_status(self, creation_id: str) -> str:
        self.status_calls += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def make(tmp_path: Path, fake: FakeClient) -> tuple[ThreadsService, list[float]]:
    sleeps: list[float] = []
    svc = ThreadsService(
        settings(tmp_path),
        token_store=MemoryTokenStore(),  # type: ignore[arg-type]
        ledger=PublicationLedger(tmp_path / "ledger.sqlite3"),
        client_factory=lambda _: fake,  # type: ignore[arg-type]
        sleeper=sleeps.append,
    )
    return svc, sleeps


def test_image_publish_waits_for_finished_then_publishes_once(tmp_path: Path) -> None:
    fake = ImageFakeClient(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    svc, sleeps = make(tmp_path, fake)
    first = svc.publish_image("hello img", IMG, KEY, "AIThreads")
    again = svc.publish_image("hello img", IMG, KEY, "AIThreads")
    assert first.status == again.status == "published"
    assert fake.image_calls == [("hello img", IMG, "AIThreads")]
    assert fake.publish_calls == 1 and fake.status_calls == 3 and len(sleeps) == 2
    record = svc.ledger.get(KEY)
    assert record and record.image_url == IMG and record.topic_tag == "AIThreads"
    assert record.text_sha256 == content_hash("hello img", IMG, "AIThreads") != text_hash("hello img")


def test_image_not_ready_is_retryable_and_reuses_container(tmp_path: Path) -> None:
    fake = ImageFakeClient(["IN_PROGRESS"])
    svc, _ = make(tmp_path, fake)
    result = svc.publish_image("hello img", IMG, KEY)
    assert result.status == "failed" and "not ready" in result.message
    assert fake.publish_calls == 0 and fake.status_calls == CONTAINER_POLL_ATTEMPTS
    assert svc.ledger.get(KEY).status == "retryable"
    fake.statuses = ["FINISHED"]
    assert svc.publish_image("hello img", IMG, KEY).status == "published"
    assert fake.create_calls == 1 and fake.publish_calls == 1


def test_image_error_container_is_not_published_and_next_retry_uses_fresh_container(tmp_path: Path) -> None:
    fake = ImageFakeClient(["ERROR"])
    svc, _ = make(tmp_path, fake)
    result = svc.publish_image("hello img", IMG, KEY)
    assert result.status == "failed" and "ERROR" in result.message
    record = svc.ledger.get(KEY)
    assert record.status == "retryable" and record.creation_id == ""
    fake.statuses = ["FINISHED"]
    assert svc.publish_image("hello img", IMG, KEY).status == "published"
    assert fake.create_calls == 2 and fake.publish_calls == 1


def test_container_already_published_is_unknown_and_never_replayed(tmp_path: Path) -> None:
    fake = ImageFakeClient(["PUBLISHED"])
    svc, _ = make(tmp_path, fake)
    assert svc.publish_image("hello img", IMG, KEY).status == "unknown"
    assert svc.publish_image("hello img", IMG, KEY).status == "unknown"
    assert fake.publish_calls == 0 and fake.create_calls == 1


def test_unknown_image_publish_is_not_blindly_retried(tmp_path: Path) -> None:
    fake = ImageFakeClient(["FINISHED"])
    fake.raise_unknown = True
    svc, _ = make(tmp_path, fake)
    assert svc.publish_image("hello img", IMG, KEY).status == "unknown"
    fake.raise_unknown = False
    assert svc.publish_image("hello img", IMG, KEY).status == "unknown"
    assert fake.publish_calls == 1


@pytest.mark.parametrize("other", [
    ("hello img", "https://example.com/media/other.jpg", "AIThreads"),
    ("hello img", IMG, "OtherTag"),
    ("hello img", IMG, ""),
])
def test_same_key_with_different_media_or_tag_is_a_conflict(tmp_path: Path, other) -> None:
    fake = ImageFakeClient()
    svc, _ = make(tmp_path, fake)
    svc.publish_image("hello img", IMG, KEY, "AIThreads")
    with pytest.raises(IdempotencyConflictError):
        svc.publish_image(other[0], other[1], KEY, other[2])
    with pytest.raises(IdempotencyConflictError):
        svc.publish_text("hello img", KEY)
    assert fake.publish_calls == 1


def test_text_topic_tag_is_sent_and_bound(tmp_path: Path) -> None:
    fake = ImageFakeClient()
    svc, _ = make(tmp_path, fake)
    assert svc.publish_text("hello", KEY, "AIThreads").status == "published"
    assert fake.text_tags == ["AIThreads"] and fake.status_calls == 0
    with pytest.raises(IdempotencyConflictError):
        svc.publish_text("hello", KEY)


def test_plain_text_keeps_legacy_hash(tmp_path: Path) -> None:
    fake = ImageFakeClient()
    svc, _ = make(tmp_path, fake)
    svc.publish_text("hello", KEY)
    assert svc.ledger.get(KEY).text_sha256 == text_hash("hello")


@pytest.mark.parametrize("url,tag", [
    ("http://example.com/a.jpg", ""), ("https://", ""), (" https://a.b/c.jpg", ""),
    ("https://user:pw@a.b/c.jpg", ""), (IMG, "AI.Threads"), (IMG, "A&B"), (IMG, "x" * 51), (IMG, "two words"),
])
def test_invalid_image_or_tag_rejected_before_any_write(tmp_path: Path, url: str, tag: str) -> None:
    fake = ImageFakeClient()
    svc, _ = make(tmp_path, fake)
    with pytest.raises(ValueError):
        svc.publish_image("hello", url, KEY, tag)
    assert svc.ledger.get(KEY) is None and fake.create_calls == 0


def test_legacy_ledger_is_upgraded_without_losing_rows(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE publications (idempotency_key TEXT PRIMARY KEY,text TEXT NOT NULL,text_sha256 TEXT NOT NULL,"
        "status TEXT NOT NULL,creation_id TEXT NOT NULL DEFAULT '',post_id TEXT NOT NULL DEFAULT '',"
        "permalink TEXT NOT NULL DEFAULT '',started_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
        "error TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO publications VALUES ('old-key-0001','hi',?, 'published','c','p','', "
        "'2026-11-01T00:00:00+00:00','2026-11-01T00:00:00+00:00','')",
        (text_hash("hi"),),
    )
    conn.commit()
    conn.close()
    ledger = PublicationLedger(path)
    record = ledger.reserve("old-key-0001", "hi")
    assert record.status == "published" and record.image_url == "" and record.topic_tag == ""


def test_client_image_container_payload_and_status() -> None:
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        seen.append((request.method, request.url.path, body or dict(request.url.params)))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "c1"})
        return httpx.Response(200, json={"id": "c1", "status": "FINISHED"})

    client = ThreadsClient("tok", api_base="https://graph.threads.net/v1.0", transport=httpx.MockTransport(handler))
    assert client.create_image_container("u1", "t", IMG, topic_tag="AIThreads") == "c1"
    assert client.create_text_container("u1", "t", topic_tag="AIThreads") == "c1"
    assert client.get_container_status("c1") == "FINISHED"
    image_body = seen[0][2]
    assert image_body == {"media_type": "IMAGE", "image_url": IMG, "text": "t", "topic_tag": "AIThreads"}
    assert "link_attachment" not in image_body and "auto_publish_text" not in image_body
    assert seen[1][2]["topic_tag"] == "AIThreads" and seen[1][2]["media_type"] == "TEXT"
    assert seen[2][:2] == ("GET", "/v1.0/c1") and seen[2][2]["fields"] == "id,status"


def test_client_container_status_never_publishes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and not request.url.path.endswith("threads_publish")
        raise httpx.ReadTimeout("slow", request=request)

    client = ThreadsClient("tok", api_base="https://graph.threads.net/v1.0", transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as caught:
        client.get_container_status("c1")
    assert not isinstance(caught.value, UnknownOutcomeError)


def test_server_image_round_trip(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    fake = ImageFakeClient()
    svc, _ = make(tmp_path, fake)
    monkeypatch.setattr(server, "_service", lambda: svc)
    args = {"text": "hello img", "image_url": IMG, "idempotency_key": KEY, "topic_tag": "AIThreads"}
    for _ in range(2):
        result = asyncio.run(server.mcp.call_tool("threads_publish_image", args))
        assert result.structured_content["status"] == "published"
    assert fake.publish_calls == 1
