# Contributing

Thank you for helping improve threads-mcp. Please read the [architecture notes](docs/architecture.md) first: the
publication guarantees are the reason this project exists, and changes must keep them intact.

## Ground rules

- **Synthetic data only.** Tests, fixtures and examples must not contain real tokens, app secrets, authorization codes,
  post texts from real accounts, account names, personal data, or private hosts and paths.
- **No live calls in tests.** The suite must never reach Meta or publish anything. Use the mocked transports and fake
  clients already in the test suite.
- **Do not weaken the safety rules.** Changes must keep: no blind replay after `publish_started`; `unknown` is never
  converted to `published` automatically; the gate fails closed; tokens never appear in tool results, logs or messages.
- **Keep the tool surface stable.** Adding or renaming an MCP tool, or changing a result status, needs an issue first.

## Development setup

```sh
git clone https://github.com/kerwilgil/threads-mcp-community.git
cd threads-mcp-community
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Python 3.11 or newer is required.

## Before you open a pull request

```sh
.venv/bin/ruff check .
.venv/bin/pytest
.venv/bin/python -m compileall -q src tests gate supervisor
```

All three must pass. CI runs the same commands on Python 3.11 and 3.13.

## Style

- Python: `ruff` with the rules in `pyproject.toml` (line length 120). Keep functions small and explicit about failure.
- Shell scripts: POSIX `sh`, `set -eu`, errors to stderr, and no paths that only exist on one machine. Make locations
  configurable with environment variables.
- Documentation: English first. Keep the Spanish installation guide in `README.md` in sync with any setup change.

## Pull request checklist

- [ ] The change is covered by a test that uses synthetic data.
- [ ] `ruff`, `pytest` and `compileall` pass locally.
- [ ] No secrets, personal data or machine-specific paths were added (`git diff --cached` reviewed).
- [ ] Documentation and `CHANGELOG.md` (under "Unreleased") are updated when behaviour changes.

## Reporting security issues

Do not use public issues for vulnerabilities. Follow [SECURITY.md](SECURITY.md).
