from __future__ import annotations

import httpx
import pytest

from threads_mcp.client import ThreadsClient
from threads_mcp.errors import ProviderRejectedError, SafeTransportError, UnknownOutcomeError


def client_for(handler):
    return ThreadsClient(
        "top-secret-token",
        api_base="https://graph.threads.net/v1.0",
        transport=httpx.MockTransport(handler),
    )


def test_get_profile_parses_expected_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/me")
        return httpx.Response(
            200,
            json={
                "id": "u1",
                "username": "example_user",
                "name": "Example User",
                "threads_profile_picture_url": "https://example.test/p.jpg",
                "threads_biography": "bio",
            },
        )

    with client_for(handler) as client:
        profile = client.get_profile()
    assert profile.id == "u1"
    assert profile.username == "example_user"


def test_publish_connect_failure_is_safe_to_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no connection", request=request)

    with client_for(handler) as client, pytest.raises(SafeTransportError):
        client.publish_container("u1", "c1")


def test_publish_read_timeout_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with client_for(handler) as client, pytest.raises(UnknownOutcomeError):
        client.publish_container("u1", "c1")


def test_publish_500_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "server error"}})

    with client_for(handler) as client, pytest.raises(UnknownOutcomeError):
        client.publish_container("u1", "c1")


def test_publish_400_is_definitive_rejection_without_token_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "bad token top-secret-token", "code": 190}},
        )

    with client_for(handler) as client, pytest.raises(ProviderRejectedError) as exc_info:
        client.publish_container("u1", "c1")
    rendered = str(exc_info.value)
    assert "top-secret-token" not in rendered
    assert "[redacted]" in rendered
    assert "http_status=400" in rendered
    assert "code=190" in rendered


def test_publish_connect_timeout_is_safe_to_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no connection", request=request)

    with client_for(handler) as client, pytest.raises(SafeTransportError):
        client.publish_container("u1", "c1")


def test_publish_write_timeout_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("timed out", request=request)

    with client_for(handler) as client, pytest.raises(UnknownOutcomeError):
        client.publish_container("u1", "c1")


def test_read_timeout_without_publish_flag_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with client_for(handler) as client, pytest.raises(SafeTransportError):
        client.get_profile()


def test_publish_429_is_unknown_without_token_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "limited token top-secret-token", "code": 4}},
        )

    with client_for(handler) as client, pytest.raises(UnknownOutcomeError) as exc_info:
        client.publish_container("u1", "c1")
    assert "top-secret-token" not in str(exc_info.value)
    assert "429" in str(exc_info.value)


def test_container_creation_500_is_a_rejection_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "server error"}})

    with client_for(handler) as client, pytest.raises(ProviderRejectedError) as exc_info:
        client.create_text_container("u1", "hello")
    assert exc_info.value.status_code == 500


def test_container_creation_400_is_definitive_without_token_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "invalid container top-secret-token", "code": 100}},
        )

    with client_for(handler) as client, pytest.raises(ProviderRejectedError) as exc_info:
        client.create_text_container("u1", "hello")
    rendered = str(exc_info.value)
    assert exc_info.value.status_code == 400
    assert "top-secret-token" not in rendered
    assert "[redacted]" in rendered
    assert "http_status=400" in rendered
    assert "code=100" in rendered
