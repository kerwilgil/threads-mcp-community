from __future__ import annotations

from threads_mcp.redact import sanitize


def test_sanitize_replaces_exact_secrets() -> None:
    message = sanitize("bad code CODE-123 and secret s3cr3t!", "CODE-123", "s3cr3t!")
    assert "CODE-123" not in message
    assert "s3cr3t!" not in message
    assert message.count("[redacted]") == 2


def test_sanitize_replaces_url_encoded_secrets() -> None:
    secret = "a/b+c=d"
    message = sanitize("see https://example.test/?code=a%2Fb%2Bc%3Dd end", secret)
    assert secret not in message
    assert "a%2Fb%2Bc%3Dd" not in message
    assert "[redacted]" in message


def test_sanitize_masks_labeled_credentials_without_known_secrets() -> None:
    message = sanitize("token rejected: access_token=THQVJ9XYZ client_secret=eb8c7 code=AQBx12")
    assert "THQVJ9XYZ" not in message
    assert "eb8c7" not in message
    assert "AQBx12" not in message
    assert message.count("[redacted]") == 3


def test_sanitize_masks_bearer_tokens() -> None:
    message = sanitize("upstream said Bearer THQVJ9XYZ is invalid")
    assert "THQVJ9XYZ" not in message
    assert "[redacted]" in message


def test_sanitize_leaves_clean_messages_untouched() -> None:
    message = "Threads rejected the request with HTTP 400."
    assert sanitize(message) == message


def test_sanitize_ignores_empty_secrets() -> None:
    assert sanitize("nothing secret here", "", None) == "nothing secret here"


def test_longer_secrets_are_redacted_before_overlapping_prefixes() -> None:
    assert sanitize("dummy-token-with-suffix", "dummy-token", "dummy-token-with-suffix") == "[redacted]"


def test_url_encoding_hex_case_and_form_encoding_are_redacted() -> None:
    secret = "dummy /+="
    assert sanitize("dummy%20%2f%2b%3d", secret) == "[redacted]"
    assert sanitize("dummy+%2F%2B%3D", secret) == "[redacted]"
