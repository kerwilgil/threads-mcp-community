"""Encrypted local credential storage."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .errors import AuthRequiredError, ConfigurationError
from .models import TokenRecord


class EncryptedTokenStore:
    def __init__(self, path: Path, encryption_key: str) -> None:
        if not encryption_key:
            raise ConfigurationError("TOKEN_ENCRYPTION_KEY is required.")
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (TypeError, ValueError):
            raise ConfigurationError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key.") from None
        self.path = path

    def load(self) -> TokenRecord:
        try:
            encrypted = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise AuthRequiredError(
                "No Threads token is configured. Complete OAuth with `threads-mcp-auth`."
            ) from exc
        try:
            payload = self._fernet.decrypt(encrypted)
            return TokenRecord.model_validate_json(payload)
        except (InvalidToken, ValueError):
            raise AuthRequiredError("The stored Threads credential cannot be decrypted or parsed.") from None

    def save(self, token: TokenRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)

        encrypted = self._fernet.encrypt(token.model_dump_json().encode("utf-8"))
        fd, temp_name = tempfile.mkstemp(prefix="token-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
