"""End-to-end checks against the real threads-mcp process over stdio.

No network access is needed: discovery never calls the provider, and the only tool call is made without a
configured token, so it is refused locally before any HTTP request.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

PROTOCOL_VERSION = "2025-06-18"
EXPECTED_TOOLS = {
    "threads_get_profile",
    "threads_publish_text",
    "threads_publish_image",
    "threads_get_post",
    "threads_list_recent_posts",
}
RESPONSE_TIMEOUT_SECONDS = 30


class StdioSession:
    """Keeps stdin open between requests, the way an MCP host does."""

    def __init__(self, env: dict, stderr_path: Path) -> None:
        self._stderr = stderr_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "threads_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            env=env,
        )
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put("")  # sentinel: stdout closed

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, message: dict) -> dict:
        self.send(message)
        while True:
            line = self._lines.get(timeout=RESPONSE_TIMEOUT_SECONDS)
            if not line:
                raise AssertionError("server closed stdout before answering")
            response = json.loads(line)  # stdout must carry only JSON-RPC messages
            if response.get("id") == message["id"]:
                return response

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        returncode = self.proc.wait(timeout=RESPONSE_TIMEOUT_SECONDS)
        self._stderr.close()
        return returncode


@pytest.fixture
def session(tmp_path):
    env = {
        **os.environ,
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "THREADS_TOKEN_STORE": str(tmp_path / "token.enc"),
        "THREADS_LEDGER_PATH": str(tmp_path / "ledger.sqlite3"),
        "THREADS_OAUTH_STATE_PATH": str(tmp_path / "oauth-state.json"),
    }
    stdio = StdioSession(env, tmp_path / "stderr.log")
    stdio.request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        }
    )
    stdio.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield stdio, env, tmp_path
    if stdio.proc.poll() is None:
        stdio.close()


def test_stdio_server_starts_and_exposes_the_exact_tool_surface(session) -> None:
    stdio, _, _ = session
    response = stdio.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert set(tools) == EXPECTED_TOOLS
    assert "idempotency_key" in tools["threads_publish_text"]["inputSchema"]["required"]


def test_stdio_call_without_token_is_refused_and_leaks_no_secret(session) -> None:
    stdio, env, tmp_path = session
    response = stdio.request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "threads_get_profile", "arguments": {}},
        }
    )

    assert response["result"]["isError"] is True
    assert "Threads tool failed" in json.dumps(response)
    assert not (tmp_path / "token.enc").exists()  # nothing was written during the refused call
    assert env["TOKEN_ENCRYPTION_KEY"] not in json.dumps(response)

    assert stdio.close() == 0
    assert env["TOKEN_ENCRYPTION_KEY"] not in (tmp_path / "stderr.log").read_text(encoding="utf-8")
