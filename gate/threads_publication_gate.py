#!/usr/bin/env python3
"""Deterministic publication gate in front of threads-mcp (stdio JSON-RPC proxy).

Why: the idempotency key of an approved publication must reach threads_publish_text
byte-for-byte as fixed by the approval flow, and an approver must be able to read it
in approval UIs. Some MCP hosts redact every tool argument whose name ends in `_key`,
so `idempotency_key` may never be visible to the approver.

What it does:
  * tools/list: threads_publish_text is exposed with (text, publication_ref) instead of
    (text, idempotency_key). `publication_ref` is not matched by that redaction.
  * tools/call threads_publish_text: sha256(text) must be in the approved-publications
    registry and publication_ref must equal the registry key exactly. The gate then
    forwards {text, idempotency_key=<registry key>}. Anything else is rejected here,
    before threads-mcp is reached.
  * threads_publish_image (text, image_url, publication_ref, optional topic_tag): same rule, and the
    registry entry must also carry exactly this image_url (and topic_tag, if any). Entries without
    image_url only approve threads_publish_text; entries with image_url only approve threads_publish_image.
  * optional topic_tag on either publish tool must equal the entry's topic_tag (absent = no tag).
  * threads_publish_text with an `image_url` argument (hosts that cached the original tool list) is
    checked exactly like threads_publish_image: only an image entry with this exact image_url and
    topic_tag approves it, and image_url is forwarded so threads-mcp takes the image path.
  * any other tools/call whose name starts with `threads_publish` is rejected (fail closed).
  * every other message is passed through unchanged.

Stdlib only. threads-mcp itself is not modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

PUBLISH = "threads_publish_text"
PUBLISH_IMAGE = "threads_publish_image"
PUBLISH_TOOLS = (PUBLISH, PUBLISH_IMAGE)
PUBLISH_PREFIX = "threads_publish"
REF_FIELD = "publication_ref"
TOPIC_TAG_RE = re.compile(r"[^.&\s\x00-\x1f\x7f]{1,50}")  # Threads: 1-50 chars, no '.' or '&'
IMAGE_URL_RE = re.compile(r"https://[^\s\x00-\x1f\x7f]{1,2040}")
KEY_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")  # same rule as threads-mcp
REF_DESCRIPTION = (
    "Exact reference of the user-approved publication, e.g. demo-2026-01-15-launch-post-v1. "
    "It must match the approved-publications registry entry for this exact text; it is used "
    "verbatim as the idempotency key. Do not invent, shorten or reformat it."
)


class GateError(Exception):
    pass


def registry_path() -> Path:
    override = os.environ.get("THREADS_GATE_REGISTRY")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "threads-mcp" / "approved-publications.json"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_registry(path: Path) -> dict[str, dict]:
    """Return {sha256: entry}. Any problem raises GateError, so the gate fails closed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GateError(f"approved-publications registry not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise GateError(f"approved-publications registry unreadable: {exc}") from None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("publications"), list):
        raise GateError("approved-publications registry has an invalid structure")
    by_hash: dict[str, dict] = {}
    keys: set[str] = set()
    for entry in data["publications"]:
        if not isinstance(entry, dict):
            raise GateError("approved-publications registry has an invalid entry")
        key, digest, chars = entry.get("key"), entry.get("text_sha256"), entry.get("chars")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise GateError("approved-publications registry has an invalid key")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(chars, int):
            raise GateError("approved-publications registry has an invalid hash or length")
        image_url, topic_tag = entry.get("image_url"), entry.get("topic_tag")
        if image_url is not None and (not isinstance(image_url, str) or not IMAGE_URL_RE.fullmatch(image_url)):
            raise GateError("approved-publications registry has an invalid image_url")
        if topic_tag is not None and (not isinstance(topic_tag, str) or not TOPIC_TAG_RE.fullmatch(topic_tag)):
            raise GateError("approved-publications registry has an invalid topic_tag")
        if digest in by_hash or key in keys:
            raise GateError("approved-publications registry has a duplicate key or text")
        by_hash[digest] = entry
        keys.add(key)
    return by_hash


def check_publish(arguments: object, registry: dict[str, dict], tool: str = PUBLISH) -> dict:
    """Validate a publish call and return the arguments to forward to threads-mcp."""
    # threads_publish_text carrying image_url is an image post and gets the image rules (no bypass).
    image = tool == PUBLISH_IMAGE or (tool == PUBLISH and isinstance(arguments, dict) and "image_url" in arguments)
    if tool not in PUBLISH_TOOLS:
        raise GateError(f"{tool} is not an approved publish tool. Nothing was published.")
    required = {"text", REF_FIELD, "image_url"} if image else {"text", REF_FIELD}
    if not isinstance(arguments, dict) or not required <= set(arguments) <= required | {"topic_tag"}:
        optional = "and optional `topic_tag`" if tool == PUBLISH_IMAGE else "and optional `topic_tag`, `image_url`"
        fields = "`text`, `image_url`" if tool == PUBLISH_IMAGE else "`text`"
        raise GateError(f"{tool} takes exactly {fields}, `{REF_FIELD}` {optional} (no idempotency_key).")
    if not all(isinstance(value, str) for value in arguments.values()):
        raise GateError("text, image_url, topic_tag and publication_ref must be strings.")
    text, ref = arguments["text"], arguments[REF_FIELD]
    entry = registry.get(text_hash(text))
    if entry is None:
        raise GateError("This exact text is not in the approved-publications registry. Nothing was published.")
    if ref != entry["key"]:
        raise GateError(
            f"publication_ref does not match the approved reference for this text "
            f"(expected {entry['key']!r}, got {ref!r}). Nothing was published."
        )
    if len(text) != entry["chars"]:
        raise GateError("Text length does not match the approved entry. Nothing was published.")
    approved_image = entry.get("image_url") or ""
    approved_tag = entry.get("topic_tag") or ""
    if image and not approved_image:
        raise GateError("The approved entry has no image_url; use threads_publish_text. Nothing was published.")
    if not image and approved_image:
        raise GateError(
            "The approved entry includes an image; pass its image_url (or use threads_publish_image). "
            "Nothing was published."
        )
    if image and arguments["image_url"] != approved_image:
        raise GateError("image_url does not match the approved entry exactly. Nothing was published.")
    if arguments.get("topic_tag", "") != approved_tag:
        raise GateError("topic_tag does not match the approved entry exactly. Nothing was published.")
    forward = {"text": text, "idempotency_key": entry["key"]}
    if image:
        forward["image_url"] = approved_image
    if approved_tag:
        forward["topic_tag"] = approved_tag
    return forward


def transform_tools(result: dict) -> dict:
    tools = result.get("tools", [])
    # Fail closed: publish-like tools the gate does not know are hidden from the host.
    if isinstance(tools, list):
        result["tools"] = tools = [
            t for t in tools
            if not (isinstance(t, dict) and str(t.get("name", "")).startswith(PUBLISH_PREFIX)
                    and t.get("name") not in PUBLISH_TOOLS)
        ]
    for tool in tools:
        if tool.get("name") not in PUBLISH_TOOLS:
            continue
        image = tool["name"] == PUBLISH_IMAGE
        schema = tool.get("inputSchema") or {}
        props = dict(schema.get("properties") or {})
        props.pop("idempotency_key", None)
        props[REF_FIELD] = {"title": "Publication Ref", "type": "string", "description": REF_DESCRIPTION}
        if not image and "image_url" in props:
            props["image_url"] = {
                "title": "Image Url", "type": "string", "default": "",
                "description": "Optional public HTTPS image URL; must equal the approved entry's image_url exactly "
                "(omit for text-only posts).",
            }
        if "topic_tag" in props:
            props["topic_tag"] = {
                "title": "Topic Tag", "type": "string", "default": "",
                "description": "Optional topic tag; must equal the approved entry's topic_tag exactly (omit if none).",
            }
        schema["properties"] = props
        schema["required"] = ["text", "image_url", REF_FIELD] if image else ["text", REF_FIELD]
        tool["inputSchema"] = schema
        if image:
            tool["description"] = (
                "Publish one user-approved image post to Threads. text, image_url and topic_tag must match one "
                "entry of the approved-publications registry exactly and `publication_ref` must be that entry's "
                "exact reference (used as the idempotency key; retries of the same publication reuse it). The tool "
                "prevents blind retries when the provider outcome is unknown."
            )
        else:
            tool["description"] = (
                "Publish one user-approved text post to Threads. The text must be an entry of the approved-"
                f"publications registry and `{REF_FIELD}` must be that entry's exact reference (used as the "
                "idempotency key; retries of the same publication reuse it). Optional topic_tag must equal the "
                "entry's topic_tag. The tool prevents blind retries when the provider outcome is unknown."
            )
    return result


# --- canonical verification of a published post -------------------------------------------
# Threads turns the FIRST hashtag of a post into its native topic tag and the API then returns
# the text without that single `#`. That is the only difference tolerated.
HASHTAG = re.compile(r"(?<!\S)#(?=\w)")


def canonical_verification(requested: str, remote: str) -> dict:
    """PASS iff remote == requested, or remote == requested minus ONLY the '#' of its first hashtag."""
    exact = remote == requested
    normalized = False
    if not exact:
        m = HASHTAG.search(requested)
        if m and remote == requested[: m.start()] + requested[m.start() + 1 :]:
            normalized = True
    return {
        "requested_text": requested,
        "remote_text": remote,
        "exact_match": exact,
        "threads_topic_normalization": normalized,
        "verification": "PASS" if (exact or normalized) else "FAIL",
    }


def ledger_path() -> Path:
    override = os.environ.get("THREADS_LEDGER_PATH")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "threads-mcp" / "publications.sqlite3"


def ledger_record(post_id: object) -> tuple[str, str] | None:
    """(idempotency_key, requested_text) of the ledger row that published post_id (read-only)."""
    if not isinstance(post_id, str) or not post_id:
        return None
    path = ledger_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "select idempotency_key, text from publications where post_id = ? and status = 'published'",
                (post_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return (row[0], row[1]) if row else None


def audit_path() -> Path:
    return ledger_path().parent / "gate-verifications.ndjson"


def augment_get_post(result: dict) -> dict:
    """Add a canonical `verification` block to a threads_get_post result (read-only; the post is untouched)."""
    sc = result.get("structuredContent")
    if result.get("isError") or not isinstance(sc, dict):
        return result
    record = ledger_record(sc.get("id"))
    if record is None:
        verification = {"verification": "NOT_APPLICABLE", "reason": "post_id is not a published entry of the ledger"}
    else:
        key, requested = record
        verification = {
            "post_id": sc.get("id"),
            "publication_ref": key,
            **canonical_verification(requested, sc.get("text") or ""),
        }
        try:
            audit = audit_path()
            fd = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"at": datetime.now(UTC).isoformat(timespec="seconds"), **verification}, ensure_ascii=False
                    )
                    + "\n"
                )
        except OSError:
            pass  # the verification itself is still returned
    sc["verification"] = verification
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                obj = json.loads(item["text"])
            except (TypeError, ValueError):
                continue
            if isinstance(obj, dict):
                obj["verification"] = verification
                item["text"] = json.dumps(obj, indent=2, ensure_ascii=False)
    return result


