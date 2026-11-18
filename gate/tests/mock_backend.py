"""Fake threads-mcp over stdio: serves the REAL tools/list schema, records every tools/call it receives,
never touches the network or any ledger."""

import json
import os
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "real_tools_list.jsonl"
RECORD = Path(os.environ["THREADS_GATE_RECORD"])
tools = next(json.loads(row)["result"] for row in FIXTURE.open() if json.loads(row).get("id") == 2)


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock", "version": "0"},
                },
            }
        )
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": json.loads(json.dumps(tools))})
    elif msg.get("method") == "tools/call" and msg["params"].get("name") == "threads_get_post":
        with RECORD.open("a") as fh:
            fh.write(json.dumps(msg["params"], ensure_ascii=False) + "\n")
        post = {
            "id": msg["params"]["arguments"]["post_id"],
            "text": os.environ.get("THREADS_GATE_MOCK_REMOTE_TEXT", ""),
            "media_type": "TEXT_POST",
            "media_url": "",
            "thumbnail_url": "",
            "permalink": "mock://p",
            "timestamp": "t",
        }
        send(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps(post, indent=2, ensure_ascii=False)}],
                    "structuredContent": post,
                    "isError": False,
                },
            }
        )
    elif msg.get("method") == "tools/call":
        with RECORD.open("a") as fh:
            fh.write(json.dumps(msg["params"], ensure_ascii=False) + "\n")
        a = msg["params"].get("arguments", {})
        send(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "content": [{"type": "text", "text": "mock"}],
                    "structuredContent": {
                        "status": "published",
                        "idempotency_key": a.get("idempotency_key", ""),
                        "post_id": "MOCK",
                        "permalink": "mock://",
                    },
                    "isError": False,
                },
            }
        )
