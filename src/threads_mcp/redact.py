"""Secret redaction for error messages surfaced to the model, logs, or ledger.

Provider error bodies are omitted entirely. Allowlisted diagnostics are
extracted separately and do not pass through this sanitizer, because labeled
`code=` redaction would erase a Graph error code. Known credential echoes in
successful public data are redacted, and HTTP library logs are suppressed
during credential-bearing requests.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from urllib.parse import quote, quote_plus

import httpx

from .errors import ProviderRejectedError

REDACTED = "[redacted]"

# Labeled credential patterns. Values are replaced but the label is kept so
# the message stays diagnosable. Only applied to error strings, never to
# request construction.
_PATTERNS = (
    re.compile(r"(?i)(access_token\s*[:=]\s*)([^\s&;\"'\),\]]+)"),
    re.compile(r"(?i)(client_secret\s*[:=]\s*)([^\s&;\"'\),\]]+)"),
    re.compile(r"(?i)(authorization_code\s*[:=]\s*)([^\s&;\"'\),\]]+)"),
    re.compile(r"(?i)(\bcode\s*=\s*)([^\s&;\"'\),\]]+)"),
    re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+\-/]+)"),
)


def sanitize(message: str, *secrets: str | None) -> str:
    """Return *message* with every known secret and labeled credential masked."""
    safe = message
    variants = set()
    for secret in secrets:
        if not secret:
            continue
        text = str(secret)
        if not text:
            continue
        variants.update((text, quote(text, safe=""), quote_plus(text, safe="")))
    for variant in sorted(variants, key=len, reverse=True):
        safe = safe.replace(variant, REDACTED)
        if "%" in variant:
            # Hex escapes are case insensitive; the rest of the token is not.
            pattern = re.sub(r"%[0-9A-Fa-f]{2}", lambda match: f"(?i:{match.group()})", re.escape(variant))
            safe = re.sub(pattern, lambda _: REDACTED, safe)
    for pattern in _PATTERNS:
        safe = pattern.sub(lambda match: match.group(1) + REDACTED, safe)
    return safe


_private_request = ContextVar("threads_private_request", default=False)


class _PrivateHTTPFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX logs full OAuth URLs at INFO; HTTPCore can log echoed response
        # headers at DEBUG. Suppress library records only during our own requests.
        return not _private_request.get()


for _logger_name in (
    "httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
):
    logging.getLogger(_logger_name).addFilter(_PrivateHTTPFilter())


@contextmanager
def private_http_logs():
    token = _private_request.set(True)
    try:
        yield
    finally:
        _private_request.reset(token)


def private_http(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        with private_http_logs():
            try:
                return func(*args, **kwargs)
            except httpx.HTTPError:
                raise ProviderRejectedError("Threads OAuth transport failed; credential details [redacted].") from None
    return wrapped


def sanitize_payload(value, *secrets: str):
    if isinstance(value, str):
        return sanitize(value, *secrets)
    if isinstance(value, dict):
        return {sanitize(key, *secrets): sanitize_payload(item, *secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, *secrets) for item in value]
    return value