def register(args: argparse.Namespace) -> int:
    path = registry_path()
    text = Path(args.text_file).read_bytes().decode("utf-8")
    if not KEY_RE.fullmatch(args.key):
        raise SystemExit("key must be 8-128 characters using letters, digits, '.', '_', ':', or '-'.")
    try:
        existing = load_registry(path)
    except GateError as exc:
        if path.exists():
            raise SystemExit(f"refusing to modify an unreadable registry: {exc}") from None
        existing = {}
    digest = text_hash(text)
    if digest in existing or any(e["key"] == args.key for e in existing.values()):
        raise SystemExit("this text or key is already registered; entries are never overwritten.")
    if args.image_url is not None and not IMAGE_URL_RE.fullmatch(args.image_url):
        raise SystemExit("image_url must be an https:// URL without whitespace.")
    if args.topic_tag is not None and not TOPIC_TAG_RE.fullmatch(args.topic_tag):
        raise SystemExit("topic_tag must be 1-50 characters without '.', '&', or whitespace.")
    entry = {
        "key": args.key,
        "text_sha256": digest,
        "chars": len(text),
        "label": args.label or "",
        "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "approved_by": args.approved_by,
    }
    if args.image_url is not None:
        entry["image_url"] = args.image_url
    if args.topic_tag is not None:
        entry["topic_tag"] = args.topic_tag
    entries = [*existing.values(), entry]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".approved-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "publications": entries}, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    extra = "".join(f" {k}={entry[k]}" for k in ("image_url", "topic_tag") if k in entry)
    print(f"registered key={args.key} chars={len(text)} sha256={digest}{extra}")
    return 0


