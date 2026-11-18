# Publication gate

A small stdio JSON-RPC proxy between an MCP host and the `threads-mcp` server. It turns the editorial approval
into a technical check: only texts that are registered as approved can be published, and the idempotency key is
taken from the registry, never from the model. It uses only the Python standard library and does not modify
`threads-mcp`.

```
MCP host --stdio--> bin/threads-mcp-supervised (or gate/bin/threads-mcp-gated-launcher)
                      --> gate/threads_publication_gate.py proxy
                            --> bin/threads-mcp-launcher --> threads-mcp
```

Python 3.11 or newer is required for the gate (`python3` by default; set `THREADS_GATE_PYTHON` to choose another
interpreter).

## What it does

| Message | Behaviour |
|---|---|
| `tools/list` | `threads_publish_text` and `threads_publish_image` are exposed with `publication_ref` instead of `idempotency_key`. Unknown `threads_publish*` tools are hidden. |
| `tools/call threads_publish_text` / `threads_publish_image` | The text (and `image_url` / `topic_tag` when present) must match a registry entry exactly, and `publication_ref` must equal that entry's key. The gate forwards `{text, idempotency_key=<registry key>}`. Anything else is rejected here, before `threads-mcp` is reached. |
| `tools/call threads_get_post` | The result gets a `verification` block (see below). The remote post itself is not changed. |
| everything else | Passed through unchanged. |

Why `publication_ref` and not `idempotency_key`: some MCP hosts redact tool arguments whose names end in `_key` in
their approval UI, so the key would not be readable by the person approving the call. `publication_ref` is not
matched by that redaction, so the approver can read the full reference.

Rejection rules (all fail closed; nothing is published):

- text not in the registry, or changed by even one byte (including whitespace);
- `publication_ref` missing, abbreviated or different from the registered key;
- extra arguments, including a legacy `idempotency_key`;
- registry missing, unreadable, invalid, or with duplicate keys or texts;
- JSON-RPC batches that contain a publish call;
- an `image_url` or `topic_tag` that does not match the approved entry exactly.

## The registry: `approved-publications.json`

Location: `$XDG_CONFIG_HOME/threads-mcp/approved-publications.json` (default `~/.config/threads-mcp/`), or the path in
`THREADS_GATE_REGISTRY`. **The registry is never committed** (`.gitignore` excludes it). It is the only editorial
authorization, so whoever can write it can authorize publications.

```json
{
  "version": 1,
  "publications": [
    {
      "key": "demo-2026-01-15-launch-post-v1",
      "text_sha256": "<sha256 of the exact UTF-8 text>",
      "chars": 473,
      "label": "free text",
      "approved_at": "2026-01-15T09:00:00+00:00",
      "approved_by": "who approved it"
    }
  ]
}
```

- `key`: 8-128 characters of `A-Za-z0-9._:-`. Unique; it never changes for a given piece of content.
- One entry per approved text; a text and a key can each appear only once.
- Optional `image_url` and `topic_tag` fields bind an entry to an image post (see
  [docs/images-and-topic-tags.md](../docs/images-and-topic-tags.md)).

### Permissions

| Path | Mode |
|---|---|
| `~/.config/threads-mcp/` | `700` |
| `approved-publications.json` | `600` |
| `~/.local/state/threads-mcp/gate-verifications.ndjson` (verification log, created by the gate) | `600` |

### Registering an approved publication

Register only after the person responsible has approved the **exact** text.

```sh
printf '%s' "$APPROVED_TEXT" > ./approved-piece.txt    # exact bytes: no trailing newline, no edits
python3 gate/threads_publication_gate.py register \
    --key demo-2026-01-15-launch-post-v1 --text-file ./approved-piece.txt \
    --label "Launch post" --approved-by "operator, 2026-01-15"
rm -f ./approved-piece.txt
python3 gate/threads_publication_gate.py list           # key, chars and sha256 of every entry
```

`register` writes atomically with mode `600`, refuses to overwrite an entry and refuses to touch an unreadable
registry. The new entry is used by the next publish call; no restart is needed. The text is used byte-for-byte, so a
trailing newline in the file becomes part of the approved text.

## Canonical verification (`threads_get_post`)

For a post that the ledger records as `published`, the result includes a `verification` block:

```json
{"post_id": "...", "publication_ref": "...", "requested_text": "...", "remote_text": "...",
 "exact_match": false, "threads_topic_normalization": true, "verification": "PASS"}
```

`PASS` only if `remote_text == requested_text`, or if it equals `requested_text` minus the single `#` of its **first**
hashtag. Threads turns the first hashtag into its native topic tag and returns the text without that `#`. A hashtag is
a `#` preceded by whitespace or the start of the text and followed by a word character, so a `#` inside a URL does not
count. Any other difference is `FAIL`. Posts that are not in the ledger report `NOT_APPLICABLE`. Each verification is
appended to `gate-verifications.ndjson`.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `THREADS_GATE_REGISTRY` | Registry file | `$XDG_CONFIG_HOME/threads-mcp/approved-publications.json` |
| `THREADS_LEDGER_PATH` | Publication ledger (read-only for the gate) | `$XDG_STATE_HOME/threads-mcp/publications.sqlite3` |
| `THREADS_GATE_DIR` | Gate directory (launcher only) | directory above `gate/bin/` |
| `THREADS_MCP_LAUNCHER` | Backend launcher (gated launcher only) | `<repo>/bin/threads-mcp-launcher` |
| `THREADS_GATE_PYTHON` | Python 3.11+ used by the wrapper scripts | `python3` |

## Tests

```sh
python -m pip install -e '.[dev]'
pytest gate/tests
```

The suite uses a mock backend that records every call it receives. It never touches the network or a real ledger, and
it uses only synthetic texts and keys.

## Known limits

- The gate only publishes texts that were registered beforehand. Changing a text means a new registry entry and a new
  key.
- Threads may normalize the first hashtag; verification accepts exactly that normalization and nothing else.
- Removing the gate from the MCP host configuration (calling `bin/threads-mcp-launcher` directly) removes the approval
  control: the model can then publish any text and choose its own idempotency key. Keep the gate in the host
  configuration for agent use.
