from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from threads_mcp.errors import AuthRequiredError, ConfigurationError
from threads_mcp.models import TokenRecord
from threads_mcp.token_store import EncryptedTokenStore


def test_token_store_round_trip_is_encrypted(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode("ascii")
    path = tmp_path / "token.enc"
    store = EncryptedTokenStore(path, key)
    store.save(TokenRecord(access_token="super-secret", account_id="u1", username="example_user"))

    raw = path.read_bytes()
    assert b"super-secret" not in raw
    loaded = store.load()
    assert loaded.access_token == "super-secret"
    assert loaded.account_id == "u1"


def test_token_store_wrong_key_is_an_auth_error(tmp_path: Path) -> None:
    path = tmp_path / "token.enc"
    EncryptedTokenStore(path, Fernet.generate_key().decode("ascii")).save(
        TokenRecord(access_token="super-secret", account_id="u1", username="example_user")
    )
    with pytest.raises(AuthRequiredError):
        EncryptedTokenStore(path, Fernet.generate_key().decode("ascii")).load()


def test_token_store_tampered_file_is_an_auth_error(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode("ascii")
    path = tmp_path / "token.enc"
    store = EncryptedTokenStore(path, key)
    store.save(TokenRecord(access_token="super-secret", account_id="u1", username="example_user"))

    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(AuthRequiredError):
        store.load()


def test_token_store_missing_file_is_an_auth_error(tmp_path: Path) -> None:
    store = EncryptedTokenStore(tmp_path / "token.enc", Fernet.generate_key().decode("ascii"))
    with pytest.raises(AuthRequiredError):
        store.load()


def test_token_store_rejects_invalid_key() -> None:
    with pytest.raises(ConfigurationError):
        EncryptedTokenStore(Path("token.enc"), "not-a-fernet-key")
