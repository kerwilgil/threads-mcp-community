from __future__ import annotations

import logging
import traceback
from urllib.parse import parse_qs

import httpx
import pytest

from threads_mcp.client import ThreadsClient
from threads_mcp.errors import ConfigurationError, ProviderRejectedError, SafeTransportError, UnknownOutcomeError


def make_client(handler):
    return ThreadsClient(
        "dummy/a+b=token", api_base="https://graph.threads.net/v1.0", transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("kind", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_proven_unsent_errors_have_no_credential_traceback(kind):
    def handler(request):
        raise kind("dummy/a+b=token echoed by transport", request=request)

    with make_client(handler) as client, pytest.raises(SafeTransportError) as caught:
        client.publish_container("u1", "c1")
    assert "dummy/a+b=token" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("kind", [
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
    httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.CloseError, httpx.DecodingError,
])
def test_possibly_sent_errors_are_unknown(kind):
    def handler(request):
        raise kind("dummy/a+b=token", request=request)

    with make_client(handler) as client, pytest.raises(UnknownOutcomeError) as caught:
        client.publish_container("u1", "c1")
    assert "dummy/a+b=token" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504])
@pytest.mark.parametrize("body", [None, {"error": "bad"}, {"error": {"message": "secret", "code": "secret"}}])
def test_unproven_publish_rejections_stay_unknown(status, body):
    def handler(request):
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="broken")

    with make_client(handler) as client, pytest.raises(UnknownOutcomeError) as caught:
        client.publish_container("u1", "c1")
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_structured_graph_client_rejection_is_definitive_and_drops_all_details(status):
    def handler(request):
        return httpx.Response(status, json={"error": {
            "message": "unrecognized-provider-credential", "code": 190,
            "type": "raw-app-secret", "error_data": {"access_token": "another-secret"},
        }})

    with make_client(handler) as client, pytest.raises(ProviderRejectedError) as caught:
        client.publish_container("u1", "c1")
    rendered = str(caught.value)
    assert caught.value.definitive
    assert caught.value.status_code == status
    assert "secret" not in rendered
    assert "unrecognized-provider-credential" not in rendered
    assert "raw-app-secret" not in rendered
    assert "another-secret" not in rendered
    assert f"http_status={status}" in rendered
    assert "code=190" in rendered


@pytest.mark.parametrize("body", [[], "dummy/a+b=token", {}, {"id": None}, {"id": 123},
                                     {"id": {"token": "dummy/a+b=token"}}, {"id": "../threads_publish"},
                                     {"id": "dummy/a+b=token"}, {"id": "p1", "error": {"code": 100}}])
def test_malformed_publish_success_is_unknown(body):
    with make_client(lambda _: httpx.Response(200, json=body)) as client, pytest.raises(UnknownOutcomeError):
        client.publish_container("u1", "c1")


def test_invalid_json_success_is_unknown():
    with make_client(lambda _: httpx.Response(200, text="dummy/a+b=token")) as client:
        with pytest.raises(UnknownOutcomeError):
            client.publish_container("u1", "c1")


@pytest.mark.parametrize("body", [
    {"error": {"code": 100, "is_transient": True}},
    {"error": {"code": 100, "is_transient": "raw-credential"}},
    {"id": "p1", "error": {"code": 100}},
])
def test_transient_or_contradictory_4xx_is_unknown(body):
    with make_client(lambda _: httpx.Response(400, json=body)) as client, pytest.raises(UnknownOutcomeError) as caught:
        client.publish_container("u1", "c1")
    assert "raw-credential" not in str(caught.value)
    assert "credential" not in str(caught.value)


def test_create_explicitly_disables_auto_publish_and_uses_bearer_header():
    def handler(request):
        assert request.method == "POST"
        assert request.url.query == b""
        assert request.headers["Authorization"] == "Bearer dummy/a+b=token"
        assert parse_qs(request.content.decode()) == {
            "media_type": ["TEXT"], "text": ["approved text"], "auto_publish_text": ["false"],
        }
        return httpx.Response(200, json={"id": "c1"})

    with make_client(handler) as client:
        assert client.create_text_container("u1", "approved text") == "c1"


def test_read_tools_issue_only_get_and_redact_echoed_token(caplog):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        logging.getLogger("httpcore.http11").debug("response headers dummy/a+b=token")
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "u1", "username": "test", "name": "dummy/a+b=token"})
        post = {"id": "p1", "text": "dummy/a+b=token"}
        return httpx.Response(200, json={"data": [post]} if paths[-1].endswith("/threads") else post)

    caplog.set_level(logging.DEBUG)
    with make_client(handler) as client:
        assert "dummy/a+b=token" not in client.get_profile().model_dump_json()
        assert "dummy/a+b=token" not in client.get_post("p1").model_dump_json()
        assert "dummy/a+b=token" not in client.list_recent_posts()[0].model_dump_json()
    assert len(paths) == 3
    assert "dummy/a+b=token" not in caplog.text


@pytest.mark.parametrize("base", [
    "http://graph.threads.net/v1.0", "https://evil.test/v1.0", "https://graph.threads.net.evil.test/v1.0",
    "https://user:secret@graph.threads.net/v1.0", "https://graph.threads.net/v1.0?access_token=secret",
    "https://graph.threads.net/v1.0#secret",
])
def test_credentials_cannot_be_sent_to_overridden_untrusted_base(base):
    with pytest.raises(ConfigurationError) as caught:
        ThreadsClient("dummy/a+b=token", api_base=base)
    assert base not in str(caught.value)
