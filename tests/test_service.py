from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from threads_mcp.config import Settings
from threads_mcp.errors import (
    AuthRequiredError,
    IdempotencyConflictError,
    ProviderRejectedError,
    SafeTransportError,
    UnknownOutcomeError,
)
from threads_mcp.ledger import PublicationLedger
from threads_mcp.models import Profile, ThreadPost, TokenRecord
from threads_mcp.service import ThreadsService


class MemoryTokenStore:
    def __init__(self) -> None:
        self.token = TokenRecord(access_token="secret", account_id="u1", username="example_user")

    def load(self) -> TokenRecord:
        return self.token

    def save(self, token: TokenRecord) -> None:
        self.token = token


class FakeClient:
    def __init__(self) -> None:
        self.publish_calls = 0
        self.create_calls = 0
        self.recent: list[ThreadPost] = []
        self.raise_unknown = False
        self.raise_safe = False

    def close(self) -> None:
        pass

    def get_profile(self) -> Profile:
        return Profile(id="u1", username="example_user")

    def create_text_container(self, user_id: str, text: str) -> str:
        assert user_id == "u1"
        self.create_calls += 1
        return "c1"

    def publish_container(self, user_id: str, creation_id: str) -> str:
        self.publish_calls += 1
        if self.raise_safe:
            raise SafeTransportError("unsent")
        if self.raise_unknown:
            raise UnknownOutcomeError("unknown")
        return "p1"

    def get_post(self, post_id: str) -> ThreadPost:
        return ThreadPost(id=post_id, text="hello", permalink="https://www.threads.net/@example_user/post/example")

    def list_recent_posts(self, limit: int = 10) -> list[ThreadPost]:
        return self.recent[:limit]


def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_base="https://graph.threads.net/v1.0",
        token_store_path=tmp_path / "token.enc",
        ledger_path=tmp_path / "ledger.sqlite3",
        oauth_state_path=tmp_path / "oauth.json",
        encryption_key="unused-in-test",
    )


def service(tmp_path: Path, fake: FakeClient) -> ThreadsService:
    return ThreadsService(
        settings(tmp_path),
        token_store=MemoryTokenStore(),  # type: ignore[arg-type]
        ledger=PublicationLedger(tmp_path / "ledger.sqlite3"),
        client_factory=lambda _: fake,  # type: ignore[arg-type]
    )


def test_publish_is_idempotent_after_success(tmp_path: Path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)

    first = svc.publish_text("hello", "post:12345678")
    second = svc.publish_text("hello", "post:12345678")

    assert first.status == "published"
    assert second.status == "published"
    assert fake.publish_calls == 1
    assert fake.create_calls == 1


def test_unknown_outcome_is_not_blindly_retried(tmp_path: Path) -> None:
    fake = FakeClient()
    fake.raise_unknown = True
    svc = service(tmp_path, fake)

    first = svc.publish_text("hello", "post:12345678")
    second = svc.publish_text("hello", "post:12345678")

    assert first.status == "unknown"
    assert second.status == "unknown"
    assert fake.publish_calls == 1


def test_single_identical_manual_post_is_insufficient_recovery_evidence(tmp_path: Path) -> None:
    fake = FakeClient()
    fake.raise_unknown = True
    fake.recent = [
        ThreadPost(
            id="p-recovered",
            text="hello",
            permalink="https://www.threads.net/@example_user/post/recovered",
            timestamp=datetime.now(UTC),
        )
    ]
    svc = service(tmp_path, fake)

    result = svc.publish_text("hello", "post:12345678")

    assert result.status == "unknown"
    assert result.recovered is False
    assert result.post_id == ""
    assert fake.publish_calls == 1


def test_safe_unsent_publish_reuses_same_container_on_retry(tmp_path: Path) -> None:
    fake = FakeClient()
    fake.raise_safe = True
    svc = service(tmp_path, fake)

    first = svc.publish_text("hello", "post:12345678")
    assert first.status == "failed"
    assert fake.create_calls == 1
    assert fake.publish_calls == 1

    fake.raise_safe = False
    second = svc.publish_text("hello", "post:12345678")
    assert second.status == "published"
    assert fake.create_calls == 1
    assert fake.publish_calls == 2


