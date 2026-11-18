import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from threads_mcp.errors import ClaimLostError, IdempotencyConflictError
from threads_mcp.ledger import PublicationLedger


def test_reserve_is_stable_for_same_text(tmp_path: Path) -> None:
    ledger = PublicationLedger(tmp_path / "ledger.sqlite3")
    first = ledger.reserve("post:12345678", "hello")
    second = ledger.reserve("post:12345678", "hello")
    assert first.idempotency_key == second.idempotency_key
    assert second.status == "reserved"


def test_reusing_key_for_different_text_is_rejected(tmp_path: Path) -> None:
    ledger = PublicationLedger(tmp_path / "ledger.sqlite3")
    ledger.reserve("post:12345678", "hello")
    with pytest.raises(IdempotencyConflictError):
        ledger.reserve("post:12345678", "different")


def test_failed_record_stays_terminal(tmp_path: Path) -> None:
    ledger = PublicationLedger(tmp_path / "ledger.sqlite3")
    ledger.reserve("post:12345678", "hello")
    ledger.set_status("post:12345678", "failed", owner="", error="definitive rejection")
    retried = ledger.reserve("post:12345678", "hello")
    assert retried.status == "failed"
    assert retried.error == "definitive rejection"


def test_claim_is_atomic_and_not_reentrant(tmp_path: Path) -> None:
    ledger = PublicationLedger(tmp_path / "ledger.sqlite3")
    ledger.reserve("post:12345678", "hello")
    first = ledger.claim("post:12345678")
    second = ledger.claim("post:12345678")
    assert first is not None
    assert first.status == "creating"
    assert second is None


def test_retryable_record_can_be_claimed(tmp_path: Path) -> None:
    ledger = PublicationLedger(tmp_path / "ledger.sqlite3")
    ledger.reserve("post:12345678", "hello")
    ledger.set_status("post:12345678", "retryable", owner="", creation_id="c1", error="unsent")
    claimed = ledger.claim("post:12345678")
    assert claimed is not None
    assert claimed.status == "creating"
    assert claimed.creation_id == "c1"


def _backdate_updated_at(ledger_path: Path, key: str, *, seconds: int) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    conn = sqlite3.connect(ledger_path)
    try:
        conn.execute("UPDATE publications SET updated_at = ? WHERE idempotency_key = ?", (old, key))
        conn.commit()
    finally:
        conn.close()


def test_fresh_creating_record_is_not_reentrant(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = PublicationLedger(path)
    ledger.reserve("post:12345678", "hello")
    assert ledger.claim("post:12345678") is not None

    restarted = PublicationLedger(path)
    assert restarted.claim("post:12345678") is None


def test_stale_creating_record_can_be_reclaimed_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = PublicationLedger(path)
    ledger.reserve("post:12345678", "hello")
    assert ledger.claim("post:12345678") is not None
    _backdate_updated_at(path, "post:12345678", seconds=3600)

    restarted = PublicationLedger(path)
    claimed = restarted.claim("post:12345678")
    assert claimed is not None
    assert claimed.status == "creating"


def test_stale_container_created_record_can_be_reclaimed_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = PublicationLedger(path)
    ledger.reserve("post:12345678", "hello")
    assert ledger.claim("post:12345678") is not None
    ledger.set_status(
        "post:12345678", "container_created", owner=ledger.get("post:12345678").owner, creation_id="c1", error=""
    )
    _backdate_updated_at(path, "post:12345678", seconds=3600)

    restarted = PublicationLedger(path)
    claimed = restarted.claim("post:12345678")
    assert claimed is not None
    assert claimed.creation_id == "c1"


def test_publish_started_is_never_reclaimable(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = PublicationLedger(path)
    ledger.reserve("post:12345678", "hello")
    assert ledger.claim("post:12345678") is not None
    owner = ledger.get("post:12345678").owner
    ledger.set_status("post:12345678", "container_created", owner=owner, creation_id="c1")
    ledger.set_status("post:12345678", "publish_started", owner=owner, creation_id="c1", error="")
    _backdate_updated_at(path, "post:12345678", seconds=86400)

    restarted = PublicationLedger(path)
    assert restarted.claim("post:12345678") is None


def test_unknown_is_never_reclaimable(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = PublicationLedger(path)
    ledger.reserve("post:12345678", "hello")
    ledger.set_status("post:12345678", "unknown", owner="", creation_id="c1", error="ambiguous")
    _backdate_updated_at(path, "post:12345678", seconds=86400)

    restarted = PublicationLedger(path)
    assert restarted.claim("post:12345678") is None


def test_unknown_cannot_be_reset_even_with_current_owner(tmp_path: Path) -> None:
    ledger = PublicationLedger(tmp_path / "ledger.sqlite3")
    ledger.reserve("post:12345678", "hello")
    ledger.set_status("post:12345678", "unknown", owner="")
    for status in ("reserved", "retryable", "creating", "container_created", "publish_started", "published"):
        with pytest.raises(ClaimLostError):
            ledger.set_status("post:12345678", status, owner="")
    assert ledger.get("post:12345678").status == "unknown"


@pytest.mark.parametrize("status", ["published", "publish_started", "unknown", "failed", "retryable"])
def test_existing_v1_ledger_migration_preserves_all_evidence(tmp_path: Path, status: str) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE publications (idempotency_key TEXT PRIMARY KEY,text TEXT,text_sha256 TEXT,"
            "status TEXT,creation_id TEXT,post_id TEXT,permalink TEXT,started_at TEXT,updated_at TEXT,error TEXT)"
        )
        now = datetime.now(UTC).isoformat()
        conn.execute("INSERT INTO publications VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "post:12345678", "hello", "legacy-digest", status, "c1", "p1",
            "https://example.test/p1", now, now, "old error",
        ))
    migrated = PublicationLedger(path).get("post:12345678")
    assert migrated.status == status
    assert migrated.creation_id == "c1" and migrated.post_id == "p1"
    assert migrated.error == "old error" and migrated.text_sha256 == "legacy-digest"
    assert migrated.owner == "" and migrated.account_id == ""


def test_simultaneous_initialization_and_claim_returns_one_owner(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "ledger.sqlite3"

    def initialize(_):
        ledger = PublicationLedger(path)
        ledger.reserve("post:12345678", "hello")
        return ledger.claim("post:12345678")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(initialize, range(8)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert PublicationLedger(path).get("post:12345678").owner == winners[0].owner
