"""Stdio supervisor: the MCP host stays attached across backend exits.

The fake child speaks a minimal MCP session, records every tools/call, and can
crash or hang when a tool argument asks it to. Nothing here talks to Threads.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SUPERVISOR = REPO / "supervisor" / "threads_stdio_supervisor.py"
WRAPPER = REPO / "bin" / "threads-mcp-supervised"
GATE = REPO / "gate" / "threads_publication_gate.py"
MOCK = REPO / "gate" / "tests" / "mock_backend.py"

ERROR_MESSAGE = "threads-mcp backend restarted; request was NOT retried"
CANARY_POST = "CANARY_POST_TEXT_9f3a"
CANARY_TOKEN = "CANARY_TOKEN_9f3a"

FAKE_CHILD = r"""
import json
import os
import signal
import sys
import time
from pathlib import Path

counter_path = Path(os.environ["COUNTER"])
boot_path = Path(os.environ["BOOT"])
boot_n = int(boot_path.read_text()) if boot_path.exists() else 0
boot_path.write_text(str(boot_n + 1))
pidfile = os.environ.get("PIDFILE")
if pidfile:
    Path(pidfile).write_text(str(os.getpid()))


def record(obj):
    with counter_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if os.environ.get("STUBBORN") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        time.sleep(30)

if os.environ.get("GARBAGE") == "1":
    sys.stdout.write("not-json-stdout-line\n")
    sys.stdout.flush()

sys.stderr.write("backend-stderr-marker\n")
sys.stderr.flush()

if boot_n >= 1 and os.environ.get("BLOCK_READY") == "1":
    for _line in sys.stdin:
        pass
    raise SystemExit(0)

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        record({"method": "initialize", "id": msg.get("id")})
        if str(msg.get("id", "")).startswith("supervisor-") and os.environ.get("FAIL_REPLAY") == "1":
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "replay refused"}})
            continue
        send(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"},
                },
            }
        )
        continue
    if method == "notifications/initialized":
        record({"method": method})
        continue
    if method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments") or {}
        record({"method": "tools/call", "name": name, "id": msg.get("id")})
        action = args.get("action")
        if action == "crash":
            sys.stdout.flush()
            os._exit(1)
        if action == "hang":
            time.sleep(3600)
            continue
        if action == "garbage":
            sys.stdout.write("not-json-stdout-line\n")
            sys.stdout.flush()
        if action == "ask":
            send({"jsonrpc": "2.0", "id": "from-server", "method": "sampling/createMessage", "params": {}})
            reply = json.loads(sys.stdin.readline())
            record({"method": "client-response", "id": reply.get("id")})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True, "tool": name}})
        continue
    if method:
        record({"method": method, "id": msg.get("id")})
        if "id" in msg:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