def test_in_progress_record_does_not_send_second_write(tmp_path: Path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)
    svc.ledger.reserve("post:12345678", "hello")
    claimed = svc.ledger.claim("post:12345678")
    assert claimed is not None

    result = svc.publish_text("hello", "post:12345678")

    assert result.status == "unknown"
    assert fake.create_calls == 0
    assert fake.publish_calls == 0

class Reject429Client(FakeClient):
    def publish_container(self, user_id: str, creation_id: str) -> str:
        from threads_mcp.errors import ProviderRejectedError

        self.publish_calls += 1
        raise ProviderRejectedError("rate limited", status_code=429)


def test_429_is_unknown_and_never_replayed(tmp_path: Path) -> None:
    fake = Reject429Client()
    svc = service(tmp_path, fake)

    first = svc.publish_text("hello", "post:12345678")
    assert first.status == "unknown"
    assert fake.create_calls == 1
    assert fake.publish_calls == 1

    # Swap in a healthy client while keeping the durable ledger.
    healthy = FakeClient()
    svc._client_factory = lambda _: healthy  # type: ignore[assignment]
    second = svc.publish_text("hello", "post:12345678")
    assert second.status == "unknown"
    assert healthy.create_calls == 0
    assert healthy.publish_calls == 0


def test_unknown_recovery_refuses_ambiguous_duplicate_matches(tmp_path: Path) -> None:
    fake = FakeClient()
    fake.raise_unknown = True
    now = datetime.now(UTC)
    fake.recent = [
        ThreadPost(id="p1", text="hello", timestamp=now),
        ThreadPost(id="p2", text="hello", timestamp=now),
    ]
    svc = service(tmp_path, fake)

    result = svc.publish_text("hello", "post:12345678")

    assert result.status == "unknown"
    assert fake.publish_calls == 1


def test_same_key_different_text_is_rejected_without_second_write(tmp_path: Path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)

    first = svc.publish_text("hello", "post:12345678")
    assert first.status == "published"

    with pytest.raises(IdempotencyConflictError):
        svc.publish_text("different content", "post:12345678")
    assert fake.publish_calls == 1


def test_concurrent_publishers_share_one_claim(tmp_path: Path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)
    svc.ledger.reserve("post:12345678", "hello")
    assert svc.ledger.claim("post:12345678") is not None

    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(svc.publish_text("hello", "post:12345678")))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    assert all(result.status == "unknown" for result in results)
    assert fake.create_calls == 0
    assert fake.publish_calls == 0


def _backdate_updated_at(ledger_path: Path, key: str, *, seconds: int) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute("UPDATE publications SET updated_at = ? WHERE idempotency_key = ?", (old, key))
        conn.commit()
    finally:
        conn.close()


def test_crash_around_publish_started_stays_unknown_without_new_write(tmp_path: Path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)
    svc.ledger.reserve("post:12345678", "hello")
    assert svc.ledger.claim("post:12345678") is not None
    owner = svc.ledger.get("post:12345678").owner
    svc.ledger.set_status("post:12345678", "container_created", owner=owner, creation_id="c1")
    svc.ledger.set_status("post:12345678", "publish_started", owner=owner, creation_id="c1", error="")

    restarted = service(tmp_path, fake)
    result = restarted.publish_text("hello", "post:12345678")

    assert result.status == "unknown"
    assert fake.create_calls == 0
    assert fake.publish_calls == 0


def test_restart_keeps_single_unattributed_candidate_unknown(tmp_path: Path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)
    svc.ledger.reserve("post:12345678", "hello")
    assert svc.ledger.claim("post:12345678") is not None
    owner = svc.ledger.get("post:12345678").owner
    svc.ledger.set_status("post:12345678", "container_created", owner=owner, creation_id="c1")
    svc.ledger.set_status("post:12345678", "publish_started", owner=owner, creation_id="c1", error="")

    fake.recent = [
        ThreadPost(
            id="p-recovered",
            text="hello",
            permalink="https://www.threads.net/@example_user/post/recovered",
            timestamp=datetime.now(UTC),
        )
    ]
    restarted = service(tmp_path, fake)
    result = restarted.publish_text("hello", "post:12345678")

    assert result.status == "unknown"
    assert result.recovered is False
    assert result.post_id == ""
    assert fake.publish_calls == 0


