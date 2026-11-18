"""Two installations of threads-mcp (different encryption keys and local paths) share nothing."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from threads_mcp.config import DEFAULT_API_BASE, Settings
from threads_mcp.errors import AuthRequiredError
from threads_mcp.ledger import PublicationLedger
from threads_mcp.models import TokenRecord
from threads_mcp.token_store import EncryptedTokenStore


def _settings(root: Path, encryption_key: str) -> Settings:
    return Settings(
        api_base=DEFAULT_API_BASE,
        token_store_path=root / "token.enc",
        ledger_path=root / "publications.sqlite3",
        oauth_state_path=root / "oauth-state.json",
        encryption_key=encryption_key,
    )


@pytest.fixture
def installs(tmp_path):
    install_a = _settings(tmp_path / "install-a", Fernet.generate_key().decode())
    install_b = _settings(tmp_path / "install-b", Fernet.generate_key().decode())
    return install_a, install_b


def test_token_of_one_installation_cannot_be_read_by_another(installs) -> None:
    install_a, install_b = installs
    store_a = EncryptedTokenStore(install_a.token_store_path, install_a.encryption_key)
    store_a.save(TokenRecord(access_token="synthetic-access-token-a", account_id="a-1", username="example_a"))

    assert b"synthetic-access-token-a" not in install_a.token_store_path.read_bytes()

    install_b.token_store_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(install_a.token_store_path, install_b.token_store_path)
    with pytest.raises(AuthRequiredError):
        EncryptedTokenStore(install_b.token_store_path, install_b.encryption_key).load()


def test_publication_ledgers_are_independent(installs) -> None:
    install_a, install_b = installs
    ledger_a = PublicationLedger(install_a.ledger_path)
    ledger_b = PublicationLedger(install_b.ledger_path)

    ledger_a.reserve("demo-isolation-0001", "Synthetic text for isolation.")

    assert ledger_a.get("demo-isolation-0001") is not None
    assert ledger_b.get("demo-isolation-0001") is None
