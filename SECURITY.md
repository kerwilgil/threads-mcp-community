# Security policy

## Supported versions

Only the latest release on the `main` branch receives security fixes.

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

1. Use GitHub's private vulnerability reporting: open the **Security** tab of this repository and choose
   **Report a vulnerability**.
2. If that option is not available, open a regular issue that only says you have a security report and asks for a
   private channel. Do not include exploit details, tokens, encryption keys or personal data in a public issue.

Include, when possible: the affected version or commit, the component (`src/threads_mcp`, `gate/`, `supervisor/`,
`bin/`), steps to reproduce using synthetic data, and the expected and actual behaviour.

## What to expect

- An acknowledgement as soon as the maintainer reads the report.
- An assessment and, if the report is valid, a fix in a new release with a note in `CHANGELOG.md`.
- Credit in the release notes if you want it.

## Security model

The threat model and guarantees are described in [docs/architecture.md](docs/architecture.md). In particular, this
project does not protect against an attacker who already controls the machine, can read the encryption key file, or
can change the approval registry. Those are operating-system and operator responsibilities.

## Handling secrets

- Never commit `.env` files, encryption keys, tokens, the approval registry, the publication ledger or OAuth state files.
  `.gitignore` excludes them, but check `git status` before every commit.
- Do not paste real tokens, app secrets or authorization codes into issues, pull requests or chat messages.
