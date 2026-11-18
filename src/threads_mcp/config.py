"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigurationError

DEFAULT_API_BASE = "https://graph.threads.net/v1.0"


def validate_api_base(value: str) -> None:
    if not re.fullmatch(r"https://graph\.threads\.net/v[0-9]+\.[0-9]+/?", value):
        raise ConfigurationError("THREADS_API_BASE must be an official HTTPS Threads Graph API version URL.")


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "threads-mcp"


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "threads-mcp"


@dataclass(frozen=True)
class Settings:
    api_base: str
    token_store_path: Path
    ledger_path: Path
    oauth_state_path: Path
    encryption_key: str = field(repr=False)
    request_timeout_seconds: float = 15.0
    refresh_window_days: int = 3
    verification_limit: int = 25
    max_text_chars: int = 500

    @classmethod
    def from_env(cls, *, require_encryption_key: bool = True) -> Settings:
        encryption_key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
        if require_encryption_key and not encryption_key:
            raise ConfigurationError(
                "TOKEN_ENCRYPTION_KEY is required. Generate one locally with `threads-mcp-auth keygen`."
            )

        api_base = os.environ.get("THREADS_API_BASE", DEFAULT_API_BASE).rstrip("/")
        validate_api_base(api_base)
        token_store_path = Path(
            os.environ.get("THREADS_TOKEN_STORE", _config_home() / "token.enc")
        ).expanduser()
        ledger_path = Path(
            os.environ.get("THREADS_LEDGER_PATH", _state_home() / "publications.sqlite3")
        ).expanduser()
        oauth_state_path = Path(
            os.environ.get("THREADS_OAUTH_STATE_PATH", _state_home() / "oauth-state.json")
        ).expanduser()

        try:
            timeout = float(os.environ.get("THREADS_REQUEST_TIMEOUT", "15"))
        except ValueError:
            raise ConfigurationError("THREADS_REQUEST_TIMEOUT must be numeric.") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConfigurationError("THREADS_REQUEST_TIMEOUT must be greater than zero.")

        return cls(
            api_base=api_base,
            token_store_path=token_store_path,
            ledger_path=ledger_path,
            oauth_state_path=oauth_state_path,
            encryption_key=encryption_key,
            request_timeout_seconds=timeout,
        )
