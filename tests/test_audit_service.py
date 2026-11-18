from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_service import FakeClient, MemoryTokenStore, _backdate_updated_at, service, settings

from threads_mcp.errors import ClaimLostError, ProviderRejectedError
from threads_mcp.ledger import PublicationLedger
from threads_mcp.models import ThreadPost
from threads_mcp.service import ThreadsService

KEY = "post:audit123"


def test_independent_concurrent_callers_publish_only_once(tmp_path):
    barrier = threading.Barrier(8)
    fake = FakeClient()
    services = [service(tmp_path, fake) for _ in range(8)]

    def publish(svc):
        barrier.wait(timeout=10)
        return svc.publish_text("hello", KEY)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, services))
    assert all(result.status in {"published", "unknown"} for result in results)
    assert fake.create_calls == fake.publish_calls == 1
    assert services[0].ledger.get(KEY).status == "published"


def test_slow_original_worker_cannot_publish_after_stale_reclaim(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class SlowClient(FakeClient):
        def create_text_container(self, user_id, text):
            entered.set()
            assert release.wait(timeout=10)
            return super().create_text_container(user_id, text)

    slow = SlowClient()
    healthy = FakeClient()
    first = service(tmp_path, slow)
    second = service(tmp_path, healthy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(first.publish_text, "hello", KEY)
        try:
            assert entered.wait(timeout=10)
            old_owner = first.ledger.get(KEY).owner
            _backdate_updated_at(tmp_path / "ledger.sqlite3", KEY, seconds=3600)
            assert second.publish_text("hello", KEY).status == "published"
            assert second.ledger.get(KEY).owner != old_owner
        finally:
            release.set()
        assert pending.result(timeout=10).status == "published"
    assert slow.publish_calls == 0
    assert healthy.publish_calls == 1
    assert first.ledger.get(KEY).post_id == "p1"


def test_stale_owner_cannot_reset_new_claim_or_cross_publish_boundary(tmp_path):
    ledger = PublicationLedger(tmp_path / "ledger.sqlite3")
    ledger.reserve(KEY, "hello")
    old = ledger.claim(KEY)
    _backdate_updated_at(ledger.path, KEY, seconds=3600)
    fresh = ledger.claim(KEY)
    assert old.owner != fresh.owner
    for status in ("container_created", "publish_started", "retryable", "failed", "published", "unknown"):
        with pytest.raises(ClaimLostError):
            ledger.set_status(KEY, status, owner=old.owner)
    assert ledger.get(KEY).owner == fresh.owner
    assert ledger.get(KEY).status == "creating"


@pytest.mark.parametrize("after_send", [False, True])
def test_process_crash_at_public_boundary_never_replays(tmp_path, after_send):
    script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_service import FakeClient, service
root = Path(sys.argv[2])
class CrashClient(FakeClient):
    def publish_container(self, user_id, creation_id):
        assert svc.ledger.get('post:audit123').status == 'publish_started'
        if sys.argv[3] == 'sent':
            (root / 'public-call.txt').write_text('one simulated public call')
        os._exit(17)
svc = service(root, CrashClient())
svc.publish_text('hello', 'post:audit123')
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).parent), str(tmp_path), "sent" if after_send else "unsent"],
        capture_output=True, text=True, timeout=20,
    )
    assert child.returncode == 17, child.stderr
    assert (tmp_path / "public-call.txt").exists() == after_send
    fake = FakeClient()
    restarted = service(tmp_path, fake)
    assert restarted.ledger.get(KEY).status == "publish_started"
    for _ in range(3):
        assert restarted.publish_text("hello", KEY).status == "unknown"
    assert fake.create_calls == fake.publish_calls == 0


@pytest.mark.parametrize("count", [0, 1, 2])
def test_unknown_with_zero_one_multiple_candidates_has_no_second_write(tmp_path, count):
    fake = FakeClient()
    fake.raise_unknown = True
    fake.recent = [ThreadPost(id=f"p{i}", text="hello", timestamp=datetime.now(UTC)) for i in range(count)]
    first = service(tmp_path, fake)
    assert first.publish_text("hello", KEY).status == "unknown"
    restarted = service(tmp_path, fake)
    # Read-only tools remain available for human verification.
    assert len(restarted.list_recent_posts()) == count
    for _ in range(3):
        result = restarted.publish_text("hello", KEY)
        assert result.status == "unknown"
        assert not result.recovered and not result.post_id
    assert fake.publish_calls == 1


