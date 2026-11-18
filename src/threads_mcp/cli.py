"""One-time local OAuth setup commands. Secrets are never exposed as MCP tools."""

from __future__ import annotations

import argparse
import os
import sys

from cryptography.fernet import Fernet

from .config import Settings
from .errors import ThreadsMCPError
from .oauth import OAuthStateStore, build_authorization_url, exchange_code
from .token_store import EncryptedTokenStore


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _cmd_keygen(_: argparse.Namespace) -> int:
    print(Fernet.generate_key().decode("ascii"))
    return 0


def _cmd_auth_url(_: argparse.Namespace) -> int:
    settings = Settings.from_env(require_encryption_key=False)
    app_id = _required_env("THREADS_APP_ID")
    redirect_uri = _required_env("THREADS_REDIRECT_URI")
    state_store = OAuthStateStore(settings.oauth_state_path)
    state = state_store.begin()
    print(build_authorization_url(app_id, redirect_uri, state))
    return 0


def _cmd_exchange(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    app_id = _required_env("THREADS_APP_ID")
    app_secret = _required_env("THREADS_APP_SECRET")
    redirect_uri = _required_env("THREADS_REDIRECT_URI")
    OAuthStateStore(settings.oauth_state_path).consume(args.state)
    token = exchange_code(
        app_id=app_id,
        app_secret=app_secret,
        redirect_uri=redirect_uri,
        code=args.code,
        api_base=settings.api_base,
        timeout_seconds=settings.request_timeout_seconds,
    )
    EncryptedTokenStore(settings.token_store_path, settings.encryption_key).save(token)
    print(f"Threads account connected: @{token.username or 'unknown'}")
    print(f"Encrypted token store: {settings.token_store_path}")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    token = EncryptedTokenStore(settings.token_store_path, settings.encryption_key).load()
    print(f"account_id={token.account_id or 'unknown'}")
    print(f"username=@{token.username or 'unknown'}")
    print(f"expires_at={token.expires_at.isoformat() if token.expires_at else 'unknown'}")
    print(f"token_store={settings.token_store_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads-mcp-auth")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen", help="Generate a Fernet key for TOKEN_ENCRYPTION_KEY.").set_defaults(
        func=_cmd_keygen
    )
    sub.add_parser("auth-url", help="Create a state-bound Threads OAuth authorization URL.").set_defaults(
        func=_cmd_auth_url
    )
    exchange = sub.add_parser(
        "exchange", help="Validate OAuth state, exchange the code, and store the token encrypted."
    )
    exchange.add_argument("--code", required=True)
    exchange.add_argument("--state", required=True)
    exchange.set_defaults(func=_cmd_exchange)
    sub.add_parser("status", help="Show non-secret connection metadata.").set_defaults(func=_cmd_status)
    return parser


def main() -> None:
    try:
        code = build_parser().parse_args()
        raise SystemExit(code.func(code))
    except ThreadsMCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("error: Threads OAuth setup encountered a local failure; credential details [redacted].", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
