# Stdio supervisor

## Why it exists

An MCP host launches one stdio process. If that process exits, many hosts report `MCP server exited` and do not start
it again. In the recommended setup the process in front of the backend is the publication gate, so a `SIGTERM` to the
gate or to the backend would end the host session until the host is restarted.

`supervisor/threads_stdio_supervisor.py` is the process the host should launch. It uses only the Python 3.11+ standard
library and POSIX `poll`, so it runs on macOS and Linux. It reads and writes newline-delimited JSON-RPC on stdio and
stays alive for as long as the host keeps stdin open. When the child exits for any reason, the supervisor answers the
client requests that were in flight, waits with exponential backoff, and starts the child again.

Backoff starts at 0.5 s and doubles, capped by `--max-backoff` (default 30 s). After the child has been healthy for
60 s, the next wait starts again at 0.5 s. `--ready-timeout` (default 20 s) is how long a request may wait in the queue
while the child is down or initializing.

## What a restart does to a call

In-flight client requests are **never sent again**. Each one is answered immediately:

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32002,"message":"threads-mcp backend restarted; request was NOT retried","data":{"method":"tools/call","tool":"threads_publish_text","retried":false,"hint":"Check the publication ledger before calling a threads_publish tool again. This request was not retried and the post may already have been published."}}}
```

Other tools omit `hint`. `retried` is always `false`. Requests that arrive while the child is down are queued and get the
same error if the child is not ready within `--ready-timeout`. `notifications/initialized` is remembered and sent to the
new child after an internal `initialize`. Other notifications are dropped, except `notifications/cancelled`, which drops
a request that is still queued.

The supervisor logs restart events to stderr as one JSON object per line. It never logs tool arguments, post text or
tokens. Child stderr is copied through with a `[backend] ` prefix. Lines from the child that are not JSON-RPC go to
stderr, never to stdout.

After a restart the supervisor replays the cached `initialize` with an id prefixed `supervisor-`, swallows that
response, and then sends `notifications/initialized`. Only then does it forward new client traffic. If the replay fails
or times out, the child is treated as failed and the supervisor backs off.

## The gate stays the publication authority

The supervisor does not read the registry, the ledger or tokens, and it does not rewrite `tools/list` or `tools/call`.
`publication_ref`, idempotency keys and publish decisions stay in `gate/threads_publication_gate.py` and `threads-mcp`.
A restart is never a second publish. If the `-32002` error names a `threads_publish` tool, check the publication ledger
before calling the tool again.

## Recommended host command

Point the MCP host at the wrapper in this repository:

```text
/absolute/path/to/threads-mcp/bin/threads-mcp-supervised
```

The wrapper execs:

```sh
python3 supervisor/threads_stdio_supervisor.py -- \
    python3 gate/threads_publication_gate.py proxy -- \
    bin/threads-mcp-launcher
```

To use another backend, keep the supervisor in front of the same gate command and replace only the final backend
command after `--`. Restart the MCP host after changing its command so it attaches to the new process.

## Shutdown

On stdin EOF the supervisor closes the child's stdin, waits up to 5 s, then sends SIGTERM, then SIGKILL, and exits 0.
On SIGTERM or SIGINT sent to the supervisor itself, it forwards the signal to the child and exits.

## Limitation

If the supervisor process itself is killed, the host's stdio client will report the server as exited and will not
reconnect on its own. The supervisor can only hide exits of the gate and the backend behind it. Do not send signals to
the running stdio processes while a host session is attached; restart the host instead.
