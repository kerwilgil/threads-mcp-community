#!/usr/bin/env python3
"""Long-lived stdio supervisor in front of the threads-mcp publication chain.

The MCP host treats any exit of its stdio child as a permanent failure. This
process stays up while that stdin is open and restarts the child command
(publication gate, then backend) without replaying client work. In-flight
requests are failed with JSON-RPC -32002 and retried=false. Tool arguments,
post text, and tokens are never written to the supervisor log.

Python 3.11+. Standard library only.
"""

from __future__ import annotations

import argparse
import copy
import enum
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO

INITIAL_BACKOFF = 0.5
HEALTHY_SECONDS = 60.0
SHUTDOWN_WAIT = 5.0
TERM_GRACE = 0.5
ERROR_CODE = -32002
ERROR_MESSAGE = "threads-mcp backend restarted; request was NOT retried"
PUBLISH_TOOL = "threads_publish_text"
PUBLISH_TOOLS = frozenset({PUBLISH_TOOL, "threads_publish_image"})
PUBLISH_HINT = (
    "Check the publication ledger before calling a threads_publish tool again. "
    "This request was not retried and the post may already have been published."
)
INITIALIZED_LINE = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
LIST_CHANGED_LINE = b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n'


class Phase(enum.Enum):
    DOWN = "down"
    STARTING = "starting"
    REINIT = "reinit"
    READY = "ready"
    STOPPING = "stopping"


