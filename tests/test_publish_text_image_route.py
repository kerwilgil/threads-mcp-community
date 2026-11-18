"""threads_publish_text + image_url takes the same image path as threads_publish_image."""

import asyncio
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from test_image_publish import IMG, KEY, ImageFakeClient, make

from threads_mcp import server


def test_text_tool_schema_advertises_optional_image_url():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    publish = tools["threads_publish_text"]
    assert set(publish.input_schema["required"]) == {"text", "idempotency_key"}
    assert {"image_url", "topic_tag"} <= set(publish.input_schema["properties"])


def test_text_tool_with_image_url_routes_to_image_path(tmp_path: Path, monkeypatch) -> None:
    fake = ImageFakeClient(["IN_PROGRESS", "FINISHED"])
    svc, _ = make(tmp_path, fake)
    monkeypatch.setattr(server, "_service", lambda: svc)
    for _ in range(2):
        result = asyncio.run(server.mcp.call_tool(
            "threads_publish_text",
            {"text": "hello img", "idempotency_key": KEY, "image_url": IMG, "topic_tag": "AIThreads"},
        ))
        assert result.structured_content["status"] == "published"
    assert fake.image_calls == [("hello img", IMG, "AIThreads")]
    assert fake.publish_calls == 1 and fake.create_calls == 1


def test_text_tool_route_shares_ledger_with_image_tool(tmp_path: Path, monkeypatch) -> None:
    fake = ImageFakeClient(["FINISHED"])
    svc, _ = make(tmp_path, fake)
    monkeypatch.setattr(server, "_service", lambda: svc)
    assert server.threads_publish_image("hello img", IMG, KEY, "AIThreads").status == "published"
    # Same key + same content through the text tool is the same publication: replay, no second post.
    assert server.threads_publish_text("hello img", KEY, "AIThreads", IMG).status == "published"
    assert fake.publish_calls == 1
    # Same key with different content (no image) must not be accepted as a new post.
    try:
        server.threads_publish_text("hello img", KEY, "AIThreads")
    except ToolError:
        pass
    assert fake.publish_calls == 1


def test_text_tool_image_url_is_validated_like_image_tool(tmp_path: Path, monkeypatch) -> None:
    fake = ImageFakeClient(["FINISHED"])
    svc, _ = make(tmp_path, fake)
    monkeypatch.setattr(server, "_service", lambda: svc)
    for bad_url, tag in (("http://insecure/x.jpg", ""), ("https://a.b/x .jpg", ""), (IMG, "a.b"), (IMG, "a&b")):
        try:
            server.threads_publish_text("hello img", KEY, tag, bad_url)
        except ToolError:
            continue
        raise AssertionError(f"accepted {bad_url!r} {tag!r}")
    assert fake.create_calls == 0 and fake.publish_calls == 0


def test_text_tool_without_image_url_stays_text(tmp_path: Path, monkeypatch) -> None:
    fake = ImageFakeClient(["FINISHED"])
    svc, _ = make(tmp_path, fake)
    monkeypatch.setattr(server, "_service", lambda: svc)
    assert server.threads_publish_text("plain", KEY, "AIThreads").status == "published"
    assert fake.image_calls == []