def test_stale_creating_record_resumes_after_restart(tmp_path: Path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)
    svc.ledger.reserve("post:12345678", "hello")
    assert svc.ledger.claim("post:12345678") is not None
    _backdate_updated_at(tmp_path / "ledger.sqlite3", "post:12345678", seconds=3600)

    restarted = service(tmp_path, fake)
    result = restarted.publish_text("hello", "post:12345678")

    assert result.status == "published"
    assert fake.create_calls == 1
    assert fake.publish_calls == 1


def test_unknown_recovery_ignores_stale_and_future_duplicates(tmp_path: Path) -> None:
    fake = FakeClient()
    fake.raise_unknown = True
    now = datetime.now(UTC)
    fake.recent = [
        ThreadPost(id="p-old", text="hello", timestamp=now - timedelta(hours=2)),
        ThreadPost(id="p-future", text="hello", timestamp=now + timedelta(days=1)),
    ]
    svc = service(tmp_path, fake)

    result = svc.publish_text("hello", "post:12345678")

    assert result.status == "unknown"
    assert fake.publish_calls == 1


class ExplodingTokenStore:
    def load(self) -> TokenRecord:
        raise AuthRequiredError("No Threads token is configured.")

    def save(self, token: TokenRecord) -> None:
        pass


def test_setup_failure_after_claim_is_retryable(tmp_path: Path) -> None:
    fake = FakeClient()
    broken = ThreadsService(
        settings(tmp_path),
        token_store=ExplodingTokenStore(),  # type: ignore[arg-type]
        ledger=PublicationLedger(tmp_path / "ledger.sqlite3"),
        client_factory=lambda _: fake,  # type: ignore[arg-type]
    )
    first = broken.publish_text("hello", "post:12345678")
    assert first.status == "failed"
    assert fake.create_calls == 0
    assert fake.publish_calls == 0

    record = broken.ledger.get("post:12345678")
    assert record is not None
    assert record.status == "retryable"

    restarted = service(tmp_path, fake)
    second = restarted.publish_text("hello", "post:12345678")
    assert second.status == "published"
    assert fake.create_calls == 1
    assert fake.publish_calls == 1


class ContainerFailureClient(FakeClient):
    status_code = 500

    def create_text_container(self, user_id: str, text: str) -> str:
        self.create_calls += 1
        raise ProviderRejectedError("container failed", status_code=self.status_code, definitive=True)


def test_container_500_is_retryable_with_new_container(tmp_path: Path) -> None:
    fake = ContainerFailureClient()
    svc = service(tmp_path, fake)

    first = svc.publish_text("hello", "post:12345678")
    assert first.status == "failed"
    record = svc.ledger.get("post:12345678")
    assert record is not None
    assert record.status == "retryable"

    healthy = FakeClient()
    svc._client_factory = lambda _: healthy  # type: ignore[assignment]
    second = svc.publish_text("hello", "post:12345678")
    assert second.status == "published"
    assert healthy.create_calls == 1
    assert healthy.publish_calls == 1


def test_container_400_is_terminal(tmp_path: Path) -> None:
    fake = ContainerFailureClient()
    fake.status_code = 400
    svc = service(tmp_path, fake)

    first = svc.publish_text("hello", "post:12345678")
    assert first.status == "failed"
    record = svc.ledger.get("post:12345678")
    assert record is not None
    assert record.status == "failed"

    healthy = FakeClient()
    svc._client_factory = lambda _: healthy  # type: ignore[assignment]
    second = svc.publish_text("hello", "post:12345678")
    assert second.status == "failed"
    assert healthy.create_calls == 0
    assert healthy.publish_calls == 0


class Publish400Client(FakeClient):
    def publish_container(self, user_id: str, creation_id: str) -> str:
        self.publish_calls += 1
        raise ProviderRejectedError("bad request", status_code=400, definitive=True)


def test_publish_400_is_terminal_without_retry(tmp_path: Path) -> None:
    fake = Publish400Client()
    svc = service(tmp_path, fake)

    first = svc.publish_text("hello", "post:12345678")
    assert first.status == "failed"
    record = svc.ledger.get("post:12345678")
    assert record is not None
    assert record.status == "failed"

    healthy = FakeClient()
    svc._client_factory = lambda _: healthy  # type: ignore[assignment]
    second = svc.publish_text("hello", "post:12345678")
    assert second.status == "failed"
    assert healthy.create_calls == 0
    assert healthy.publish_calls == 0