def list_registry(_: argparse.Namespace) -> int:
    for digest, e in load_registry(registry_path()).items():
        extra = "".join(f"  {k}={e[k]}" for k in ("image_url", "topic_tag") if k in e)
        print(f"{e['key']}  chars={e['chars']}  sha256={digest}{extra}  label={e.get('label', '')}")
    return 0


def _canon(msg_id: object) -> str:
    return json.dumps(msg_id, sort_keys=True)


def proxy(child_cmd: list[str]) -> int:
    child = subprocess.Popen(child_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out_lock = threading.Lock()
    list_ids: set[str] = set()
    get_post_ids: set[str] = set()
    ids_lock = threading.Lock()

    def emit(raw: bytes) -> None:
        with out_lock:
            sys.stdout.buffer.write(raw if raw.endswith(b"\n") else raw + b"\n")
            sys.stdout.buffer.flush()

    def reject(msg_id: object, message: str) -> None:
        emit(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"BLOCKED by publication gate: {message}"}],
                        "isError": True,
                    },
                }
            ).encode()
        )

    def from_child() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                emit(line)
                continue
            if isinstance(msg, dict) and "result" in msg and "id" in msg:
                with ids_lock:
                    is_list = _canon(msg["id"]) in list_ids
                    list_ids.discard(_canon(msg["id"]))
                    is_get_post = _canon(msg["id"]) in get_post_ids
                    get_post_ids.discard(_canon(msg["id"]))
                if is_list and isinstance(msg["result"], dict):
                    msg["result"] = transform_tools(msg["result"])
                    line = json.dumps(msg).encode()
                elif is_get_post and isinstance(msg["result"], dict):
                    msg["result"] = augment_get_post(msg["result"])
                    line = json.dumps(msg, ensure_ascii=False).encode("utf-8")
            emit(line)

    threading.Thread(target=from_child, daemon=True).start()
    assert child.stdin is not None

    def is_publish(m: object) -> bool:
        return (
            isinstance(m, dict)
            and m.get("method") == "tools/call"
            and isinstance(m.get("params"), dict)
            and isinstance(m["params"].get("name"), str)
            and m["params"]["name"].startswith(PUBLISH_PREFIX)
        )

    for line in sys.stdin.buffer:
        try:
            msg = json.loads(line)
        except ValueError:
            child.stdin.write(line)
            child.stdin.flush()
            continue
        if isinstance(msg, list):  # JSON-RPC batches are not used by MCP; never let one carry a publish
            if any(is_publish(m) for m in msg):
                continue
        elif isinstance(msg, dict) and msg.get("method") == "tools/list" and "id" in msg:
            with ids_lock:
                list_ids.add(_canon(msg["id"]))
        elif (
            isinstance(msg, dict)
            and msg.get("method") == "tools/call"
            and "id" in msg
            and isinstance(msg.get("params"), dict)
            and msg["params"].get("name") == "threads_get_post"
        ):
            with ids_lock:
                get_post_ids.add(_canon(msg["id"]))
        elif is_publish(msg):
            try:
                forward = check_publish(
                    msg["params"].get("arguments"), load_registry(registry_path()), msg["params"]["name"]
                )
            except GateError as exc:
                # Field NAMES only (never values) so an operator can see what the host actually forwarded.
                received = msg["params"].get("arguments")
                names = sorted(k for k in received if isinstance(k, str)) if isinstance(received, dict) else []
                reject(msg.get("id"), f"{exc} [gate received fields: {', '.join(names) or 'none'}]")
                continue
            msg["params"]["arguments"] = forward
            line = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        child.stdin.write(line if line.endswith(b"\n") else line + b"\n")
        child.stdin.flush()
    try:
        child.stdin.close()
    except OSError:
        pass
    return child.wait()


def main() -> int:
    parser = argparse.ArgumentParser(prog="threads_publication_gate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("proxy", help="stdio proxy: proxy -- <threads-mcp command...>")
    p.add_argument("child", nargs=argparse.REMAINDER)
    r = sub.add_parser("register", help="add an approved publication (never overwrites)")
    r.add_argument("--key", required=True)
    r.add_argument("--text-file", required=True, help="file holding the exact approved text (used byte-for-byte)")
    r.add_argument("--image-url", help="approved public https image URL (image posts only)")
    r.add_argument("--topic-tag", help="approved Threads topic tag (1-50 chars, no '.' or '&')")
    r.add_argument("--label")
    r.add_argument("--approved-by", default="user")
    sub.add_parser("list", help="show registered approved publications")
    args = parser.parse_args()
    if args.cmd == "proxy":
        cmd = args.child[1:] if args.child[:1] == ["--"] else args.child
        if not cmd:
            raise SystemExit("proxy needs the threads-mcp command after `--`")
        return proxy(cmd)
    if args.cmd == "register":
        return register(args)
    return list_registry(args)


if __name__ == "__main__":
    raise SystemExit(main())
