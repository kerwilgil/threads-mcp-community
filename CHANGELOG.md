# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-17

First public, community edition. Each installation uses its own Meta app, its own Threads account and its own local
secrets, ledger and approval registry.

### Added

- MCP tools over stdio: `threads_get_profile`, `threads_publish_text`, `threads_publish_image`, `threads_get_post` and
  `threads_list_recent_posts`.
- Text posts with optional `topic_tag`, and image posts from a public HTTPS URL (`threads_publish_image`, or
  `threads_publish_text` with `image_url`).
- OAuth set-up through the `threads-mcp-auth` command line tool (`keygen`, `auth-url`, `exchange`, `status`). OAuth is not
  exposed as MCP tools.
- Encrypted token store (Fernet) with atomic writes and restrictive file modes on POSIX systems.
- SQLite publication ledger with idempotency keys, content binding, fencing of stale workers and durable
  `publish_started` boundaries. Ambiguous outcomes are reported as `unknown` and never replayed.
- Approval gate (`gate/threads_publication_gate.py`): only texts registered in a local registry can be published; the
  idempotency key is taken from the registry. Includes canonical verification of posts published through the gate.
- Stdio supervisor (`supervisor/threads_stdio_supervisor.py`) and wrapper (`bin/threads-mcp-supervised`) that keep an MCP
  host attached when a child process exits, without repeating in-flight publications.
- Test suite with synthetic data (unit, integration, OAuth against a mocked provider, stdio end-to-end, gate and
  supervisor tests).
- Documentation in English with a Spanish installation guide, architecture notes, and gate and supervisor references.

### Notes

- Supported platforms: macOS and Linux. Python 3.11 or newer.
- Not included: replies, moderation, insights, video, carousels and deletion.
