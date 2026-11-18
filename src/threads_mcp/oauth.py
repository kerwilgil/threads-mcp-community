"""Threads OAuth helpers kept outside the MCP tool surface."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .client import _valid_id
from .config import validate_api_base
from .errors import AuthRequiredError, ConfigurationError, ProviderRejectedError
from .models import TokenRecord
from .redact import private_http, sanitize_payload

AUTH_URL = "https://threads.net/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"
LONG_LIVED_URL = "https://graph.threads.net/access_token"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
DEFAULT_SCOPES = ("threads_basic", "threads_content_publish")
STATE_TTL_MINUTES = 15


def build_authorization_url(app_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(DEFAULT_SCOPES),
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


class OAuthStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _locked(self):
        # SQLite's OS locks serialize separate processes and release on crashes.
        # Keep the JSON format so existing pending authorizations remain valid.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path) + ".lock.sqlite3", timeout=10)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield
            conn.commit()
        finally:
            conn.close()

    def begin(self) -> str:
        with self._locked():
            return self._begin_locked()

    def _begin_locked(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = secrets.token_urlsafe(32)
        payload = {
            "state": state,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=STATE_TTL_MINUTES)).isoformat(),
        }
        fd, temporary = tempfile.mkstemp(prefix="oauth-state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return state

    def consume(self, state: str) -> None:
        with self._locked():
            self._consume_locked(state)

    def _consume_locked(self, state: str) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AuthRequiredError("No pending OAuth authorization was found. Start a new login.") from None
        # A tampered state file must fail closed as an auth error, never with a raw TypeError.
        if not isinstance(payload, dict):
            raise AuthRequiredError("The pending OAuth authorization is invalid. Start a new login.")
        try:
            expires_at = datetime.fromisoformat(payload.get("expires_at"))
        except (TypeError, ValueError, AttributeError):
            raise AuthRequiredError("The pending OAuth authorization is invalid. Start a new login.") from None
        if expires_at.tzinfo is None:
            raise AuthRequiredError("The pending OAuth authorization is invalid. Start a new login.")
        now = datetime.now(UTC)
        if expires_at <= now:
            self.path.unlink(missing_ok=True)
            raise AuthRequiredError("The pending OAuth authorization expired. Start a new login.")
        if expires_at > now + timedelta(minutes=STATE_TTL_MINUTES):
            raise AuthRequiredError("The pending OAuth authorization lifetime is invalid. Start a new login.")
        expected = payload.get("state")
        if not isinstance(expected, str) or not expected or not isinstance(state, str) or not state.isascii():
            raise AuthRequiredError("OAuth state is invalid. Start a new login.")
        if not expected.isascii() or not secrets.compare_digest(expected, state):
            raise AuthRequiredError("OAuth state validation failed.")
        self.path.unlink(missing_ok=True)


@private_http
def exchange_code(
    *,
    app_id: str,
    app_secret: str,
    redirect_uri: str,
    code: str,
    api_base: str,
    timeout_seconds: float,
) -> TokenRecord:
    if not all([app_id, app_secret, redirect_uri, code]):
        raise ConfigurationError("Threads OAuth app id, secret, redirect URI, and code are required.")
    validate_api_base(api_base)
    # Meta appends a `#_` fragment to the redirect URI; it is not part of the code.
    code = code.split("#", 1)[0].strip()
    if not code:
        raise ConfigurationError("Threads OAuth authorization code is required.")
    with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
        short = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": app_id,
                "client_secret": app_secret,
            },
        )
        _raise_oauth(short, "Threads rejected the authorization code.", app_secret, code)
        short_token = _oauth_json(short).get("access_token")
        if not isinstance(short_token, str) or not short_token:
            raise ProviderRejectedError("Threads returned no access token.")

        long_response = client.get(
            LONG_LIVED_URL,
            params={
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": short_token,
            },
        )
        _raise_oauth(
            long_response,
            "Threads rejected the long-lived token exchange.",
            app_secret,
            code,
            short_token,
        )
        long_data = _oauth_json(long_response)
        access_token = long_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderRejectedError("Threads returned no long-lived access token.")
        expires_at = _expires_at_from(long_data)

        profile = client.get(
            f"{api_base.rstrip('/')}/me",
            params={"fields": "id,username", "access_token": access_token},
        )
        _raise_oauth(
            profile,
            "Threads could not verify the connected account.",
            app_secret,
            code,
            short_token,
            access_token,
        )
        profile_data = sanitize_payload(_oauth_json(profile), app_secret, code, short_token, access_token)
        if not _valid_id(profile_data.get("id")):
            raise ProviderRejectedError("Threads could not verify a valid account id.")
        return TokenRecord(
            access_token=access_token,
            expires_at=expires_at,
            account_id=str(profile_data.get("id") or ""),
            username=str(profile_data.get("username") or ""),
        )


@private_http
def refresh_long_lived_token(token: TokenRecord, *, timeout_seconds: float) -> TokenRecord:
    if token.expires_at and token.expires_at <= datetime.now(UTC):
        raise AuthRequiredError("The Threads token expired. Complete a new OAuth login.")
    with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
        response = client.get(
            REFRESH_URL,
            params={
                "grant_type": "th_refresh_token",
                "access_token": token.access_token,
            },
        )
    _raise_oauth(response, "Threads rejected the token refresh.", token.access_token)
    data = _oauth_json(response)
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ProviderRejectedError("Threads refresh returned no access token.")
    expires_at = _expires_at_from(data)
    return token.model_copy(update={"access_token": access_token, "expires_at": expires_at})


def _oauth_json(response: httpx.Response) -> dict:
    """Decode a provider JSON object response without surfacing raw bodies."""
    try:
        payload = response.json()
    except ValueError:
        raise ProviderRejectedError(
            "Threads returned an unreadable OAuth response.",
            status_code=response.status_code,
        ) from None
    if not isinstance(payload, dict) or "error" in payload:
        raise ProviderRejectedError(
            "Threads returned an unexpected OAuth response shape.",
            status_code=response.status_code,
        )
    return payload


def _expires_at_from(data: dict) -> datetime:
    raw = data.get("expires_in")
    if type(raw) not in {int, str}:
        raise ProviderRejectedError("Threads returned an invalid OAuth token lifetime.")
    try:
        expires_in = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise ProviderRejectedError("Threads returned an invalid OAuth token lifetime.") from None
    if not 0 < expires_in <= 366 * 24 * 3600:
        raise ProviderRejectedError("Threads returned an invalid OAuth token lifetime.")
    return datetime.now(UTC) + timedelta(seconds=expires_in)


def _raise_oauth(response: httpx.Response, fallback: str, *secrets: str | None) -> None:
    if 200 <= response.status_code < 300:
        return
    # Raw messages can encode or echo any credential, including fields not known
    # to this request. Surface only our fixed diagnostics and HTTP status.
    raise ProviderRejectedError(f"{fallback} Provider details [redacted].", status_code=response.status_code)