@pytest.mark.parametrize("kind", ["setup", "profile", "container", "publish"])
def test_arbitrary_exception_text_never_reaches_result_or_ledger(tmp_path, kind):
    class BrokenClient(FakeClient):
        def get_profile(self):
            if kind == "profile":
                raise RuntimeError("unlabeled-sensitive-oauth-credential")
            return super().get_profile()

        def create_text_container(self, user_id, text):
            if kind == "container":
                raise RuntimeError("unlabeled-sensitive-oauth-credential")
            return super().create_text_container(user_id, text)

        def publish_container(self, user_id, creation_id):
            if kind == "publish":
                self.publish_calls += 1
                raise RuntimeError("unlabeled-sensitive-oauth-credential")
            return super().publish_container(user_id, creation_id)

    fake = BrokenClient()
    store = MemoryTokenStore()
    if kind == "profile":
        store.token = store.token.model_copy(update={"account_id": ""})

    def factory(_):
        if kind == "setup":
            raise RuntimeError("unlabeled-sensitive-oauth-credential")
        return fake

    svc = ThreadsService(settings(tmp_path), token_store=store, client_factory=factory)
    result = svc.publish_text("hello", KEY)
    assert result.status == ("unknown" if kind == "publish" else "failed")
    assert "unlabeled-sensitive" not in result.model_dump_json()
    assert "unlabeled-sensitive" not in svc.ledger.get(KEY).error


def test_success_is_durable_before_permalink_lookup(tmp_path):
    class CrashDuringLookup(FakeClient):
        def get_post(self, post_id):
            assert svc.ledger.get(KEY).status == "published"
            raise SystemExit("simulated crash")

    fake = CrashDuringLookup()
    svc = service(tmp_path, fake)
    with pytest.raises(SystemExit):
        svc.publish_text("hello", KEY)
    healthy = FakeClient()
    result = service(tmp_path, healthy).publish_text("hello", KEY)
    assert result.status == "published" and result.post_id == "p1"
    assert healthy.publish_calls == healthy.create_calls == 0


def test_failure_to_persist_public_boundary_sends_no_public_write(tmp_path, monkeypatch):
    fake = FakeClient()
    svc = service(tmp_path, fake)
    original = svc.ledger.set_status

    def fail(key, status, **fields):
        if status == "publish_started":
            raise OSError("disk full")
        return original(key, status, **fields)

    monkeypatch.setattr(svc.ledger, "set_status", fail)
    with pytest.raises(OSError):
        svc.publish_text("hello", KEY)
    assert fake.publish_calls == 0
    assert svc.ledger.get(KEY).status == "container_created"


def test_failure_to_save_success_leaves_durable_ambiguity_and_never_replays(tmp_path, monkeypatch):
    fake = FakeClient()
    svc = service(tmp_path, fake)
    original = svc.ledger.set_status

    def fail(key, status, **fields):
        if status == "published":
            raise OSError("disk full")
        return original(key, status, **fields)

    monkeypatch.setattr(svc.ledger, "set_status", fail)
    with pytest.raises(OSError):
        svc.publish_text("hello", KEY)
    assert svc.ledger.get(KEY).status == "publish_started"
    assert service(tmp_path, fake).publish_text("hello", KEY).status == "unknown"
    assert fake.publish_calls == 1


def test_retryable_container_is_bound_to_original_account(tmp_path):
    fake = FakeClient()
    fake.raise_safe = True
    svc = service(tmp_path, fake)
    assert svc.publish_text("hello", KEY).status == "failed"
    svc.token_store.token = svc.token_store.token.model_copy(update={"account_id": "different-account"})
    fake.raise_safe = False
    result = svc.publish_text("hello", KEY)
    assert result.status == "failed"
    assert "account changed" in result.message
    assert fake.publish_calls == 1


def test_unverified_4xx_exception_cannot_be_terminal_proof(tmp_path):
    class Unverified(FakeClient):
        def publish_container(self, user_id, creation_id):
            self.publish_calls += 1
            raise ProviderRejectedError("raw-credential", status_code=400)

    fake = Unverified()
    svc = service(tmp_path, fake)
    first = svc.publish_text("hello", KEY)
    assert first.status == "unknown"
    assert "raw-credential" not in first.model_dump_json()
    assert "raw-credential" not in svc.ledger.get(KEY).error
    assert svc.publish_text("hello", KEY).status == "unknown"
    assert fake.publish_calls == 1


def test_expired_token_does_not_refresh_or_publish(tmp_path):
    fake = FakeClient()
    svc = service(tmp_path, fake)
    svc.token_store.token = svc.token_store.token.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(days=1)}
    )

    def forbidden(_):
        pytest.fail("Expired tokens cannot be refreshed.")

    svc._token_refresher = forbidden
    assert svc.publish_text("hello", KEY).status == "failed"
    assert fake.create_calls == fake.publish_calls == 0
