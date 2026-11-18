# Architecture and publication safety

This document describes how threads-mcp publishes, what it guarantees, and what it deliberately does not do.

## Components

```text
MCP host (any compatible client)
      |  stdio (JSON-RPC)
      v
bin/threads-mcp-supervised           keeps the host attached when a child exits
      |
      v
gate/threads_publication_gate.py     approval: exact text + registry key, fail closed
      |
      v
bin/threads-mcp-launcher             loads the encryption key from the local config directory
      |
      v
threads-mcp (src/threads_mcp)        tools, OAuth, encrypted token store, publication ledger
      |
      v
Meta Threads Graph API (official, HTTPS only)
```

- **Token store**: the Threads access token is encrypted with Fernet (AES-128-CBC with HMAC-SHA256). The key is a
  separate file that the launcher reads. Tokens are never returned by any tool.
- **Publication ledger**: a local SQLite database (WAL mode, `synchronous=FULL`) that records every publication
  attempt by idempotency key. It is the duplicate-protection mechanism.
- **Gate**: the editorial control. Nothing reaches the backend unless its exact text is registered.

## Tool surface

| Tool | Kind | Notes |
|---|---|---|
| `threads_get_profile` | read | Authenticated profile without credentials. |
| `threads_publish_text` | write | Text post. Optional `topic_tag`; a non-empty `image_url` takes the image path. |
| `threads_publish_image` | write | Image post from a public HTTPS URL. Optional `topic_tag`. |
| `threads_get_post` | read | One post by provider id, for verification. |
| `threads_list_recent_posts` | read | Recent posts (1-100), for verification. Read tools never create publications. |

Write tools require a stable `idempotency_key` (8-128 characters of letters, digits, `.`, `_`, `:` or `-`). Behind the
gate the host supplies `publication_ref` instead, and the gate forwards the registry key.

## Text publication flow

```text
reserve idempotency key (ledger row, digest of the content)
        |
        v
claim (unique owner token; stale pre-publication claims may be reclaimed after 120 s)
        |
        v
POST /{user_id}/threads                 media_type=TEXT, auto_publish_text=false  -> creation_id
        |
        v
persist publish_started                 durable BEFORE the public write
        |
        v
POST /{user_id}/threads_publish         creation_id  -> post id
        |
        +--> post id returned             -> published (id persisted before any further lookup)
        +--> proven unsent (connect error) -> retryable (same key, same container may be reused)
        +--> definitive Graph 4xx rejection -> failed (terminal for this key)
        +--> timeout, 408/425/429, 5xx, malformed or ambiguous response -> unknown
```

Image publication creates an `IMAGE` container, polls its status with read-only `GET` requests (up to 15 checks, 3 s
apart), and calls `threads_publish` only after the container reports `FINISHED`. See
[images-and-topic-tags.md](images-and-topic-tags.md).

## Guarantees

- **No blind replay after the public boundary.** Once `publish_started` is persisted, an ambiguous outcome is `unknown`
  and is never retried automatically. Restarts keep it that way, even if the process crashed before sending anything.
- **Same key, same content.** The ledger digest binds the text, `image_url` and `topic_tag`. Reusing a key for other
  content is rejected without any write. Plain text keeps the historical `sha256(text)` digest, so existing rows stay
  valid.
- **Fencing.** Every ledger transition checks the current owner token and the previous state, so a slow worker cannot
  cross the publish boundary after its claim was reclaimed.
- **Evidence is durable.** The provider post id is committed before any optional permalink lookup.
- **Narrow error surface.** Provider response bodies, URLs, headers and raw exception chains are not copied into
  messages. A rejection may include only a shape-checked HTTP status, Graph error code, subcode, type, `is_transient`
  flag and trace id. Credential-bearing HTTP library logs are suppressed during requests to Meta.
- **Official API only.** Publication never uses browser automation. The API base must be an HTTPS `graph.threads.net`
  version URL; environment proxies and redirects cannot forward credentials.

## What `unknown` means for an operator

`unknown` means Threads may or may not have published the post. Operators should check the account in the Threads app
or with `threads_list_recent_posts` before doing anything else.

Exact-text matches in the feed do **not** prove that a specific attempt published. A manual post with the same text
looks identical. For that reason the server never converts `unknown` into `published` automatically, and no read tool
resets the key. Recovery requires authoritative evidence that links a specific container to a post id.

## Explicit non-goals (this release)

- Automatic publishing without an approval step. The gate is the approval mechanism; it is not optional for agent use.
- Replies, moderation, insights, video, carousels and deletion.
- Editorial decisions. The server executes publication requests that were already approved; it does not decide what to
  approve.

## Operating notes

- Keep the ledger on durable local storage and reuse the same key for the same logical publication. Deleting the
  ledger, changing the encryption key or running independent ledger copies removes duplicate protection.
- Stop older server versions before upgrading. Mixed old and new workers do not share the owner checks.
- Fernet protects stored credentials against casual reads, not against an attacker who controls the machine or can read
  the key file. Protect both the key file and the token store with operating-system permissions.
