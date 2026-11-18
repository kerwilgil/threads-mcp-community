from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from threads_mcp.errors import AuthRequiredError, ProviderRejectedError
from threads_mcp.models import TokenRecord
from threads_mcp.oauth import (
    OAuthStateStore,
    _raise_oauth,
    build_authorization_url,
    exchange_code,
    refresh_long_lived_token,
)


def test_authorization_url_has_minimal_publish_scopes() -> None:
    url = build_authorization_url("app123", "https://example.test/callback", "state123")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "threads.net"
    assert query["client_id"] == ["app123"]
    assert query["redirect_uri"] == ["https://example.test/callback"]
    assert query["state"] == ["state123"]
    assert set(query["scope"][0].split(",")) == {"threads_basic", "threads_content_publish"}


def test_oauth_state_is_single_use(tmp_path: Path) -> None:
    store = OAuthStateStore(tmp_path / "oauth-state.json")
    state = store.begin()
    store.consume(state)

    with pytest.raises(AuthRequiredError):
        store.consume(state)


def test_oauth_state_rejects_wrong_value_without_consuming_valid_state(tmp_path: Path) -> None:
    store = OAuthStateStore(tmp_path / "oauth-state.json")
    state = store.begin()

    with pytest.raises(AuthRequiredError):
        store.consume("wrong-state")

    store.consume(state)


def test_oauth_state_rejects_expired_state(tmp_path: Path) -> None:
    path = tmp_path / "oauth-state.json"
    store = OAuthStateStore(path)
    state = store.begin()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AuthRequiredError):
        store.consume(state)


def test_oauth_state_rejects_tampered_payloads(tmp_path: Path) -> None:
    path = tmp_path / "oauth-state.json"
    OAuthStateStore(path).begin()

    for tampered in (
        "[1, 2, 3]",
        '{"state": "abc"}',
        json.dumps({"state": "abc", "expires_at": "not-a-date"}),
        json.dumps({"state": "abc", "expires_at": "2030-01-01T00:00:00"}),
    ):
        path.write_text(tampered, encoding="utf-8")
        with pytest.raises(AuthRequiredError):
            OAuthStateStore(path).consume("abc")


def test_oauth_provider_error_redacts_echoed_secrets() -> None:
    secret, code, token = "app-secret-123", "auth-code-456", "short-token-789"
    response = httpx.Response(
        400,
        json={"error": {"message": f"bad {secret} {code} {token}", "code": 100}},
    )
    with pytest.raises(ProviderRejectedError) as exc_info:
        _raise_oauth(response, "Threads rejected it.", secret, code, token)
    message = str(exc_info.value)
    assert secret not in message
    assert code not in message
    assert token not in message
    assert "[redacted]" in message


class _OAuthResponse:
    def __init__(self, status_code: int, payload: object = None, broken: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._broken = broken

    def json(self) -> object:
        if self._broken:
            raise ValueError("unreadable")
        assert self._payload is not None
        return self._payload


class _OAuthHttpClient:
    """Fake httpx.Client double for the OAuth setup flow (never touches the network)."""

    posted: dict
    responses: list[_OAuthResponse]

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.posted = {}
        self.responses = list(type(self).responses)

    def __enter__(self) -> _OAuthHttpClient:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def post(self, url: str, data: dict | None = None) -> _OAuthResponse:
        self.posted = {"url": url, "data": dict(data or {})}
        return self.responses.pop(0)

    def get(self, url: str, params: dict | None = None) -> _OAuthResponse:
        self.posted.setdefault("gets", []).append({"url": url, "params": dict(params or {})})
        return self.responses.pop(0)


def _patch_oauth_http(monkeypatch: pytest.MonkeyPatch, responses: list[_OAuthResponse]) -> type[_OAuthHttpClient]:
    _OAuthHttpClient.responses = list(responses)
    monkeypatch.setattr(httpx, "Client", _OAuthHttpClient)
    return _OAuthHttpClient


def test_exchange_code_strips_redirect_fragment_and_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class Client(_OAuthHttpClient):
        def post(self, url: str, data: dict | None = None) -> _OAuthResponse:
            captured.update(data or {})
            return super().post(url, data)

    Client.responses = [
        _OAuthResponse(200, {"access_token": "short-token"}),
        _OAuthResponse(200, {"access_token": "long-token", "expires_in": 5183944}),
        _OAuthResponse(200, {"id": "u1", "username": "example_user"}),
    ]
    monkeypatch.setattr(httpx, "Client", Client)

    token = exchange_code(
        app_id="app123",
        app_secret="app-secret",
        redirect_uri="https://example.test/callback",
        code="auth-code-456#_",
        api_base="https://graph.threads.net/v1.0",
        timeout_seconds=5.0,
    )
    assert captured["code"] == "auth-code-456"
    assert token.access_token == "long-token"
    assert token.account_id == "u1"
    assert token.username == "example_user"
    assert token.expires_at is not None


def test_exchange_code_error_redacts_echoed_code_and_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_oauth_http(
        monkeypatch,
        [_OAuthResponse(400, {"error": {"message": "bad auth-code-456 with app-secret-xyz"}})],
    )
    with pytest.raises(ProviderRejectedError) as exc_info:
        exchange_code(
            app_id="app123",
            app_secret="app-secret-xyz",
            redirect_uri="https://example.test/callback",
            code="auth-code-456",
            api_base="https://graph.threads.net/v1.0",
            timeout_seconds=5.0,
        )
    message = str(exc_info.value)
    assert "auth-code-456" not in message
    assert "app-secret-xyz" not in message
    assert "[redacted]" in message


def test_refresh_error_redacts_echoed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_oauth_http(
        monkeypatch,
        [_OAuthResponse(400, {"error": {"message": "token old-token-abc expired"}})],
    )
    token = TokenRecord(access_token="old-token-abc", account_id="u1", username="example_user")
    with pytest.raises(ProviderRejectedError) as exc_info:
        refresh_long_lived_token(token, timeout_seconds=5.0)
    assert "old-token-abc" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


def test_refresh_rejects_malformed_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_oauth_http(
        monkeypatch,
        [_OAuthResponse(200, {"access_token": "new-token", "expires_in": "soon"})],
    )
    token = TokenRecord(access_token="old-token", account_id="u1", username="example_user")
    with pytest.raises(ProviderRejectedError, match="invalid OAuth token lifetime"):
        refresh_long_lived_token(token, timeout_seconds=5.0)
