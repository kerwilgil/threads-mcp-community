# Threads MCP

A controlled [Model Context Protocol](https://modelcontextprotocol.io) server for reading and publishing to a
**Meta Threads** account through the **official Threads API**. Each person runs their own copy, registers their own Meta
app, and connects their own Threads account. Nothing is shared between installations.

Publishing is designed for human-approved content: an agent can only publish a text or image that was explicitly
registered as approved, and a publication that may have reached Meta is never replayed automatically.

> Unofficial project. It is not affiliated with, endorsed by, or sponsored by Meta or Threads.

**Idioma:** the documentation is in English. A step-by-step installation guide in Spanish is in
[Guía de instalación (español)](#guía-de-instalación-español).

## Features

| Tool | Type | Description |
|---|---|---|
| `threads_get_profile` | read | Authenticated account profile (no credentials). |
| `threads_publish_text` | write | Publish a text post (up to 500 characters). Optional `topic_tag`, and an optional `image_url` that takes the image path. |
| `threads_publish_image` | write | Publish an image post from a public HTTPS URL, with optional text and `topic_tag`. |
| `threads_get_post` | read | Fetch one post by its provider id, for verification. |
| `threads_list_recent_posts` | read | List recent posts (1-100), for verification. |

Also included:

- **OAuth set-up from the command line** (`threads-mcp-auth`). OAuth helpers are not MCP tools, so agents cannot
  change credentials.
- **Encrypted token store** (Fernet). Tokens are never returned by any tool.
- **Publication ledger** (SQLite). Repeating a successful call with the same idempotency key returns the recorded post
  instead of creating a second one.
- **Unknown-outcome handling.** If Meta may have received a publish request, the result is `unknown` and the write is
  never replayed.
- **Approval gate** (`gate/`). Only texts registered in a local registry can be published through the gate.
- **Stdio supervisor** (`supervisor/`). Keeps the MCP host attached if the gate or the backend exits, without repeating
  in-flight publications.

## Requirements

- **Python 3.11 or newer** (3.11 and 3.13 are tested in CI). On macOS, the system `python3` may be older; install a
  newer Python with Homebrew, the python.org installer, or `uv`.
- **macOS or Linux.** The launchers are POSIX `sh` scripts and the supervisor uses POSIX `poll`. Windows is not
  supported by these components, although the Python package itself does not depend on them.
- A **Meta developer app** with the Threads API enabled, and a Threads account you control.
- An MCP client that can launch a stdio server (for example Claude Desktop, Claude Code, Cursor or another MCP host).

## Guía de instalación (español)

Esta guía explica cómo instalar el servidor, crear tu propia aplicación de Meta, conectar tu cuenta de Threads y
configurar la aprobación de publicaciones. Todos los comandos se ejecutan en una terminal de macOS o Linux.

### 1. Crear tu aplicación de Meta

1. Entra en el portal de desarrolladores de Meta y crea una aplicación con el caso de uso de la **API de Threads**.
2. En la configuración del producto Threads, añade una **URL de redirección** (`THREADS_REDIRECT_URI`). Debe ser una
   URL HTTPS que controles. No necesita servir ninguna página: copiarás el `code` y el `state` de la barra de
   direcciones después de autorizar.
3. Activa los permisos `threads_basic` y `threads_content_publish`.
4. Si la aplicación está en modo de desarrollo, añade tu cuenta de Threads como usuario de prueba. Las políticas y
   nombres de permisos de Meta cambian con el tiempo: sigue siempre su documentación vigente.
5. Anota el **ID de la aplicación** (`THREADS_APP_ID`) y el **secreto de la aplicación** (`THREADS_APP_SECRET`). El
   secreto nunca debe guardarse en el repositorio ni en un chat.

### 2. Descargar e instalar el servidor

```sh
git clone https://github.com/kerwilgil/threads-mcp-community.git
cd threads-mcp-community
python3.12 -m venv .venv          # cualquier Python 3.11 o superior (o: uv venv -p 3.12 .venv)
.venv/bin/pip install -e ".[dev]"
```

Instala desde el repositorio clonado, no solo desde pip: el wheel no incluye `gate/` ni `supervisor/`, que los lanzadores
usan directamente. El entorno virtual debe estar en `.venv` dentro de la carpeta del repositorio (o define `THREADS_MCP_VENV`). El
lanzador lo usa para arrancar el servidor. Si `python3` es anterior a 3.11, indica el intérprete correcto en
`THREADS_GATE_PYTHON` cuando configures el cliente MCP.

### 3. Crear la clave de cifrado local

```sh
mkdir -p ~/.config/threads-mcp && chmod 700 ~/.config/threads-mcp
.venv/bin/threads-mcp-auth keygen > ~/.config/threads-mcp/encryption.key
chmod 600 ~/.config/threads-mcp/encryption.key
```

Esta clave cifra el token de Threads. **Haz una copia de seguridad fuera del equipo** y no la compartas. Si la pierdes,
tendrás que volver a autorizar la cuenta; el registro de publicaciones no se pierde.

### 4. Autorizar tu cuenta de Threads

Define las variables de la aplicación (solo en esta sesión de terminal):

```sh
export THREADS_APP_ID="<ID de tu aplicación>"
export THREADS_REDIRECT_URI="<la misma URL de redirección de Meta>"
bin/threads-mcp-launcher auth auth-url
```

Abre la URL que aparece, autoriza la cuenta y copia los valores `code` y `state` de la URL a la que te redirige Meta.
El `state` caduca a los 15 minutos. Después completa el intercambio:

```sh
export THREADS_APP_SECRET="<secreto de tu aplicación>"
bin/threads-mcp-launcher auth exchange --code '<code>' --state '<state>'
bin/threads-mcp-launcher auth status
```

`auth status` muestra solo metadatos no secretos (nombre de usuario, caducidad). Cuando termines, borra las variables
sensibles (`unset THREADS_APP_SECRET`) y considera limpiar el historial del terminal si contiene el `code`.

### 5. Configurar el cliente MCP con aprobación

Configura tu cliente MCP para lanzar **el supervisor, que ejecuta el gate**, y no el lanzador directamente. Ejemplo con
la estructura habitual `mcpServers`:

```json
{
  "mcpServers": {
    "threads": {
      "command": "/ruta/absoluta/a/threads-mcp-community/bin/threads-mcp-supervised",
      "args": []
    }
  }
}
```

Reinicia el cliente MCP después de cambiar su configuración. Si Python 3.11+ no es el `python3` del sistema, añade
`"env": {"THREADS_GATE_PYTHON": "/ruta/a/python3.12"}`.

### 6. Aprobar publicaciones

Un agente **solo puede publicar textos registrados como aprobados**. Cuando una persona apruebe el texto exacto:

```sh
printf '%s' "<texto exacto aprobado>" > ./aprobado.txt
python3 gate/threads_publication_gate.py register \
    --key demo-2026-01-15-mi-publicacion-v1 --text-file ./aprobado.txt \
    --label "Mi publicación" --approved-by "yo, 2026-01-15"
rm -f ./aprobado.txt
python3 gate/threads_publication_gate.py list
```

El agente debe usar el mismo texto y el mismo `publication_ref`. Cualquier cambio, incluso un espacio, exige una nueva
aprobación con una nueva clave. Las imágenes y etiquetas se aprueban con `--image-url` y `--topic-tag`; consulta
[docs/images-and-topic-tags.md](docs/images-and-topic-tags.md).

### 7. Comprobar que funciona

Pide al cliente MCP que llame a `threads_get_profile` y a `threads_list_recent_posts`. Después, registra un texto de
prueba y publícalo **solo si lo has aprobado explícitamente**. Si un resultado es `unknown`, no vuelvas a publicar:
revisa primero la cuenta en la aplicación de Threads.

## Quick start (English)

```sh
git clone https://github.com/kerwilgil/threads-mcp-community.git && cd threads-mcp-community
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
mkdir -p ~/.config/threads-mcp && chmod 700 ~/.config/threads-mcp
.venv/bin/threads-mcp-auth keygen > ~/.config/threads-mcp/encryption.key && chmod 600 ~/.config/threads-mcp/encryption.key
# Then authorize the account (section "Connect a Threads account") and point your MCP host at bin/threads-mcp-supervised.
```

## Connect a Threads account

The OAuth flow is the same as the Spanish guide above:

1. Create a Meta app with the Threads API, a redirect URI and the scopes `threads_basic` and `threads_content_publish`.
2. Set `THREADS_APP_ID` and `THREADS_REDIRECT_URI`, then run `bin/threads-mcp-launcher auth auth-url` and open the URL.
3. Copy `code` and `state` from the redirect address (the state expires after 15 minutes).
4. Set `THREADS_APP_SECRET` and run `bin/threads-mcp-launcher auth exchange --code '<code>' --state '<state>'`.
5. Verify with `bin/threads-mcp-launcher auth status`.

The OAuth commands are CLI-only and are never exposed as MCP tools.

## Using the tools

Read examples (no approval needed):

```json
{"name": "threads_get_profile", "arguments": {}}
{"name": "threads_list_recent_posts", "arguments": {"limit": 5}}
{"name": "threads_get_post", "arguments": {"post_id": "<post id>"}}
```

Publishing through the approval gate (the host shows `publication_ref` instead of `idempotency_key`):

```json
{"name": "threads_publish_text",
 "arguments": {"text": "<exact approved text>", "publication_ref": "demo-2026-01-15-launch-post-v1"}}
```

```json
{"name": "threads_publish_image",
 "arguments": {"text": "<exact approved text>", "image_url": "https://example.com/images/demo.jpg",
               "publication_ref": "demo-2026-02-01-image-post-v1", "topic_tag": "ExampleTopic"}}
```

A publish result has one of three statuses:

| Status | Meaning | What to do |
|---|---|---|
| `published` | Meta returned a post id. The result includes `post_id` and, when available, `permalink`. | Nothing. Repeating the call returns the same post. |
| `failed` | Nothing was published. Either the failure was definitive, or it happened before any public write. | Read the message. Retrying with the same key is safe: the ledger either makes a new attempt or returns the same failure. |
| `unknown` | Meta may have published the post. | **Do not retry.** Check the account in the Threads app or with `threads_list_recent_posts`. |

Without the gate (`bin/threads-mcp-launcher` directly), `threads_publish_text` takes an `idempotency_key` and no approval
check is performed. Do not expose that configuration to an agent.

## Configuration

Copy `.env.example` only for local experiments; the launchers do not read `.env`. The variables are:

| Variable | Required | Purpose |
|---|---|---|
| `TOKEN_ENCRYPTION_KEY` | yes (set by the launcher) | Fernet key for the token store. Read from `~/.config/threads-mcp/encryption.key` by the launcher. |
| `THREADS_APP_ID`, `THREADS_REDIRECT_URI` | OAuth only | Meta app and redirect URI. |
| `THREADS_APP_SECRET` | OAuth exchange only | Meta app secret. Never store it in a file under version control. |
| `THREADS_API_BASE` | no | Must be `https://graph.threads.net/vX.Y`. Default `https://graph.threads.net/v1.0`. |
| `THREADS_REQUEST_TIMEOUT` | no | Seconds per request. Default `15`. |
| `THREADS_TOKEN_STORE`, `THREADS_LEDGER_PATH`, `THREADS_OAUTH_STATE_PATH` | no | Overrides. The launcher sets them from the XDG directories. |
| `THREADS_GATE_REGISTRY`, `THREADS_GATE_PYTHON` | no | Gate registry path and Python interpreter for the wrapper scripts. |
| `THREADS_MCP_RUNTIME`, `THREADS_MCP_VENV` | no | Launcher layout overrides. |

Local files (default XDG locations):

| File | Purpose |
|---|---|
| `~/.config/threads-mcp/encryption.key` | Encryption key (mode `600`). |
| `~/.config/threads-mcp/token.enc` | Encrypted Threads token. |
| `~/.config/threads-mcp/approved-publications.json` | Approval registry (mode `600`). |
| `~/.local/state/threads-mcp/publications.sqlite3` | Publication ledger. Keep it; it prevents duplicates. |
| `~/.local/state/threads-mcp/gate-verifications.ndjson` | Verification log written by the gate. |

## Security

- Tokens are encrypted at rest and are never returned by tools, logged, or placed in error messages.
- The encryption key and the token store are separate files. Anyone who can read both can use the token: protect them
  with operating-system permissions and back up the key securely.
- Provider response bodies are not returned to the model. Errors carry only fixed messages and shape-checked diagnostics.
- Each installation has its own key, token, ledger and registry. Nothing is shared with other people or with this
  repository.
- The gate fails closed: unregistered text, a changed byte, a wrong reference, or a missing registry blocks publication.
- Publication uses only the official HTTPS Graph API. Browser automation is not used.

See [SECURITY.md](SECURITY.md) for how to report a vulnerability and [docs/architecture.md](docs/architecture.md) for the
full publication model.

## Limitations

- Text posts are limited to 500 characters. Image posts need a public HTTPS URL that Meta can fetch.
- Image processing is asynchronous. If the container is not `FINISHED` after about 45 seconds, the result is a retryable
  `failed` with the same reference.
- Not implemented in this release: replies, moderation, insights, video, carousels, and deleting posts.
- Access tokens expire. Long-lived tokens are refreshed automatically when they are within 3 days of expiry; if a token
  has already expired, run the OAuth flow again.
- Meta's rate limits and app-review rules apply. A rate-limit response on a publish request produces `unknown`.
- A recent feed match of the same text does not prove that a specific attempt published.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `missing executable .../.venv/bin/threads-mcp` | Install the package into `.venv` (step 2), or set `THREADS_MCP_VENV`. |
| `ModuleNotFoundError: threads_mcp` | The virtualenv lost the package. Run `.venv/bin/pip install -e .` again. |
| `missing .../encryption.key` | Create the key with `threads-mcp-auth keygen` (step 3). |
| `Threads tool failed. Check local authentication, input, and publication state.` | Usually no token or an expired token: complete the OAuth flow, then run `auth status`. |
| `The stored Threads credential cannot be decrypted` | The key file does not match the token store. Restore the original key or authorize again. |
| `approved-publications registry not found` | Register the approved text first (step 6). |
| `This exact text is not in the approved-publications registry` | The text differs from the registered text by at least one byte. Re-register the exact text with a new key. |
| `publication_ref does not match` | Use the key shown by `list`, unchanged. |
| `… takes exactly … (no idempotency_key)` | The host is still calling with the old schema. Restart the MCP host so it reloads the tool list. |
| `ImportError: cannot import name 'UTC'` from the gate | The `python3` on your PATH is older than 3.11. Set `THREADS_GATE_PYTHON` to a 3.11+ interpreter. |
| Host shows `MCP server exited` | Restart the host. Check that `bin/threads-mcp-supervised` is executable and that its path is absolute. |
| Result `unknown` | Do not retry. Check the account in the Threads app first. |

## Updating

```sh
cd threads-mcp-community
git pull
.venv/bin/pip install -e ".[dev]"
```

Then restart the MCP host. Keep the ledger and the registry when updating. Stop any older server process before you
upgrade, because older versions do not share the owner checks of newer ones.

## Development

```sh
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest
.venv/bin/python -m compileall -q src tests gate supervisor
```

The test suite uses synthetic credentials and texts, blocks live HTTP, and never publishes. Tests cover the tool
surface over stdio, OAuth against a mocked provider, encryption, duplicate prevention, concurrency, provider errors,
image publication, the gate, and the supervisor. Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Project layout

```text
src/threads_mcp/        MCP server, OAuth, token store, ledger, Graph client
bin/                    stdio launcher and supervised entry point
gate/                   approval gate (registry, verification, tests with a mock backend)
supervisor/             stdio supervisor that restarts children without replaying work
docs/                   architecture, supervisor and image/topic-tag documentation
tests/                  unit, integration and stdio tests
```

## License

[MIT](LICENSE). Copyright (c) 2026 kerwilgil.
