import asyncio
import traceback

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_service import FakeClient, service

from threads_mcp import server


def test_exact_v1_surface_annotations_and_required_key():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {
        "threads_get_profile", "threads_publish_text", "threads_publish_image", "threads_get_post",
        "threads_list_recent_posts",
    }
    publish = tools["threads_publish_text"]
    assert set(publish.input_schema["required"]) == {"text", "idempotency_key"}
    assert "topic_tag" in publish.input_schema["properties"]
    image = tools["threads_publish_image"]
    assert set(image.input_schema["required"]) == {"text", "image_url", "idempotency_key"}
    assert "topic_tag" in image.input_schema["properties"]
    for tool in (publish, image):
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.idempotent_hint is True
    for name, tool in tools.items():
        if not name.startswith("threads_publish"):
            assert tool.annotations.read_only_hint is True


@pytest.mark.parametrize("name,args", [
    ("threads_get_profile", ()), ("threads_get_post", ("p1",)),
    ("threads_list_recent_posts", ()), ("threads_publish_text", ("hello", "post:audit123")),
    ("threads_publish_image", ("hello", "https://a.b/c.jpg", "post:audit123")),
])
def test_unexpected_setup_errors_cannot_leak_to_mcp(name, args, monkeypatch):
    def broken():
        raise RuntimeError("raw-unlabeled-credential")

    monkeypatch.setattr(server, "_service", broken)
    with pytest.raises(ToolError) as caught:
        getattr(server, name)(*args)
    assert "raw-unlabeled-credential" not in "".join(traceback.format_exception(caught.value))


def test_mcp_publish_round_trip_and_replay(tmp_path, monkeypatch):
    fake = FakeClient()
    svc = service(tmp_path, fake)
    monkeypatch.setattr(server, "_service", lambda: svc)
    for _ in range(2):
        result = asyncio.run(server.mcp.call_tool(
            "threads_publish_text", {"text": "hello", "idempotency_key": "post:audit123"},
        ))
        assert result.structured_content["status"] == "published"
        assert result.structured_content["post_id"] == "p1"
    assert fake.publish_calls == 1


def test_read_tools_do_not_create_publications(tmp_path, monkeypatch):
    fake = FakeClient()
    svc = service(tmp_path, fake)
    monkeypatch.setattr(server, "_service", lambda: svc)
    server.threads_get_profile()
    server.threads_get_post("p1")
    server.threads_list_recent_posts()
    assert fake.create_calls == fake.publish_calls == 0


def test_cli_unexpected_error_does_not_print_credentials(monkeypatch, capsys):
    from threads_mcp import cli

    def broken():
        raise RuntimeError("unlabeled-dummy-app-secret")

    monkeypatch.setattr(cli, "build_parser", broken)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    assert "unlabeled-dummy-app-secret" not in capsys.readouterr().err