class WallClock:
    """Real-time clock. `wait` returns early when `event` is set."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wait(self, delay: float | None, event: threading.Event) -> None:
        if delay is None:
            event.wait()
        else:
            event.wait(timeout=delay)


def parse_args(argv: list[str]) -> tuple[float, float, list[str]]:
    """Split `[options] -- child...` into backoff, ready timeout, and the child command."""
    if "--" not in argv:
        raise SystemExit(
            "usage: threads_stdio_supervisor.py [--max-backoff SECONDS] "
            "[--ready-timeout SECONDS] -- <child command...>"
        )
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1 :]
    if not command:
        raise SystemExit("child command is required after --")
    parser = argparse.ArgumentParser(prog="threads_stdio_supervisor.py")
    parser.add_argument("--max-backoff", type=float, default=30.0)
    parser.add_argument("--ready-timeout", type=float, default=20.0)
    parsed = parser.parse_args(options)
    if parsed.max_backoff < 0 or parsed.ready_timeout < 0:
        parser.error("timeouts must be non-negative")
    return parsed.max_backoff, parsed.ready_timeout, command


def _canon(msg_id: object) -> str:
    return json.dumps(msg_id, sort_keys=True, separators=(",", ":"))


def _wants_list_changed(params: dict) -> bool:
    capabilities = params.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    tools = capabilities.get("tools")
    if not isinstance(tools, dict):
        return False
    return tools.get("listChanged") is True


def _is_jsonrpc(msg: object) -> bool:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return False
    if "method" in msg:
        return isinstance(msg["method"], str)
    return "id" in msg and ("result" in msg or "error" in msg)


def _loads(raw: bytes) -> object | None:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _normalize(raw: bytes) -> bytes:
    return raw if raw.endswith(b"\n") else raw + b"\n"


def _tool_name(method: str, msg: dict) -> str | None:
    if method != "tools/call":
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return name if isinstance(name, str) else None


def _error_line(item: dict) -> bytes:
    data: dict[str, object] = {"method": item["method"], "tool": item["tool"], "retried": False}
    if item["tool"] in PUBLISH_TOOLS:
        data["hint"] = PUBLISH_HINT
    payload = {
        "jsonrpc": "2.0",
        "id": item["id"],
        "error": {"code": ERROR_CODE, "message": ERROR_MESSAGE, "data": data},
    }
    return json.dumps(payload, ensure_ascii=False).encode() + b"\n"


def _wait_proc(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


class Supervisor:
    """Restart `command` until stdin closes. Never resend a client request."""

    def __init__(
        self,
        command: list[str],
        *,
        max_backoff: float = 30.0,
        ready_timeout: float = 20.0,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        stderr: BinaryIO | None = None,
        clock: WallClock | None = None,
    ) -> None:
        if not command:
            raise SystemExit("child command is required after --")
        self.command = command
        self.max_backoff = max_backoff
        self.ready_timeout = ready_timeout
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.stdout = stdout if stdout is not None else sys.stdout.buffer
        self.stderr = stderr if stderr is not None else sys.stderr.buffer
        self.clock = clock if clock is not None else WallClock()
        self.phase = Phase.DOWN
        self.next_backoff = min(INITIAL_BACKOFF, max_backoff)
        self.relaunch_at = self.clock.monotonic()
        self.generation = 0
        self.accounted_generation = 0
        self.internal_seq = 0
        self.queue: list[dict] = []
        self.inflight: dict[str, dict] = {}
        self.failed_ids: set[str] = set()
        self.cached_params: dict | None = None
        self.client_initialized = False
        self.handshake_complete = False
        self.tools_list_changed = False
        self.pending_internal_id: str | None = None
        self.replay_status: str | None = None
        self.reinit_deadline = float("inf")
        self.healthy_since: float | None = None
        self.child: subprocess.Popen[bytes] | None = None
        self.eof = False
        self.signal: int | None = None
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.stdout_lock = threading.Lock()
        self.stderr_lock = threading.Lock()
        self.child_write_lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        self._stdin_stop = False
        self._workers: list[threading.Thread] = []

    def run(self) -> int:
        try:
            return self._run()
        finally:
            self._stdin_stop = True
            self._poke()
            proc = self.child
            if proc is not None and proc.poll() is None:
                self._signal_tree(proc, signal.SIGKILL)
                _wait_proc(proc, 2)
            self._join_workers()
            for fd in (self._wake_r, self._wake_w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _run(self) -> int:
        self._install_signals()
        stdin_thread = threading.Thread(target=self._stdin_loop, name="supervisor-stdin", daemon=True)
        self._workers.append(stdin_thread)
        stdin_thread.start()
        while True:
            mode, payload, delay = self._step()
            if mode == "signal":
                return self._shutdown_signal(payload)
            if mode == "eof":
                return self._shutdown_eof()
            if mode == "actions":
                self._perform(payload)
                continue
            self.clock.wait(delay, self.wake)

    def _step(self) -> tuple[str, object, float | None]:
        with self.lock:
            self.wake.clear()
            if self.signal is not None:
                return "signal", self.signal, None
            if self.eof:
                return "eof", None, None
            actions = self._collect_actions_locked()
            if actions:
                return "actions", actions, None
            return "wait", None, self._next_delay_locked()

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum: int, _frame: object) -> None:
            self.signal = signum
            self._stdin_stop = True
            proc = self.child
            if proc is not None and proc.poll() is None:
                self._signal_tree(proc, signum)
            self._poke()
            self.wake.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _poke(self) -> None:
        try:
            os.write(self._wake_w, b"\0")
        except OSError:
            pass

    def _join_workers(self) -> None:
        for thread in list(self._workers):
            if thread is threading.current_thread():
                continue
            thread.join(timeout=2)

    def _stdin_loop(self) -> None:
        """Read stdin with poll so SIGTERM can wake this thread before shutdown.

        A daemon blocked in BufferedReader.readline holds the stdin lock and makes
        CPython abort during interpreter finalization.
        """
        stdin_fd = self.stdin.fileno()
        poll = select.poll()
        poll.register(stdin_fd, select.POLLIN)
        poll.register(self._wake_r, select.POLLIN)
        pending = b""
        eof = False
        try:
            while not self._stdin_stop:
                try:
                    events = poll.poll()
                except InterruptedError:
                    continue
                ready = {fd for fd, mask in events if mask & (select.POLLIN | select.POLLERR | select.POLLHUP)}
                if self._wake_r in ready:
                    try:
                        os.read(self._wake_r, 64)
                    except OSError:
                        pass
                    if self._stdin_stop or self.signal is not None:
                        break
                if stdin_fd not in ready:
                    continue
                try:
                    chunk = os.read(stdin_fd, 65536)
                except InterruptedError:
                    continue
                except OSError:
                    eof = True
                    break
                if not chunk:
                    eof = True
                    break
                pending += chunk
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    line = pending[: newline + 1]
                    pending = pending[newline + 1 :]
                    if line.strip() and not self._stdin_stop:
                        self._on_client_line(line)
        finally:
            if eof and self.signal is None:
                with self.lock:
                    self.eof = True
                    self.phase = Phase.STOPPING
                    self.wake.set()
            else:
                self.wake.set()

    def _on_client_line(self, raw: bytes) -> None:
        msg = _loads(raw)
        if not _is_jsonrpc(msg):
            return
        assert isinstance(msg, dict)
        if "method" not in msg:
            self._forward_client_response(raw)
            return
        method = msg["method"]
        if method == "initialize" and isinstance(msg.get("params"), dict):
            with self.lock:
                self.cached_params = copy.deepcopy(msg["params"])
                self.tools_list_changed = _wants_list_changed(msg["params"])
        if "id" not in msg:
            self._on_notification(method, msg, raw)
            return
        self._on_request(method, msg, raw)

    def _on_notification(self, method: str, msg: dict, raw: bytes) -> None:
        line = _normalize(raw)
        with self.child_write_lock:
            with self.lock:
                if method == "notifications/initialized":
                    self.client_initialized = True
                if method == "notifications/cancelled" and self._drop_queued_cancel_locked(msg):
                    return
                if self.phase is not Phase.READY or self.child is None:
                    return
                proc = self.child
            self._write_child_unlocked(proc, line)

    def _drop_queued_cancel_locked(self, msg: dict) -> bool:
        params = msg.get("params")
        if not isinstance(params, dict) or "requestId" not in params:
            return False
        key = _canon(params["requestId"])
        for index, item in enumerate(self.queue):
            if item["key"] == key:
                del self.queue[index]
                return True
        return False

    def _on_request(self, method: str, msg: dict, raw: bytes) -> None:
        item = {
            "key": _canon(msg["id"]),
            "id": msg["id"],
            "method": method,
            "tool": _tool_name(method, msg),
            "raw": _normalize(raw),
            "deadline": self.clock.monotonic() + self.ready_timeout,
        }
        with self.child_write_lock:
            with self.lock:
                ready = (
                    self.phase is Phase.READY
                    and not self.queue
                    and self.child is not None
                    and not self.eof
                    and self.signal is None
                )
                if ready:
                    self.inflight[item["key"]] = item
                    proc = self.child
                else:
                    self.queue.append(item)
                    self.wake.set()
                    proc = None
            if proc is not None:
                self._write_child_unlocked(proc, item["raw"])

    def _forward_client_response(self, raw: bytes) -> None:
        line = _normalize(raw)
        with self.child_write_lock:
            with self.lock:
                if self.phase not in (Phase.READY, Phase.REINIT) or self.child is None:
                    return
                proc = self.child
            self._write_child_unlocked(proc, line)

    def _collect_actions_locked(self) -> list[tuple]:
        now = self.clock.monotonic()
        if self.phase is Phase.DOWN and self.relaunch_at is not None and now >= self.relaunch_at:
            self.phase = Phase.STARTING
            self.relaunch_at = None
            return [("launch",)]
        if self.phase is Phase.REINIT and self.replay_status == "ok":
            self.replay_status = None
            return [("replay_ok",)]
        if self.phase is Phase.REINIT and (self.replay_status == "error" or now >= self.reinit_deadline):
            reason = "error" if self.replay_status == "error" else "timeout"
            self.replay_status = None
            self.reinit_deadline = float("inf")
            return [("replay_fail", reason)]
        expired = self._expire_locked(now)
        if expired:
            return [("expire", expired)]
        return []

    def _next_delay_locked(self) -> float | None:
        now = self.clock.monotonic()
        deadlines: list[float] = []
        if self.phase is Phase.DOWN and self.relaunch_at is not None:
            deadlines.append(self.relaunch_at)
        if self.phase is Phase.REINIT and self.reinit_deadline != float("inf"):
            deadlines.append(self.reinit_deadline)
        deadlines.extend(item["deadline"] for item in self.queue)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def _expire_locked(self, now: float) -> list[dict]:
        keep: list[dict] = []
        expired: list[dict] = []
        for item in self.queue:
            if now >= item["deadline"]:
                expired.append(item)
                self.failed_ids.add(item["key"])
            else:
                keep.append(item)
        self.queue = keep
        return expired

    def _perform(self, actions: object) -> None:
        if not isinstance(actions, list):
            return
        for action in actions:
            kind = action[0]
            if kind == "launch":
                self._perform_launch()
            elif kind == "replay_ok":
                self._perform_replay_ok()
            elif kind == "replay_fail":
                self._perform_replay_fail(action[1])
            elif kind == "expire":
                self._emit_errors(action[1])
            else:
                unknown: str = kind
                raise RuntimeError(f"unknown supervisor action {unknown}")

    def _perform_launch(self) -> None:
        try:
            proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            with self.lock:
                event = self._account_backoff_locked(127) if self.phase is Phase.STARTING else None
                if event is not None:
                    self.wake.set()
            if event is not None:
                self._log(event)
            return
        with self.lock:
            if self.eof or self.signal is not None:
                # New generation so a waiter from the previous child cannot drop this proc.
                self.generation += 1
                generation = self.generation
                self.phase = Phase.STOPPING
                self.child = proc
                abandon = True
                replay = False
                forward: list[dict] = []
                expired: list[dict] = []
            else:
                abandon = False
                self.generation += 1
                generation = self.generation
                self.child = proc
                self.failed_ids.clear()
                self.replay_status = None
                self.pending_internal_id = None
                replay = self._should_replay_locked()
                forward = []
                expired = []
                if replay:
                    self.phase = Phase.REINIT
                    self.healthy_since = None
                    self.internal_seq += 1
                    self.pending_internal_id = f"supervisor-{self.internal_seq}"
                    self.reinit_deadline = self.clock.monotonic() + self.ready_timeout
                else:
                    self.phase = Phase.READY
                    self.healthy_since = self.clock.monotonic()
                    forward, expired = self._split_queue_locked()
                    for item in forward:
                        self.inflight[item["key"]] = item
            pid = proc.pid
        self._start_pumps(generation, proc)
        self._log({"event": "backend_start", "pid": pid})
        if abandon:
            self._signal_tree(proc, signal.SIGTERM)
            return
        if replay:
            self._send_replay(proc)
            return
        self._flush_forward(proc, forward)
        self._emit_errors(expired)

    def _should_replay_locked(self) -> bool:
        return self.cached_params is not None and (self.client_initialized or self.handshake_complete)

    def _send_replay(self, proc: subprocess.Popen[bytes]) -> None:
        with self.lock:
            params = self.cached_params
            internal_id = self.pending_internal_id
        if params is None or internal_id is None:
            self._perform_replay_fail("write")
            return
        line = json.dumps(
            {"jsonrpc": "2.0", "id": internal_id, "method": "initialize", "params": params},
            ensure_ascii=False,
        ).encode() + b"\n"
        if not self._write_child(proc, line):
            self._log({"event": "initialize_replay_failed", "reason": "write"})
            self._signal_tree(proc, signal.SIGTERM)

    def _perform_replay_ok(self) -> None:
        proc = self.child
        if proc is None or not self._write_child(proc, INITIALIZED_LINE):
            self._perform_replay_fail("write")
            return
        with self.child_write_lock:
            with self.lock:
                if self.phase is not Phase.REINIT or self.child is not proc:
                    return
                now = self.clock.monotonic()
                forward, expired = self._split_queue_locked(now)
                self.phase = Phase.READY
                self.healthy_since = now
                self.pending_internal_id = None
                for item in forward:
                    self.inflight[item["key"]] = item
                list_changed = self.tools_list_changed
                generation = self.generation
            wrote = True
            for item in forward:
                if not self._write_child_unlocked(proc, item["raw"]):
                    wrote = False
                    break
        self._emit_errors(expired)
        if not wrote:
            self._signal_tree(proc, signal.SIGTERM)
            return
        if list_changed:
            with self.lock:
                still_ready = self.generation == generation and self.phase is Phase.READY
            if still_ready:
                self._write_stdout(LIST_CHANGED_LINE)

    def _perform_replay_fail(self, reason: str) -> None:
        self._log({"event": "initialize_replay_failed", "reason": reason})
        proc = self.child
        if proc is None:
            return
        self._signal_tree(proc, signal.SIGTERM)
        if _wait_proc(proc, 1):
            return
        self._signal_tree(proc, signal.SIGKILL)
        _wait_proc(proc, 1)

    def _flush_forward(self, proc: subprocess.Popen[bytes], forward: list[dict]) -> None:
        with self.child_write_lock:
            for item in forward:
                self._write_child_unlocked(proc, item["raw"])

    def _start_pumps(self, generation: int, proc: subprocess.Popen[bytes]) -> None:
        for target, args, name in (
            (self._pump_stdout, (generation, proc.stdout), f"supervisor-stdout-{generation}"),
            (self._pump_stderr, (proc.stderr,), f"supervisor-stderr-{generation}"),
            (self._wait_child, (generation, proc), f"supervisor-wait-{generation}"),
        ):
            thread = threading.Thread(target=target, args=args, name=name, daemon=True)
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            self._workers.append(thread)
            thread.start()

    def _pump_stdout(self, generation: int, stream: BinaryIO | None) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                self._on_child_stdout(generation, line)
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _pump_stderr(self, stream: BinaryIO | None) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                self._write_stderr(b"[backend] " + _normalize(line))
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _wait_child(self, generation: int, proc: subprocess.Popen[bytes]) -> None:
        code = proc.wait()
        self._on_child_exit(generation, code)

    def _on_child_exit(self, generation: int, code: int) -> None:
        event = None
        errors: list[dict] = []
        with self.lock:
            if generation != self.generation:
                return
            if self.signal is not None or self.eof or self.phase is Phase.STOPPING:
                self.phase = Phase.STOPPING
                self.wake.set()
                return
            if self.accounted_generation == generation:
                return
            self.accounted_generation = generation
            errors = self._take_inflight_locked()
            event = self._account_backoff_locked(code)
            self.wake.set()
        self._emit_errors(errors)
        if event is not None:
            self._log(event)

    def _take_inflight_locked(self) -> list[dict]:
        items = list(self.inflight.values())
        self.inflight.clear()
        for item in items:
            self.failed_ids.add(item["key"])
        return items

    def _account_backoff_locked(self, code: int) -> dict:
        now = self.clock.monotonic()
        if self.healthy_since is not None and now - self.healthy_since >= HEALTHY_SECONDS:
            self.next_backoff = min(INITIAL_BACKOFF, self.max_backoff)
        self.healthy_since = None
        delay = self.next_backoff
        self.next_backoff = min(delay * 2, self.max_backoff)
        self.relaunch_at = now + delay
        self.phase = Phase.DOWN
        self.child = None
        self.pending_internal_id = None
        self.replay_status = None
        return {"event": "backend_exit", "exit_code": code, "backoff_s": delay}

    def _split_queue_locked(self, now: float | None = None) -> tuple[list[dict], list[dict]]:
        if now is None:
            now = self.clock.monotonic()
        forward: list[dict] = []
        expired: list[dict] = []
        for item in self.queue:
            if now >= item["deadline"]:
                expired.append(item)
                self.failed_ids.add(item["key"])
            else:
                forward.append(item)
        self.queue = []
        return forward, expired

    def _on_child_stdout(self, generation: int, raw: bytes) -> None:
        msg = _loads(raw)
        if not _is_jsonrpc(msg):
            self._write_stderr(b"[backend] " + _normalize(raw))
            return
        assert isinstance(msg, dict)
        forward = False
        with self.lock:
            if generation != self.generation or self.phase is Phase.STOPPING:
                return
            if "method" in msg:
                forward = True
            elif isinstance(msg.get("id"), str) and msg["id"].startswith("supervisor-"):
                if self.pending_internal_id is not None and _canon(msg["id"]) == _canon(self.pending_internal_id):
                    self.pending_internal_id = None
                    self.replay_status = "error" if "error" in msg else "ok"
                    self.wake.set()
                return
            else:
                key = _canon(msg["id"])
                if key in self.failed_ids:
                    return
                item = self.inflight.pop(key, None)
                if item is None:
                    forward = True
                else:
                    if item["method"] == "initialize" and "result" in msg and "error" not in msg:
                        self.handshake_complete = True
                    forward = True
        if forward:
            self._write_stdout(raw)

    def _emit_errors(self, items: list[dict]) -> None:
        for item in items:
            self._write_stdout(_error_line(item))

    def _write_child(self, proc: subprocess.Popen[bytes] | None, data: bytes) -> bool:
        with self.child_write_lock:
            return self._write_child_unlocked(proc, data)

    def _write_child_unlocked(self, proc: subprocess.Popen[bytes] | None, data: bytes) -> bool:
        if proc is None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(_normalize(data))
            proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def _write_stdout(self, data: bytes) -> None:
        payload = _normalize(data)
        with self.stdout_lock:
            try:
                self.stdout.write(payload)
                self.stdout.flush()
            except (OSError, ValueError):
                pass

    def _write_stderr(self, data: bytes) -> None:
        with self.stderr_lock:
            try:
                self.stderr.write(data)
                self.stderr.flush()
            except (OSError, ValueError):
                pass

    def _log(self, event: dict) -> None:
        self._write_stderr(json.dumps(event, sort_keys=True).encode() + b"\n")

    def _signal_tree(self, proc: subprocess.Popen[bytes] | None, sig: int) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            return
        try:
            if pgid != os.getpgrp():
                os.killpg(pgid, sig)
                return
        except OSError:
            pass
        try:
            proc.send_signal(sig)
        except OSError:
            pass

    def _shutdown_eof(self) -> int:
        with self.lock:
            self.phase = Phase.STOPPING
            self.eof = True
            proc = self.child
        self._reap(proc)
        self._stdin_stop = True
        self._poke()
        self._join_workers()
        self._log({"event": "shutdown", "reason": "eof"})
        return 0

    def _shutdown_signal(self, signum: int) -> int:
        with self.lock:
            self.phase = Phase.STOPPING
            proc = self.child
        if proc is not None and proc.poll() is None:
            self._signal_tree(proc, signum)
            if not _wait_proc(proc, 2):
                self._signal_tree(proc, signal.SIGKILL)
                _wait_proc(proc, 2)
        elif proc is not None:
            _wait_proc(proc, 1)
        self._stdin_stop = True
        self._poke()
        self._join_workers()
        self._log({"event": "shutdown", "reason": "signal", "signal": signum})
        return 128 + signum

    def _reap(self, proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if _wait_proc(proc, SHUTDOWN_WAIT):
            return
        self._signal_tree(proc, signal.SIGTERM)
        if _wait_proc(proc, TERM_GRACE):
            return
        self._signal_tree(proc, signal.SIGKILL)
        _wait_proc(proc, 2)


def main(argv: list[str] | None = None) -> int:
    max_backoff, ready_timeout, command = parse_args(sys.argv[1:] if argv is None else argv)
    supervisor = Supervisor(command, max_backoff=max_backoff, ready_timeout=ready_timeout)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