"""


def load_supervisor():
    spec = importlib.util.spec_from_file_location("threads_stdio_supervisor", SUPERVISOR)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SUPERVISOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trace(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tool_calls(path: Path, name: str | None = None) -> list[dict]:
    rows = [row for row in trace(path) if row.get("method") == "tools/call"]
    if name is None:
        return rows
    return [row for row in rows if row.get("name") == name]


class FastClock:
    """Test clock: finite waits elapse immediately and are recorded."""

    def __init__(self) -> None:
        self._t = 0.0
        self._lock = threading.Lock()
        self.waits: list[float] = []

    def monotonic(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds

    def wait(self, delay: float | None, event: threading.Event) -> None:
        if delay is None:
            event.wait()
            return
        if event.wait(0.02):
            return
        with self._lock:
            self.waits.append(delay)
            self._t += delay

    def snapshot(self) -> list[float]:
        with self._lock:
            return list(self.waits)


class Running:
    def __init__(self, proc: subprocess.Popen[bytes], pidfile: Path, counter: Path) -> None:
        self.proc = proc
        self.pidfile = pidfile
        self.counter = counter
        self.raw_stdout: list[str] = []
        self.raw_stderr: list[str] = []
        self.messages: list[dict] = []
        self._stdout: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._read_stdout, name="test-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="test-stderr", daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode()
            with self._lock:
                self.raw_stdout.append(line)
            self._stdout.put(line)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            line = raw.decode()
            with self._lock:
                self.raw_stderr.append(line)

    def send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def send_raw(self, payload: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def close_stdin(self) -> None:
        if self._closed or self.proc.stdin is None:
            return
        self._closed = True
        self.proc.stdin.close()

    def next_message(self, timeout: float = 5) -> dict:
        line = self._stdout.get(timeout=timeout)
        msg = json.loads(line)
        self.messages.append(msg)
        return msg

    def wait_id(self, msg_id: object, timeout: float = 5) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no response for id {msg_id!r}; saw {self.messages!r}")
            msg = self.next_message(timeout=remaining)
            if msg.get("id") == msg_id and "method" not in msg:
                return msg

    def stderr_text(self) -> str:
        with self._lock:
            return "".join(self.raw_stderr)

    def stdout_text(self) -> str:
        with self._lock:
            return "".join(self.raw_stdout)

    def wait_event(self, predicate, timeout: float = 5) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.stderr_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and predicate(event):
                    return event
            time.sleep(0.02)
        raise AssertionError(self.stderr_text())

    def wait_pid(self, timeout: float = 5) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pidfile.exists() and self.pidfile.read_text().strip():
                return int(self.pidfile.read_text())
            time.sleep(0.02)
        raise AssertionError("child pid was not written")

    def wait_exit(self, timeout: float = 5) -> int:
        return self.proc.wait(timeout=timeout)

    def shutdown(self) -> None:
        self.close_stdin()
        try:
            self.proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)
        if self.pidfile.exists() and self.pidfile.read_text().strip():
            pid = int(self.pidfile.read_text())
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class Session:
    def __init__(
        self,
        tmp_path: Path,
        extra_env: dict[str, str] | None = None,
        supervisor_args: list[str] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.counter = tmp_path / "counter.jsonl"
        self.boot = tmp_path / "boot"
        self.pidfile = tmp_path / "child.pid"
        child = tmp_path / "fake_child.py"
        child.write_text(FAKE_CHILD, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "HOME": str(tmp_path),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "COUNTER": str(self.counter),
            "BOOT": str(self.boot),
            "PIDFILE": str(self.pidfile),
        }
        if extra_env:
            env.update(extra_env)
        cmd = [sys.executable, str(SUPERVISOR), *(supervisor_args or []), "--", sys.executable, str(child)]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        self.running = Running(self.proc, self.pidfile, self.counter)

    def __enter__(self) -> Running:
        return self.running

    def __exit__(self, *exc: object) -> None:
        self.running.shutdown()


def handshake(running: Running, *, list_changed: bool = False, client_name: str = "tester") -> dict:
    caps = {"tools": {"listChanged": True}} if list_changed else {"tools": {}}
    running.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": caps,
                "clientInfo": {"name": client_name, "version": "0"},
            },
        }
    )
    reply = running.wait_id(1)
    assert "result" in reply
    running.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return reply


def call(name: str, msg_id: object, **arguments: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def assert_restart_error(msg: dict, *, method: str, tool: str | None) -> None:
    error = msg["error"]
    assert error["code"] == -32002
    assert error["message"] == ERROR_MESSAGE
    data = error["data"]
    assert data["retried"] is False
    assert data["method"] == method
    assert data["tool"] == tool
    if tool == "threads_publish_text":
        assert "ledger" in data["hint"].lower()
    else:
        assert "hint" not in data


def drain_quiet(running: Running, quiet: float = 0.3) -> None:
    """Read stdout until it has been idle for `quiet` seconds."""
    deadline = time.monotonic() + quiet
    while True:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            return
        try:
            running.next_message(timeout=timeout)
        except queue.Empty:
            return
        deadline = time.monotonic() + quiet


def assert_no_secrets(running: Running) -> None:
    combined = running.stdout_text() + running.stderr_text()
    assert CANARY_POST not in combined
    assert CANARY_TOKEN not in combined
    assert "supervisor-" not in running.stdout_text()


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def test_supervisor_imports_are_stdlib_and_system_python_compiles() -> None:
    tree = ast.parse(SUPERVISOR.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".", 1)[0])
    assert modules <= sys.stdlib_module_names
    compiled = subprocess.run(
        ["/usr/bin/python3", "-m", "py_compile", str(SUPERVISOR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr


def test_cli_requires_child_command_and_keeps_stdout_empty() -> None:
    assert SUPERVISOR.is_file()
    proc = subprocess.run(
        [sys.executable, str(SUPERVISOR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "child" in proc.stderr.lower()


def test_parse_args_stops_at_double_dash() -> None:
    module = load_supervisor()
    max_backoff, ready_timeout, cmd = module.parse_args(
        ["--max-backoff", "2", "--ready-timeout", "3.5", "--", "python3", "child.py", "--flag"]
    )
    assert (max_backoff, ready_timeout) == (2.0, 3.5)
    assert cmd == ["python3", "child.py", "--flag"]


def test_normal_passthrough(tmp_path: Path) -> None:
    with Session(tmp_path) as running:
        handshake(running)
        running.send(call("threads_get_profile", 2))
        reply = running.wait_id(2)
        assert reply["result"] == {"ok": True, "tool": "threads_get_profile"}
        assert tool_calls(running.counter, "threads_get_profile")[0]["id"] == 2
        deadline = time.monotonic() + 2
        while "backend-stderr-marker" not in running.stderr_text() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "[backend] backend-stderr-marker" in running.stderr_text()
        pid = running.wait_pid()
        running.close_stdin()
        assert running.wait_exit() == 0
        assert _dead(pid)


def test_crash_inflight_returns_32002_and_supervisor_stays_up(tmp_path: Path) -> None:
    with Session(tmp_path) as running:
        handshake(running, client_name=CANARY_TOKEN)
        running.send(call("threads_publish_text", 7, text=CANARY_POST, action="crash"))
        reply = running.wait_id(7)
        assert_restart_error(reply, method="tools/call", tool="threads_publish_text")
        assert running.proc.poll() is None
        assert_no_secrets(running)
        assert len(tool_calls(running.counter, "threads_publish_text")) == 1


def test_relaunch_hides_initialize_and_later_call_succeeds(tmp_path: Path) -> None:
    with Session(tmp_path) as running:
        handshake(running)
        running.send(call("threads_publish_text", 4, text=CANARY_POST, action="crash"))
        crashed = running.wait_id(4)
        assert crashed["error"]["code"] == -32002
        # Reuse the id the crash already failed. A restart must still answer it.
        running.send(call("threads_get_profile", 4))
        reply = running.wait_id(4, timeout=5)
        assert reply["result"]["ok"] is True
        assert running.proc.poll() is None
        initializes = [row for row in trace(running.counter) if row.get("method") == "initialize"]
        assert any(str(row["id"]).startswith("supervisor-") for row in initializes)
        assert_no_secrets(running)
        assert not any(msg.get("method") == "notifications/tools/list_changed" for msg in running.messages)


def test_publish_in_flight_is_executed_exactly_once(tmp_path: Path) -> None:
    with Session(tmp_path) as running:
        handshake(running)
        running.send(call("threads_publish_text", 3, text=CANARY_POST, token=CANARY_TOKEN, action="crash"))
        failed = running.wait_id(3)
        assert_restart_error(failed, method="tools/call", tool="threads_publish_text")
        running.send(call("threads_get_profile", 8))
        assert running.wait_id(8, timeout=5)["result"]["ok"] is True
        time.sleep(0.3)
        assert len(tool_calls(running.counter, "threads_publish_text")) == 1
        assert_no_secrets(running)


def test_queued_request_times_out_while_child_is_not_ready(tmp_path: Path) -> None:
    with Session(tmp_path, extra_env={"BLOCK_READY": "1"}, supervisor_args=["--ready-timeout", "0.6"]) as running:
        handshake(running)
        running.send(call("crash", 2, action="crash"))
        assert running.wait_id(2)["error"]["code"] == -32002
        running.send(call("threads_get_profile", 9))
        reply = running.wait_id(9, timeout=3)
        assert_restart_error(reply, method="tools/call", tool="threads_get_profile")
        assert running.proc.poll() is None
        assert tool_calls(running.counter, "threads_get_profile") == []


def test_cancelled_queued_request_is_not_answered_or_forwarded(tmp_path: Path) -> None:
    with Session(tmp_path, extra_env={"BLOCK_READY": "1"}, supervisor_args=["--ready-timeout", "0.8"]) as running:
        handshake(running)
        running.send(call("crash", 2, action="crash"))
        assert running.wait_id(2)["error"]["code"] == -32002
        running.send_raw(
            json.dumps(call("threads_get_profile", 11)).encode()
            + b"\n"
            + json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 11}}
            ).encode()
            + b"\n"
        )
        with pytest.raises(queue.Empty):
            running._stdout.get(timeout=1.0)
        assert tool_calls(running.counter, "threads_get_profile") == []
        assert running.proc.poll() is None


def test_non_json_child_stdout_never_reaches_stdout(tmp_path: Path) -> None:
    with Session(tmp_path, extra_env={"GARBAGE": "1"}) as running:
        handshake(running)
        running.send(call("threads_get_profile", 2, action="garbage"))
        reply = running.wait_id(2)
        assert reply["result"]["ok"] is True
        deadline = time.monotonic() + 2
        while "not-json-stdout-line" not in running.stderr_text() and time.monotonic() < deadline:
            time.sleep(0.02)
        stdout = running.stdout_text()
        assert "not-json-stdout-line" not in stdout
        for line in stdout.splitlines():
            json.loads(line)
        assert "not-json-stdout-line" in running.stderr_text()


def test_server_request_and_client_response_pass_through(tmp_path: Path) -> None:
    with Session(tmp_path) as running:
        handshake(running)
        running.send(call("threads_get_profile", 5, action="ask"))
        server_req = running.next_message()
        assert server_req["method"] == "sampling/createMessage"
        assert server_req["id"] == "from-server"
        running.send({"jsonrpc": "2.0", "id": "from-server", "result": {"model": "fake"}})
        reply = running.wait_id(5)
        assert reply["result"]["ok"] is True
        assert any(row.get("method") == "client-response" for row in trace(running.counter))


def test_list_changed_is_emitted_only_when_the_client_advertised_it(tmp_path: Path) -> None:
    with Session(tmp_path) as running:
        handshake(running, list_changed=True)
        running.send(call("crash", 2, action="crash"))
        assert running.wait_id(2)["error"]["code"] == -32002
        running.send(call("threads_get_profile", 6))
        assert running.wait_id(6, timeout=5)["result"]["ok"] is True
        drain_quiet(running)
        notes = [msg for msg in running.messages if msg.get("method") == "notifications/tools/list_changed"]
        assert len(notes) == 1
        assert "id" not in notes[0]


def test_replayed_initialize_error_backs_off(tmp_path: Path) -> None:
    with Session(tmp_path, extra_env={"FAIL_REPLAY": "1"}, supervisor_args=["--max-backoff", "1"]) as running:
        handshake(running)
        running.send(call("crash", 2, action="crash"))
        assert running.wait_id(2)["error"]["code"] == -32002
        event = running.wait_event(lambda item: item.get("event") == "initialize_replay_failed", timeout=4)
        assert event["reason"] == "error"
        assert running.proc.poll() is None


def test_backoff_grows_and_caps_with_fast_clock(tmp_path: Path) -> None:
    module = load_supervisor()
    child = tmp_path / "exit.py"
    child.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    clock = FastClock()
    stdin_r, stdin_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    supervisor = module.Supervisor(
        [sys.executable, str(child)],
        max_backoff=2,
        ready_timeout=20,
        stdin=os.fdopen(stdin_r, "rb"),
        stdout=os.fdopen(out_w, "wb"),
        stderr=os.fdopen(err_w, "wb"),
        clock=clock,
    )
    thread = threading.Thread(target=supervisor.run, name="supervisor-backoff", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and clock.snapshot()[:4] != [0.5, 1.0, 2.0, 2.0]:
            time.sleep(0.02)
        assert clock.snapshot()[:4] == [0.5, 1.0, 2.0, 2.0]
    finally:
        os.close(stdin_w)
        thread.join(timeout=3)
        os.close(out_r)
        os.close(err_r)
    assert not thread.is_alive()


def test_backoff_resets_after_healthy_period(tmp_path: Path) -> None:
    module = load_supervisor()
    child = tmp_path / "sleep.py"
    child.write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
    clock = FastClock()
    stdin_r, stdin_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    err_lines: list[str] = []

    def read_err() -> None:
        for raw in os.fdopen(err_r, "rb"):
            err_lines.append(raw.decode())

    threading.Thread(target=read_err, daemon=True).start()
    supervisor = module.Supervisor(
        [sys.executable, str(child)],
        max_backoff=30,
        ready_timeout=20,
        stdin=os.fdopen(stdin_r, "rb"),
        stdout=os.fdopen(out_w, "wb"),
        stderr=os.fdopen(err_w, "wb"),
        clock=clock,
    )
    thread = threading.Thread(target=supervisor.run, name="supervisor-reset", daemon=True)
    thread.start()

    def exits() -> list[dict]:
        found = []
        for line in err_lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "backend_exit":
                found.append(event)
        return found

    def pid_after(index: int) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            starts = []
            for line in list(err_lines):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "backend_start":
                    starts.append(event["pid"])
            if len(starts) > index:
                return int(starts[index])
            time.sleep(0.02)
        raise AssertionError(err_lines)

    try:
        os.kill(pid_after(0), signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(exits()) < 1:
            time.sleep(0.02)
        assert exits()[0]["backoff_s"] == 0.5
        second = pid_after(1)
        clock.advance(61)
        os.kill(second, signal.SIGKILL)
        while time.monotonic() < deadline and len(exits()) < 2:
            time.sleep(0.02)
        assert exits()[1]["backoff_s"] == 0.5
        os.kill(pid_after(2), signal.SIGKILL)
        while time.monotonic() < deadline and len(exits()) < 3:
            time.sleep(0.02)
        assert exits()[2]["backoff_s"] == 1.0
    finally:
        os.close(stdin_w)
        thread.join(timeout=3)
        os.close(out_r)


def test_stdin_eof_shuts_down_cleanly_and_reaps_child(tmp_path: Path) -> None:
    with Session(tmp_path) as running:
        pid = running.wait_pid()
        running.close_stdin()
        assert running.wait_exit(timeout=3) == 0
        assert _dead(pid)


def test_stdin_eof_kills_child_that_ignores_term(tmp_path: Path) -> None:
    with Session(tmp_path, extra_env={"STUBBORN": "1"}) as running:
        pid = running.wait_pid()
        running.close_stdin()
        assert running.wait_exit(timeout=10) == 0
        assert _dead(pid)


def test_sigterm_is_forwarded_and_supervisor_exits(tmp_path: Path) -> None:
    with Session(tmp_path) as running:
        pid = running.wait_pid()
        running.proc.send_signal(signal.SIGTERM)
        assert running.wait_exit(timeout=4) == 128 + signal.SIGTERM
        assert _dead(pid)


def test_gate_proxy_tools_list_still_exposes_publication_ref(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "THREADS_GATE_RECORD": str(record),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SUPERVISOR),
            "--ready-timeout",
            "8",
            "--",
            sys.executable,
            str(GATE),
            "proxy",
            "--",
            sys.executable,
            str(MOCK),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    running = Running(proc, tmp_path / "missing.pid", record)
    try:
        handshake(running)
        running.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        reply = running.wait_id(2, timeout=8)
        tools = {tool["name"]: tool for tool in reply["result"]["tools"]}
        schema = tools["threads_publish_text"]["inputSchema"]
        assert "publication_ref" in schema["properties"]
        assert schema["required"] == ["text", "publication_ref"]
        assert "idempotency_key" not in schema["properties"]
        assert sorted(tools) == [
            "threads_get_post",
            "threads_get_profile",
            "threads_list_recent_posts",
            "threads_publish_text",
        ]
    finally:
        running.shutdown()


def test_wrapper_execs_supervisor_in_front_of_gate_launcher(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$STUB_OUT"\n', encoding="utf-8")
    stub.chmod(0o755)
    out = tmp_path / "args"
    proc = subprocess.run(
        ["sh", str(WRAPPER), "forwarded"],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "STUB_OUT": str(out),
            "HOME": str(tmp_path),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(SUPERVISOR)
    assert lines[1:] == [
        "--",
        "python3",
        str(GATE),
        "proxy",
        "--",
        str(REPO / "bin" / "threads-mcp-launcher"),
        "forwarded",
    ]
    syntax = subprocess.run(["sh", "-n", str(WRAPPER)], check=False)
    assert syntax.returncode == 0
    text = WRAPPER.read_text(encoding="utf-8")
    assert "set -eu" in text
    assert "exec " in text
