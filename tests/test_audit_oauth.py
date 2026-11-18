from __future__ import annotations

import json
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from threads_mcp.errors import AuthRequiredError, ProviderRejectedError
from threads_mcp.models import TokenRecord
from threads_mcp.oauth import OAuthStateStore, _expires_at_from, exchange_code, refresh_long_lived_token
from threads_mcp.token_store import EncryptedTokenStore

SECRET = "dummy app/secret+123="
CODE = "dummy code/+123="
SHORT = "dummy short/+123="
LONG = "dummy long/+123="


def patch_http(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


def exchange():
    return exchange_code(
        app_id="dummy-app", app_secret=SECRET, redirect_uri="https://example.test/callback", code=CODE,
        api_base="https://graph.threads.net/v1.0", timeout_seconds=2,
    )


def assert_no_credentials(text):
    assert all(secret not in text for secret in (SECRET, CODE, SHORT, LONG))


@pytest.mark.parametrize("failure_step", [0, 1, 2])
@pytest.mark.parametrize("kind", ["provider", "timeout", "invalid_json", "wrong_shape", "redirect"])
def test_every_oauth_phase_has_safe_errors_and_safe_http_logs(monkeypatch, caplog, failure_step, kind):
    requests = []

    def handler(request):
        step = len(requests)
        requests.append(request)
        assert request.url.host == "graph.threads.net"
        logging.getLogger("httpcore.http11").debug("raw headers %s %s", SECRET, LONG)
        if step == failure_step:
            if kind == "timeout":
                raise httpx.ReadTimeout(f"{request.url} {SECRET} {CODE} {SHORT} {LONG}", request=request)
            if kind == "invalid_json":
                return httpx.Response(200, text=f"{SECRET} {CODE} {SHORT} {LONG}")
            if kind == "wrong_shape":
                return httpx.Response(200, json=[SECRET, CODE, SHORT, LONG])
            if kind == "redirect":
                return httpx.Response(302, headers={"Location": "https://evil.test/"})
            return httpx.Response(400, json={"error": {
                "message": f"{SECRET} {CODE} {SHORT} {LONG}", "code": LONG, "type": SECRET,
            }})
        if step == 0:
            return httpx.Response(200, json={"access_token": SHORT})
        if step == 1:
            return httpx.Response(200, json={"access_token": LONG, "expires_in": 5184000})
        return httpx.Response(200, json={"id": "u1", "username": "dummy_user"})

    patch_http(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ProviderRejectedError) as caught:
        exchange()
    assert len(requests) == failure_step + 1
    assert_no_credentials(str(caught.value))
    assert_no_credentials("".join(traceback.format_exception(caught.value)))
    assert_no_credentials(caplog.text)


def test_successful_exchange_uses_correct_grants_and_encrypted_storage(monkeypatch, tmp_path, caplog):
    def handler(request):
        if request.url.path == "/oauth/access_token":
            assert request.method == "POST"
            assert b"grant_type=authorization_code" in request.content
            return httpx.Response(200, json={"access_token": SHORT})
        if request.url.path == "/access_token":
            assert request.method == "GET"
            assert request.url.params["grant_type"] == "th_exchange_token"
            assert request.url.params["client_secret"] == SECRET
            assert request.url.params["access_token"] == SHORT
            return httpx.Response(200, json={"access_token": LONG, "expires_in": 5184000})
        assert request.url.path == "/v1.0/me"
        assert request.url.params["access_token"] == LONG
        return httpx.Response(200, json={"id": "u1", "username": "dummy_user"})

    patch_http(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    token = exchange()
    assert token.access_token == LONG
    assert 59 < (token.expires_at - datetime.now(UTC)).total_seconds() / 86400 <= 60
    path = tmp_path / "token.enc"
    store = EncryptedTokenStore(path, Fernet.generate_key().decode())
    store.save(token)
    assert_no_credentials(path.read_bytes().decode())
    assert store.load() == token
    assert_no_credentials(caplog.text)
    assert LONG not in repr(token)


def test_refresh_updates_expiry_preserves_identity_and_hides_http_logs(monkeypatch, caplog):
    def handler(request):
        assert request.url.path == "/refresh_access_token"
        assert request.url.params["grant_type"] == "th_refresh_token"
        assert request.url.params["access_token"] == LONG
        return httpx.Response(200, json={"access_token": "dummy-refreshed-token", "expires_in": 5184000})

    patch_http(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    old = TokenRecord(access_token=LONG, account_id="u1", username="dummy_user",
                      expires_at=datetime.now(UTC) + timedelta(days=2))
    updated = refresh_long_lived_token(old, timeout_seconds=2)
    assert updated.access_token == "dummy-refreshed-token"
    assert updated.account_id == old.account_id and updated.username == old.username
    assert updated.expires_at > old.expires_at
    assert_no_credentials(caplog.text)
    assert "dummy-refreshed-token" not in caplog.text


@pytest.mark.parametrize("lifetime", [None, "", "soon", "raw-provider-credential", 0, -1, 10**100, True, [], {}, 1.5])
def test_bad_oauth_lifetime_fails_closed_without_reflecting_input(lifetime):
    with pytest.raises(ProviderRejectedError) as caught:
        _expires_at_from({"expires_in": lifetime})
    assert "raw-provider-credential" not in str(caught.value)


def test_concurrent_oauth_consumers_have_exactly_one_winner(tmp_path):
    path = tmp_path / "oauth-state.json"
    state = OAuthStateStore(path).begin()
    barrier = threading.Barrier(8)

    def consume(_):
        barrier.wait(timeout=10)
        try:
            OAuthStateStore(path).consume(state)
            return True
        except AuthRequiredError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(8))) == 1
    assert not path.exists()


@pytest.mark.parametrize("state", ["", "invalid", "\u2603"])
def test_invalid_oauth_state_does_not_consume_legitimate_state(tmp_path, state):
    store = OAuthStateStore(tmp_path / "oauth-state.json")
    valid = store.begin()
    with pytest.raises(AuthRequiredError):
        store.consume(state)
    store.consume(valid)


def test_malformed_decrypted_token_has_no_raw_pydantic_traceback(tmp_path):
    key = Fernet.generate_key()
    path = tmp_path / "token.enc"
    path.write_bytes(Fernet(key).encrypt(json.dumps({
        "access_token": LONG, "expires_at": LONG, "account_id": [SECRET],
    }).encode()))
    with pytest.raises(AuthRequiredError) as caught:
        EncryptedTokenStore(path, key.decode()).load()
    assert_no_credentials("".join(traceback.format_exception(caught.value)))


def test_naive_token_expiry_is_rejected_without_credentials(tmp_path):
    key = Fernet.generate_key()
    path = tmp_path / "token.enc"
    path.write_bytes(Fernet(key).encrypt(json.dumps({"access_token": LONG, "expires_at": "2030-01-01"}).encode()))
    with pytest.raises(AuthRequiredError) as caught:
        EncryptedTokenStore(path, key.decode()).load()
    assert_no_credentials(str(caught.value))


def test_oauth_state_rejects_unbounded_expiry(tmp_path):
    path = tmp_path / "oauth-state.json"
    store = OAuthStateStore(path)
    state = store.begin()
    payload = json.loads(path.read_text())
    payload["expires_at"] = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    path.write_text(json.dumps(payload))
    with pytest.raises(AuthRequiredError):
        store.consume(state)
