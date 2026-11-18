"""Allowlisted provider diagnostics.

Raw provider bodies, messages, URLs, and headers are never copied. Only a fixed
set of fields is kept, and only after a type and shape check. A value that does
not match is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}$")
_TRACE_RE = re.compile(r"^[A-Za-z0-9]{8,48}$")
_HTTP_TOKEN_RE = re.compile(r"[1-5][0-9]{2}")
_CREDENTIAL_WORD = re.compile(r"(?i)token|secret|bearer|password|cookie|credential|authorization")
_SUFFIX_ORDER = ("http_status", "code", "subcode", "type", "is_transient", "trace_id")

PUBLISH_REJECTION = "Threads rejected publication; provider details [redacted]."
CONTAINER_REJECTION = "Threads container creation was rejected; provider details [redacted]."
FAILED_REPLAY = "A previous definitive Threads attempt failed; provider details [redacted]."
_STORED_PREFIXES = (PUBLISH_REJECTION, CONTAINER_REJECTION)
_MAX_STORED_LENGTH = 400


@dataclass(frozen=True)
class ProviderDiagnostic:
    http_status: int | None = None
    code: int | None = None
    subcode: int | None = None
    error_type: str | None = None
    is_transient: bool | None = None
    trace_id: str | None = None


def parse_provider_diagnostic(payload: object, *, http_status: object, secret: str) -> ProviderDiagnostic:
    """Build a diagnostic from an HTTP status and a Graph error object."""
    error = _error_dict(payload)
    return ProviderDiagnostic(
        http_status=_http_status(http_status),
        code=_bounded_int(None if error is None else error.get("code"), 999_999),
        subcode=_bounded_int(None if error is None else error.get("error_subcode"), 999_999_999),
        error_type=None if error is None else _safe_type(error.get("type"), secret),
        is_transient=None if error is None else _strict_bool(error.get("is_transient")),
        trace_id=None if error is None else _safe_trace(error.get("fbtrace_id"), secret),
    )


def diagnostic_from_status(status_code: object) -> ProviderDiagnostic | None:
    status = _http_status(status_code)
    if status is None:
        return None
    return ProviderDiagnostic(http_status=status)


def format_diagnostic(diagnostic: ProviderDiagnostic | None) -> str:
    """Render only values that still satisfy the allowlist."""
    if diagnostic is None:
        return ""
    parts: list[str] = []
    status = _http_status(diagnostic.http_status)
    if status is not None:
        parts.append(f"http_status={status}")
    code = _bounded_int(diagnostic.code, 999_999)
    if code is not None:
        parts.append(f"code={code}")
    subcode = _bounded_int(diagnostic.subcode, 999_999_999)
    if subcode is not None:
        parts.append(f"subcode={subcode}")
    error_type = _safe_type(diagnostic.error_type, "")
    if error_type is not None:
        parts.append(f"type={error_type}")
    if type(diagnostic.is_transient) is bool:
        parts.append(f"is_transient={'true' if diagnostic.is_transient else 'false'}")
    trace_id = _safe_trace(diagnostic.trace_id, "")
    if trace_id is not None:
        parts.append(f"trace_id={trace_id}")
    return " ".join(parts)


def append_diagnostic(message: str, diagnostic: ProviderDiagnostic | None) -> str:
    detail = format_diagnostic(diagnostic)
    if not detail:
        return message
    return f"{message} {detail}"


def strip_secret(message: str, secret: str) -> str:
    """Remove a known bearer value if it survived formatting. Does not apply labeled redaction."""
    if not message or not secret or len(secret) < 8:
        return message
    variants = {secret, quote(secret, safe=""), quote_plus(secret, safe="")}
    redacted = message
    for variant in sorted(variants, key=len, reverse=True):
        if len(variant) < 8:
            continue
        redacted = re.sub(re.escape(variant), "[redacted]", redacted, flags=re.IGNORECASE)
    return redacted


def safe_stored_failure_message(stored: object) -> str | None:
    """Return a previously stored failure only when it is exactly our canonical diagnostic."""
    if not isinstance(stored, str) or not stored or len(stored) > _MAX_STORED_LENGTH:
        return None
    for prefix in _STORED_PREFIXES:
        marker = prefix + " "
        if not stored.startswith(marker):
            continue
        if parse_formatted_diagnostic(stored[len(marker) :]) is None:
            return None
        return stored
    return None


def parse_formatted_diagnostic(suffix: str) -> ProviderDiagnostic | None:
    if not suffix or len(suffix) > 300 or any(char.isspace() and char != " " for char in suffix):
        return None
    parts = suffix.split(" ")
    if any(not part or "=" not in part for part in parts):
        return None
    seen: dict[str, str] = {}
    last = -1
    for part in parts:
        key, _, value = part.partition("=")
        if key not in _SUFFIX_ORDER or not value or key in seen:
            return None
        position = _SUFFIX_ORDER.index(key)
        if position <= last:
            return None
        seen[key] = value
        last = position
    is_transient: bool | None
    if "is_transient" not in seen:
        is_transient = None
    elif seen["is_transient"] == "true":
        is_transient = True
    elif seen["is_transient"] == "false":
        is_transient = False
    else:
        return None
    diagnostic = ProviderDiagnostic(
        http_status=_token_http(seen["http_status"]) if "http_status" in seen else None,
        code=_token_int(seen["code"], 6, 999_999) if "code" in seen else None,
        subcode=_token_int(seen["subcode"], 9, 999_999_999) if "subcode" in seen else None,
        error_type=seen.get("type"),
        is_transient=is_transient,
        trace_id=seen.get("trace_id"),
    )
    if format_diagnostic(diagnostic) != suffix:
        return None
    return diagnostic


def _error_dict(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    return error


def _http_status(value: object) -> int | None:
    if type(value) is not int or not 100 <= value <= 599:
        return None
    return value


def _bounded_int(value: object, upper: int) -> int | None:
    if type(value) is not int or not 0 <= value <= upper:
        return None
    return value


def _strict_bool(value: object) -> bool | None:
    if type(value) is bool:
        return value
    return None


def _overlaps_secret(value: str, secret: str) -> bool:
    if not secret:
        return False
    folded_value = value.casefold()
    folded_secret = secret.casefold()
    if folded_value == folded_secret:
        return True
    if len(secret) >= 8 and folded_secret in folded_value:
        return True
    return len(value) >= 8 and folded_value in folded_secret


def _safe_type(value: object, secret: str) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 32 or not _TYPE_RE.fullmatch(value):
        return None
    if _CREDENTIAL_WORD.search(value) or _overlaps_secret(value, secret):
        return None
    return value


def _safe_trace(value: object, secret: str) -> str | None:
    if not isinstance(value, str) or not 8 <= len(value) <= 48 or not _TRACE_RE.fullmatch(value):
        return None
    if _overlaps_secret(value, secret):
        return None
    return value


def _token_http(value: str) -> int | None:
    if not _HTTP_TOKEN_RE.fullmatch(value):
        return None
    return _http_status(int(value))


def _token_int(value: str, width: int, upper: int) -> int | None:
    if not re.fullmatch(rf"[0-9]{{1,{width}}}", value):
        return None
    number = int(value)
    if str(number) != value:
        return None
    return _bounded_int(number, upper)
