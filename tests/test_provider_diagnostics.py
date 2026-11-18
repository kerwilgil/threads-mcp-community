"""Offline diagnostics, terminal-failure, and text-publish sequence tests."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
from test_service import FakeClient, MemoryTokenStore, service, settings

from threads_mcp.client import ThreadsClient
from threads_mcp.diagnostics import (
    FAILED_REPLAY,
    PUBLISH_REJECTION,
    format_diagnostic,
    parse_provider_diagnostic,
    safe_stored_failure_message,
)
from threads_mcp.errors import ProviderRejectedError, UnknownOutcomeError
from threads_mcp.ledger import PublicationLedger
from threads_mcp.models import TokenRecord
from threads_mcp.service import ThreadsService

TOKEN = "top-secret-token-value/a+b="
ALPHANUMERIC_TOKEN = "SYNTHETICVALUE0123456789ABCDEFG"
TRACE = "Abcd1234Efgh5678"
KEY = "post:363chars"
TEXT = "n" * 363


def test_parser_keeps_only_shape_checked_fields() -> None:
    diagnostic = parse_provider_diagnostic(
        {
            "access_token": ALPHANUMERIC_TOKEN,
            "error": {
                "message": f"https://graph.threads.net/v1.0?access_token={ALPHANUMERIC_TOKEN}",
                "error_user_msg": ALPHANUMERIC_TOKEN,
                "error_user_title": "Bearer " + ALPHANUMERIC_TOKEN,
                "error_data": {"access_token": ALPHANUMERIC_TOKEN},
                "type": "THApiException",
                "code": 9007,
                "error_subcode": 2207027,
                "is_transient": False,
                "fbtrace_id": TRACE,
                "http_status": 200,
            },
        },
        http_status=400,
        secret=ALPHANUMERIC_TOKEN,
    )
    rendered = format_diagnostic(diagnostic)
    assert rendered == (
        "http_status=400 code=9007 subcode=2207027 type=THApiException is_transient=false "
        f"trace_id={TRACE}"
    )
    assert ALPHANUMERIC_TOKEN not in rendered
    assert "graph.threads.net" not in rendered
    assert "access_token" not in rendered


@pytest.mark.parametrize(
    ("error", "secret"),
    [
        ({"type": "AccessToken", "code": 190}, "unused-token-value"),
        ({"type": "raw-app-secret", "code": 190}, "unused-token-value"),
        ({"type": ALPHANUMERIC_TOKEN, "code": 190}, ALPHANUMERIC_TOKEN),
        ({"code": 190, "fbtrace_id": ALPHANUMERIC_TOKEN[:16]}, ALPHANUMERIC_TOKEN),
        ({"code": 190, "fbtrace_id": f"https://evil.test/{ALPHANUMERIC_TOKEN}"}, ALPHANUMERIC_TOKEN),
        ({"code": "190", "error_subcode": "2207027", "is_transient": "false"}, "unused-token-value"),
        ({"code": True, "error_subcode": 1.5, "is_transient": 0}, "unused-token-value"),
        ({"code": 10**20, "error_subcode": -1}, "unused-token-value"),
        ({"code": 190, "fbtrace_id": "short"}, "unused-token-value"),
    ],
)
def test_unsafe_or_mistyped_fields_are_dropped(error: dict, secret: str) -> None:
    rendered = format_diagnostic(parse_provider_diagnostic({"error": error}, http_status=400, secret=secret))
    assert secret not in rendered
    assert "Bearer" not in rendered
    assert "http" not in rendered.split("http_status=", 1)[-1] or rendered.startswith("http_status=400")
    assert "AccessToken" not in rendered
    assert "raw-app-secret" not in rendered
    assert "false" not in rendered or "is_transient=false" in rendered
    if error.get("code") == 190 and not isinstance(error.get("fbtrace_id"), str):
        assert "code=190" in rendered
    if isinstance(error.get("fbtrace_id"), str):
        assert "trace_id=" not in rendered
    if error.get("is_transient") == "false":
        assert "is_transient=" not in rendered


def test_string_false_is_not_coerced_and_missing_transient_is_omitted() -> None:
    omitted = parse_provider_diagnostic({"error": {"code": 24, "type": "OAuthException"}}, http_status=400, secret="")
    assert omitted.is_transient is None
    assert "is_transient=" not in format_diagnostic(omitted)
    assert "type=OAuthException" in format_diagnostic(omitted)
    coerced = parse_provider_diagnostic(
        {"error": {"code": 24, "is_transient": "false"}}, http_status=400, secret=""
    )
    assert coerced.is_transient is None


@pytest.mark.parametrize(
    "stored",
    [
        PUBLISH_REJECTION,
        PUBLISH_REJECTION + f" http_status=400 message={TOKEN}",
        PUBLISH_REJECTION + " code=190 http_status=400",
        f"{TOKEN}",
        PUBLISH_REJECTION + " http_status=007 code=190",
        PUBLISH_REJECTION + " http_status=400 code=190\ntrace_id=Abcd1234Efgh5678",
    ],
)
def test_replay_rejects_noncanonical_stored_text(stored: str) -> None:
    assert safe_stored_failure_message(stored) is None


def test_replay_accepts_only_the_canonical_diagnostic() -> None:
    stored = (
        PUBLISH_REJECTION + " http_status=400 code=9007 subcode=2207027 "
        "type=THApiException is_transient=false trace_id=Abcd1234Efgh5678"
    )
    assert safe_stored_failure_message(stored) == stored


def _threads_service(tmp_path, handler, token: str = TOKEN) -> ThreadsService:
    store = MemoryTokenStore()
    store.token = TokenRecord(access_token=token, account_id="u1", username="example_user")
    return ThreadsService(
        settings(tmp_path),
        token_store=store,  # type: ignore[arg-type]
        ledger=PublicationLedger(tmp_path / "ledger.sqlite3"),
        client_factory=lambda record: ThreadsClient(  # type: ignore[arg-type]
            record.access_token,
            api_base="https://graph.threads.net/v1.0",
            transport=httpx.MockTransport(handler),
        ),
    )


def _assert_no_secret(text: str) -> None:
    for forbidden in (TOKEN, ALPHANUMERIC_TOKEN, "graph.threads.net", "access_token", "Bearer", "threads_publish"):
        assert forbidden not in text


def test_definitive_text_rejection_persists_diagnostic_and_stays_terminal(tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/threads"):
            posted = parse_qs(request.content.decode())
            assert posted["auto_publish_text"] == ["false"]
            assert len(posted["text"][0]) == 363
            return httpx.Response(200, json={"id": "c1"})
        return httpx.Response(
            400,
            headers={"WWW-Authenticate": f"Bearer {TOKEN}", "x-request-url": str(request.url)},
            json={
                "access_token": TOKEN,
                "error": {
                    "message": f"{TOKEN} {request.url}",
                    "error_user_msg": str(request.headers.get("Authorization")),
                    "error_data": {"access_token": TOKEN},
                    "type": "THApiException",
                    "code": 9007,
                    "error_subcode": 2207027,
                    "is_transient": False,
                    "fbtrace_id": TRACE,
                    "http_status": "200",
                },
            },
        )

    svc = _threads_service(tmp_path, handler)
    first = svc.publish_text(TEXT, KEY)
    record = svc.ledger.get(KEY)
    assert record is not None
    assert first.status == "failed"
    assert first.post_id == "" and first.permalink == ""
    assert record.status == "failed"
    assert record.creation_id == "c1" and record.post_id == ""
    assert first.message == record.error
    assert "http_status=400 code=9007 subcode=2207027 type=THApiException is_transient=false" in first.message
    assert f"trace_id={TRACE}" in first.message
    _assert_no_secret(first.model_dump_json())
    _assert_no_secret(record.error)

    second = svc.publish_text(TEXT, KEY)
    assert second.status == "failed"
    assert second.message == first.message
    assert second.post_id == ""
    assert [path for method, path in calls if method == "POST" and path.endswith("/threads_publish")] == [
        "/v1.0/u1/threads_publish"
    ]
    assert not any(path.endswith("/c1") and method == "GET" for method, path in calls)


def test_legacy_failed_row_keeps_terminal_replay_message(tmp_path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)
    svc.ledger.reserve(KEY, "hello")
    claimed = svc.ledger.claim(KEY)
    assert claimed is not None
    svc.ledger.set_status(KEY, "failed", owner=claimed.owner, error=PUBLISH_REJECTION)
    result = svc.publish_text("hello", KEY)
    assert result.status == "failed"
    assert result.message == FAILED_REPLAY
    assert result.post_id == ""
    assert fake.publish_calls == 0


def test_poisoned_ledger_error_is_not_surfaced(tmp_path) -> None:
    fake = FakeClient()
    svc = service(tmp_path, fake)
    svc.ledger.reserve(KEY, "hello")
    claimed = svc.ledger.claim(KEY)
    assert claimed is not None
    svc.ledger.set_status(
        KEY,
        "failed",
        owner=claimed.owner,
        error=PUBLISH_REJECTION + f" http_status=400 message={TOKEN} code=1",
    )
    result = svc.publish_text("hello", KEY)
    assert result.message == FAILED_REPLAY
    assert TOKEN not in result.model_dump_json()
    assert fake.publish_calls == 0


def test_rate_limit_and_server_error_stay_unknown_without_replay(tmp_path) -> None:
    calls = {"publish": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/threads_publish"):
            calls["publish"] += 1
        if request.method == "POST" and request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "c1"})
        return httpx.Response(
            429,
            json={"error": {"message": TOKEN, "code": 4, "is_transient": False, "type": "OAuthException"}},
        )

    svc = _threads_service(tmp_path, handler)
    first = svc.publish_text("hello", "post:rate-limit")
    assert first.status == "unknown"
    assert "http_status=429" in first.message and "code=4" in first.message
    assert TOKEN not in first.model_dump_json()
    second = svc.publish_text("hello", "post:rate-limit")
    assert second.status == "unknown"
    assert second.message == (
        "A previous publish attempt may have reached Threads. No automatic retry was performed "
        "because that could create a duplicate."
    )
    assert svc.ledger.get("post:rate-limit").error == first.message
    assert calls["publish"] == 1


def test_container_server_error_stays_retryable(tmp_path) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"message": TOKEN, "code": 1, "is_transient": False, "type": "OAuthException"}},
        )

    svc = _threads_service(tmp_path, failing)
    first = svc.publish_text("hello", "post:container-500")
    record = svc.ledger.get("post:container-500")
    assert first.status == "failed"  # tool surface uses failed for a retryable pre-publish outcome
    assert record is not None and record.status == "retryable"
    assert "http_status=500" in record.error
    assert TOKEN not in record.error

    def healthy(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "p1"})
        if request.method == "GET":
            return httpx.Response(200, json={"id": "p1", "permalink": "https://www.threads.net/@example_user/post/example"})
        return httpx.Response(200, json={"id": "c2"})

    svc._client_factory = lambda record: ThreadsClient(  # type: ignore[method-assign]
        record.access_token,
        api_base="https://graph.threads.net/v1.0",
        transport=httpx.MockTransport(healthy),
    )
    second = svc.publish_text("hello", "post:container-500")
    assert second.status == "published"
    assert second.post_id == "p1"


def test_example_usertext_publish_does_not_poll_container_status(tmp_path) -> None:
    events: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append((request.method, request.url.path, str(request.url.params.get("fields", ""))))
        if request.method == "POST" and request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "c_ready"})
        if request.method == "POST" and request.url.path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "17900000000000001"})
        return httpx.Response(
            200,
            json={
                "id": "17900000000000001",
                "permalink": "https://www.threads.net/@example_user/post/example",
            },
        )

    svc = _threads_service(tmp_path, handler)
    result = svc.publish_text(TEXT, "post:example-ok")
    assert result.status == "published"
    assert result.post_id == "17900000000000001"
    assert "example_user" in result.permalink
    assert svc.ledger.get("post:example-ok").error == ""
    assert [(method, path) for method, path, _fields in events] == [
        ("POST", "/v1.0/u1/threads"),
        ("POST", "/v1.0/u1/threads_publish"),
        ("GET", "/v1.0/17900000000000001"),
    ]
    assert all("status" not in fields for _method, _path, fields in events)
    assert not any(path.endswith("/c_ready") for _method, path, _fields in events)


def test_publish_without_transient_flag_is_still_terminal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": TOKEN, "code": 10, "type": "OAuthException"}})

    client = ThreadsClient(TOKEN, api_base="https://graph.threads.net/v1.0", transport=httpx.MockTransport(handler))
    with client, pytest.raises(ProviderRejectedError) as caught:
        client.publish_container("u1", "c1")
    assert caught.value.definitive is True
    rendered = str(caught.value)
    assert "http_status=403" in rendered and "code=10" in rendered and "type=OAuthException" in rendered
    assert "is_transient=" not in rendered
    assert TOKEN not in rendered


def test_transient_publish_is_unknown_and_records_the_boolean() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 100, "is_transient": True, "message": TOKEN}})

    client = ThreadsClient(TOKEN, api_base="https://graph.threads.net/v1.0", transport=httpx.MockTransport(handler))
    with client, pytest.raises(UnknownOutcomeError) as caught:
        client.publish_container("u1", "c1")
    rendered = str(caught.value)
    assert "is_transient=true" in rendered and "code=100" in rendered
    assert TOKEN not in rendered
