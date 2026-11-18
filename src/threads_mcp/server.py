"""MCP tool surface for Threads."""

from __future__ import annotations

from functools import lru_cache, wraps

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .config import Settings
from .errors import ThreadsMCPError
from .models import Profile, PublishResult, ThreadPost
from .service import ThreadsService

mcp = MCPServer(
    "Threads Write",
    version="0.1.0",
    instructions=(
        "Use this server to read the connected Threads account and publish content that has already been approved. "
        "Always supply a stable idempotency_key for publication. A result with status=unknown must never be blindly "
        "replayed. Read-only verification can help an operator, but an identical feed post cannot prove attribution."
    ),
)


@lru_cache(maxsize=1)
def _service() -> ThreadsService:
    return ThreadsService(Settings.from_env())


def _safe_tool(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (ThreadsMCPError, ValueError):
            raise ToolError(
                "Threads tool failed. Check local authentication, input, and publication state."
            ) from None
        except Exception:
            # ToolError is the only exception type the MCP SDK forwards to the client as its message.
            # Other exceptions reach the client as a generic error and are logged with their traceback.
            raise ToolError(
                "Threads tool encountered a local failure; verify publication state before retrying."
            ) from None
    return wrapped


@mcp.tool(
    title="Get Threads profile",
    description="Read the authenticated Threads profile.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
)
@_safe_tool
def threads_get_profile() -> Profile:
    return _service().get_profile()


@mcp.tool(
    title="Publish Threads text",
    description=(
        "Publish one approved text post to Threads. idempotency_key must remain stable for retries of the same logical "
        "publication. Optional topic_tag (1-50 characters without '.' or '&'). Optional image_url (public HTTPS) turns "
        "the call into an image post, exactly like threads_publish_image. The tool prevents blind retries when "
        "provider outcome is unknown."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
@_safe_tool
def threads_publish_text(
    text: str, idempotency_key: str, topic_tag: str = "", image_url: str = ""
) -> PublishResult:
    # Hosts that cached the original tool list never see threads_publish_image; an image_url here
    # takes the very same validated image path.
    if image_url:
        return _service().publish_image(text, image_url, idempotency_key, topic_tag)
    return _service().publish_text(text, idempotency_key, topic_tag)


@mcp.tool(
    title="Publish Threads image",
    description=(
        "Publish one approved image post (text + public HTTPS image_url, optional topic_tag of 1-50 characters "
        "without '.' or '&') to Threads. idempotency_key must remain stable for retries of the same logical "
        "publication. The media container is published only after Threads reports it FINISHED. The tool prevents "
        "blind retries when provider outcome is unknown."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
@_safe_tool
def threads_publish_image(text: str, image_url: str, idempotency_key: str, topic_tag: str = "") -> PublishResult:
    return _service().publish_image(text, image_url, idempotency_key, topic_tag)


@mcp.tool(
    title="Get Threads post",
    description="Fetch one Threads post by provider post id for verification.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
)
@_safe_tool
def threads_get_post(post_id: str) -> ThreadPost:
    return _service().get_post(post_id)


@mcp.tool(
    title="List recent Threads posts",
    description="List recent Threads posts for verification and duplicate detection.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
)
@_safe_tool
def threads_list_recent_posts(limit: int = 10) -> list[ThreadPost]:
    return _service().list_recent_posts(limit)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
