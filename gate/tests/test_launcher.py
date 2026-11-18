import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "bin" / "threads-mcp-launcher"
KEY = "K" * 43 + "="  # fake key, only its length may ever be observed

STUB = """#!/bin/sh
# prints what the real process would receive, never the key itself
echo "{name} args=[$*] keylen=${{#TOKEN_ENCRYPTION_KEY}} token=$THREADS_TOKEN_STORE ledger=$THREADS_LEDGER_PATH \\
oauth=$THREADS_OAUTH_STATE_PATH umask=$(umask)"
"""


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runtime = self.tmp / "runtime"
        (self.runtime / "bin").mkdir(parents=True)
        (self.runtime / ".venv" / "bin").mkdir(parents=True)
        shutil.copy(LAUNCHER, self.runtime / "bin" / "threads-mcp-launcher")
        for name in ("threads-mcp", "threads-mcp-auth"):
            stub = self.runtime / ".venv" / "bin" / name
            stub.write_text(STUB.format(name=name))
            stub.chmod(0o755)
        self.home = self.tmp / "home"
        self.conf = self.home / ".config" / "threads-mcp"
        self.conf.mkdir(parents=True)
        (self.conf / "encryption.key").write_text(KEY + "\n")
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_launcher(self, *args, env=None):
        launcher = self.runtime / "bin" / "threads-mcp-launcher"
        return subprocess.run(
            ["sh", str(launcher), *args], env=env or self.env, capture_output=True, text=True, timeout=30
        )

    def test_server_mode_uses_documented_paths_and_hides_key(self):
        p = self.run_launcher("--flag")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout.strip()
        self.assertTrue(out.startswith("threads-mcp args=[--flag]"))
        self.assertIn(f"keylen={len(KEY)}", out)
        self.assertIn(f"token={self.conf}/token.enc", out)
        self.assertIn(f"ledger={self.home}/.local/state/threads-mcp/publications.sqlite3", out)
        self.assertIn(f"oauth={self.home}/.local/state/threads-mcp/oauth-state.json", out)
        self.assertIn("umask=0077", out)
        self.assertNotIn(KEY, p.stdout + p.stderr)

    def test_auth_is_cli_only_and_forwards_args(self):
        p = self.run_launcher("auth", "status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("threads-mcp-auth args=[status]"))
        self.assertNotIn(KEY, p.stdout + p.stderr)

    def test_xdg_overrides(self):
        conf, state = self.tmp / "c", self.tmp / "s"
        (conf / "threads-mcp").mkdir(parents=True)
        (conf / "threads-mcp" / "encryption.key").write_text(KEY)
        env = {**self.env, "XDG_CONFIG_HOME": str(conf), "XDG_STATE_HOME": str(state)}
        out = self.run_launcher(env=env).stdout
        self.assertIn(f"token={conf}/threads-mcp/token.enc", out)
        self.assertIn(f"ledger={state}/threads-mcp/publications.sqlite3", out)

    def test_runtime_override(self):
        other = self.tmp / "other"
        (other / ".venv" / "bin").mkdir(parents=True)
        stub = other / ".venv" / "bin" / "threads-mcp"
        stub.write_text(STUB.format(name="other-runtime"))
        stub.chmod(0o755)
        p = self.run_launcher(env={**self.env, "THREADS_MCP_RUNTIME": str(other)})
        self.assertTrue(p.stdout.startswith("other-runtime"))

    def test_missing_key_fails_without_stdout(self):
        (self.conf / "encryption.key").unlink()
        p = self.run_launcher()
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, "")  # stdout is reserved for the MCP protocol
        self.assertIn("encryption.key", p.stderr)

    def test_empty_key_fails(self):
        (self.conf / "encryption.key").write_text("\n")
        p = self.run_launcher()
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("empty", p.stderr)

    def test_missing_executable_fails(self):
        (self.runtime / ".venv" / "bin" / "threads-mcp").unlink()
        p = self.run_launcher()
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("missing executable", p.stderr)

    def test_posix_sh_syntax(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(LAUNCHER)]).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
