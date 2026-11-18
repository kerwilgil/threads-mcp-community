# Image posts and topic tags

## Tools

- `threads_publish_image(text, image_url, idempotency_key, topic_tag="")` publishes an image post.
- `threads_publish_text(text, idempotency_key, topic_tag="", image_url="")` also accepts an optional `image_url`. When it
  is non-empty, the call takes exactly the same validated image path. This exists for hosts that cached an older tool
  list and never see `threads_publish_image`.

## Validation (rejected before any write)

- `image_url`: public `https://` URL, at most 2048 characters, no whitespace or control characters, no credentials.
- `topic_tag`: 1-50 characters, no `.`, `&`, whitespace or control characters (Threads rule). Empty means no tag.

## Image flow

1. `POST /{user-id}/threads` with `media_type=IMAGE`, `image_url`, `text` and `topic_tag`. The `link_attachment` field
   is never sent for images.
2. Read-only `GET /{container-id}?fields=id,status`, every 3 seconds, up to 15 checks.
3. `POST /{user-id}/threads_publish` runs only after the container reports `FINISHED`.

Outcomes:

- **Still processing** after the checks: the result is `failed` with a "not ready" message and nothing is published. The
  ledger row is `retryable` and keeps the container. Call again with the same `publication_ref`; the same container is
  reused.
- **`ERROR` or `EXPIRED`**: nothing is published. The row is `retryable` with the container cleared, so the next explicit
  call creates a fresh container. Check the image URL first.
- **`PUBLISHED` before our publish call**: the result is `unknown`. The post is never replayed.

## Ledger

Two columns, `image_url` and `topic_tag`, are added in place when the ledger starts (default `''`). The row digest binds
text, image URL and tag. Reusing a key with other media or another tag raises an idempotency conflict.

## Approved-publications registry (gate)

Entries stay `version: 1`. Two optional fields are added:

| Field | Meaning |
|---|---|
| `image_url` | The approved image. An entry with it approves only `threads_publish_image` (or `threads_publish_text` with the same `image_url`), and the URL must match exactly. |
| `topic_tag` | The approved tag. The call's `topic_tag` must match exactly. An absent field means no tag. |

Entries without them approve text-only posts, as before. A text-only entry never approves an image, and an image entry
never publishes without its image.

Register an image post (the text is used byte-for-byte):

```sh
python3 gate/threads_publication_gate.py register \
  --key demo-2026-02-01-image-post-v1 --text-file ./approved-image-text.txt \
  --image-url https://example.com/images/demo.jpg --topic-tag ExampleTopic \
  --label "Image post" --approved-by "operator, 2026-02-01"
```

Gate rejections end with `[gate received fields: ...]`, which lists argument names only, never values, so an operator
can see what the host forwarded.
