import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "threads_publication_gate.py"
MOCK = ROOT / "tests" / "mock_backend.py"
TEXTS = json.loads((ROOT / "tests" / "fixtures" / "approved_texts.json").read_text(encoding="utf-8"))
K1, K3 = "demo-2026-01-15-post-one-v1", "demo-2026-01-16-post-three-v1"


def backend_python():
    """The interpreter running these tests, if it can import threads_mcp; None otherwise."""
    for candidate in (sys.executable,):
        if (
            Path(candidate).exists()
            and subprocess.run([candidate, "-c", "import threads_mcp"], capture_output=True).returncode == 0
        ):
            return candidate
    return None


BACKEND_PY = backend_python()


class GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.registry = cls.tmp / "approved.json"
        cls.record = cls.tmp / "record.jsonl"
        cls.env = {**os.environ, "THREADS_GATE_REGISTRY": str(cls.registry), "THREADS_GATE_RECORD": str(cls.record)}
        for key, name in ((K1, "p1"), (K3, "p3")):  # register through the real CLI, text used byte-for-byte
            f = cls.tmp / f"{name}.txt"
            f.write_bytes(TEXTS[name].encode("utf-8"))
            subprocess.run(
                [sys.executable, str(GATE), "register", "--key", key, "--text-file", str(f)],
                env=cls.env,
                check=True,
                capture_output=True,
            )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.record.write_text("")

    def session(self, requests):
        lines = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            *requests,
        ]
        p = subprocess.run(
            [sys.executable, str(GATE), "proxy", "--", sys.executable, str(MOCK)],
            env=self.env,
            input=("\n".join(json.dumps(r) for r in lines) + "\n").encode(),
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        return {m["id"]: m for m in map(json.loads, p.stdout.decode().splitlines())}

    def publish(self, text, ref, rid=1, **extra):
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": "threads_publish_text", "arguments": {"text": text, "publication_ref": ref, **extra}},
        }

    def forwarded(self):
        return [json.loads(row) for row in self.record.read_text().splitlines() if row.strip()]

    # A
    def test_a_news1_exact_key(self):
        r = self.session([self.publish(TEXTS["p1"], K1)])
        self.assertFalse(r[1]["result"]["isError"])
        (call,) = self.forwarded()
        self.assertEqual(call["arguments"]["idempotency_key"], K1)
        self.assertEqual(len(call["arguments"]["idempotency_key"]), len(K1))
        self.assertEqual(call["arguments"]["text"], TEXTS["p1"])  # byte-identical text
        self.assertEqual(set(call["arguments"]), {"text", "idempotency_key"})

    # B
    def test_b_news3_exact_key(self):
        self.session([self.publish(TEXTS["p3"], K3)])
        (call,) = self.forwarded()
        self.assertEqual(call["arguments"]["idempotency_key"], K3)
        self.assertEqual(len(call["arguments"]["idempotency_key"]), len(K3))
        self.assertEqual(call["arguments"]["text"], TEXTS["p3"])

    # C
    def test_c_same_piece_same_key(self):
        self.session([self.publish(TEXTS["p1"], K1, 1), self.publish(TEXTS["p1"], K1, 2)])
        keys = [c["arguments"]["idempotency_key"] for c in self.forwarded()]
        self.assertEqual(keys, [K1, K1])

    # D
    def test_d_different_piece_different_key(self):
        self.session([self.publish(TEXTS["p1"], K1, 1), self.publish(TEXTS["p3"], K3, 2)])
        k1, k3 = (c["arguments"]["idempotency_key"] for c in self.forwarded())
        self.assertNotEqual(k1, k3)
        # a different piece can never borrow the previous key
        r = self.session([self.publish(TEXTS["p3"], K1)])
        self.assertTrue(r[1]["result"]["isError"])
        self.assertIn(K3, r[1]["result"]["content"][0]["text"])

    def assertBlocked(self, request):
        r = self.session([request])
        self.assertTrue(r[request["id"]]["result"]["isError"])
        self.assertIn("BLOCKED by publication gate", r[request["id"]]["result"]["content"][0]["text"])
        self.assertEqual(self.forwarded(), [], "backend must not be reached")

    def test_rejects_abbreviated_ref(self):
        self.assertBlocked(self.publish(TEXTS["p1"], "demo-2026-"))  # the 10-char key

    def test_rejects_unknown_text(self):
        self.assertBlocked(self.publish("Texto nuevo no aprobado", "demo-2026-01-15-new-v1"))

    def test_rejects_one_char_change(self):
        self.assertBlocked(self.publish(TEXTS["p1"] + " ", K1))
        self.assertBlocked(self.publish(TEXTS["p1"].replace("GitHub", "Github", 1), K1))

    def test_rejects_legacy_idempotency_key_arg(self):
        self.assertBlocked(self.publish(TEXTS["p1"], K1, idempotency_key=K1))
        self.assertBlocked(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "threads_publish_text", "arguments": {"text": TEXTS["p1"], "idempotency_key": K1}},
            }
        )

    def test_rejects_missing_registry_fail_closed(self):
        env = {**self.env, "THREADS_GATE_REGISTRY": str(self.tmp / "missing.json")}
        p = subprocess.run(
            [sys.executable, str(GATE), "proxy", "--", sys.executable, str(MOCK)],
            env=env,
            timeout=30,
            input=(json.dumps(self.publish(TEXTS["p1"], K1)) + "\n").encode(),
            capture_output=True,
        )
        self.assertIn(b"BLOCKED", p.stdout)
        self.assertEqual(self.forwarded(), [])

    def test_register_never_overwrites(self):
        f = self.tmp / "dup.txt"
        f.write_bytes(TEXTS["p1"].encode())
        p = subprocess.run(
            [sys.executable, str(GATE), "register", "--key", "other-key-0001", "--text-file", str(f)],
            env=self.env,
            capture_output=True,
        )
        self.assertNotEqual(p.returncode, 0)

    def test_tools_list_schema_and_passthrough(self):
        r = self.session(
            [
                {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "threads_get_profile", "arguments": {}},
                },
            ]
        )
        tools = {t["name"]: t for t in r[5]["result"]["tools"]}
        self.assertEqual(
            sorted(tools),
            ["threads_get_post", "threads_get_profile", "threads_list_recent_posts", "threads_publish_text"],
        )
        schema = tools["threads_publish_text"]["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"text", "publication_ref"})
        self.assertEqual(schema["required"], ["text", "publication_ref"])
        original = next(
            json.loads(row)["result"]
            for row in (ROOT / "tests/fixtures/real_tools_list.jsonl").open()
            if json.loads(row).get("id") == 2
        )
        for t in original["tools"]:
            if t["name"] != "threads_publish_text":
                self.assertEqual(tools[t["name"]], t, "other tools must be untouched")
        self.assertEqual(self.forwarded()[0]["name"], "threads_get_profile")  # reads pass through

    def test_registry_matches_certified_payloads(self):
        reg = json.loads(self.registry.read_text())["publications"]
        by_key = {e["key"]: e for e in reg}
        self.assertEqual(by_key[K1]["text_sha256"], hashlib.sha256(TEXTS["p1"].encode()).hexdigest())
        self.assertEqual((by_key[K1]["chars"], by_key[K3]["chars"]), (len(TEXTS["p1"]), len(TEXTS["p3"])))


sys.path.insert(0, str(ROOT))
import threads_publication_gate as gate  # noqa: E402

N1 = TEXTS["p1"]


def drop_hash(text, nth):
    """Remove the '#' of the nth (0-based) hashtag of text."""
    idx = [m.start() for m in gate.HASHTAG.finditer(text)][nth]
    return text[:idx] + text[idx + 1 :]


class CanonicalVerificationTest(unittest.TestCase):
    def test_exact_equality_passes(self):
        v = gate.canonical_verification(N1, N1)
        self.assertEqual((v["verification"], v["exact_match"], v["threads_topic_normalization"]), ("PASS", True, False))
        self.assertEqual((v["requested_text"], v["remote_text"]), (N1, N1))

    def test_only_first_hash_removed_passes_with_normalization(self):
        v = gate.canonical_verification(N1, drop_hash(N1, 0))
        self.assertEqual((v["verification"], v["exact_match"], v["threads_topic_normalization"]), ("PASS", False, True))
        self.assertEqual(len(v["remote_text"]), len(N1) - 1)

    def test_second_hashtag_removed_fails(self):
        v = gate.canonical_verification(N1, drop_hash(N1, 1))
        self.assertEqual((v["verification"], v["threads_topic_normalization"]), ("FAIL", False))

    def test_first_and_second_removed_fails(self):
        self.assertEqual(gate.canonical_verification(N1, drop_hash(drop_hash(N1, 0), 0))["verification"], "FAIL")

    def test_extra_character_changed_fails(self):
        r = drop_hash(N1, 0).replace("benchmark", "benchmarK", 1)
        self.assertEqual(gate.canonical_verification(N1, r)["verification"], "FAIL")
        self.assertEqual(
            gate.canonical_verification(N1, N1.replace("benchmark", "benchmarK", 1))["verification"], "FAIL"
        )

    def test_space_changed_fails(self):
        r = drop_hash(N1, 0)
        self.assertEqual(
            gate.canonical_verification(N1, r.replace("Basado en", "Basado  en", 1))["verification"], "FAIL"
        )
        self.assertEqual(gate.canonical_verification(N1, r + " ")["verification"], "FAIL")
        self.assertEqual(gate.canonical_verification(N1, r.replace("\n\n", "\n", 1))["verification"], "FAIL")

    def test_hashtag_changed_fails(self):
        self.assertEqual(
            gate.canonical_verification(N1, drop_hash(N1, 0).replace("CodeReview", "CodeReviews"))["verification"],
            "FAIL",
        )
        self.assertEqual(gate.canonical_verification(N1, N1.replace("#AI", "#ML", 1))["verification"], "FAIL")
        self.assertEqual(
            gate.canonical_verification(N1, drop_hash(N1, 0).replace("AI #", "ML #", 1))["verification"], "FAIL"
        )

    def test_url_fragment_is_not_a_hashtag(self):
        req = "mira https://x.io/a#sec ahora #Tag"
        self.assertTrue(gate.HASHTAG.search(req).start() == req.index("#Tag"))
        self.assertEqual(gate.canonical_verification(req, "mira https://x.io/a#sec ahora Tag")["verification"], "PASS")
        self.assertEqual(gate.canonical_verification(req, "mira https://x.io/asec ahora #Tag")["verification"], "FAIL")

    def test_news3_first_hashtag_only(self):
        n3 = TEXTS["p3"]
        self.assertEqual(gate.canonical_verification(n3, drop_hash(n3, 0))["verification"], "PASS")
        self.assertEqual(gate.canonical_verification(n3, drop_hash(n3, 1))["verification"], "FAIL")
        self.assertEqual(gate.canonical_verification(n3, drop_hash(n3, 2))["verification"], "FAIL")


@unittest.skipUnless(BACKEND_PY, "threads_mcp is not importable")
class GetPostIntegrationTest(GateTest):
    """threads_get_post through the gate gets a canonical `verification` block (read-only, mock backend)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.db = cls.tmp / "ledger.sqlite3"
        code = r"""
import hashlib, json, sys
from pathlib import Path
from threads_mcp.ledger import PublicationLedger
db, key, text, post_id = sys.argv[1:5]
led = PublicationLedger(Path(db))
led.reserve(key, text)
claimed = led.claim(key)
steps = (
    ("creating", {}),
    ("container_created", {"creation_id": "c1"}),
    ("publish_started", {"creation_id": "c1"}),
    ("published", {"post_id": post_id, "permalink": "https://example/p"}),
)
for status, fields in steps:
    led.set_status(key, status, owner=claimed.owner, **fields)
"""
        subprocess.run([BACKEND_PY, "-c", code, str(cls.db), K1, N1, "POST1"], check=True, capture_output=True)
        cls.env = {**cls.env, "THREADS_LEDGER_PATH": str(cls.db)}

    def get_post(self, remote, post_id="POST1"):
        env = {**self.env, "THREADS_GATE_MOCK_REMOTE_TEXT": remote}
        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "threads_get_post", "arguments": {"post_id": post_id}},
        }
        p = subprocess.run(
            [sys.executable, str(GATE), "proxy", "--", sys.executable, str(MOCK)],
            env=env,
            timeout=30,
            input=(json.dumps(req) + "\n").encode(),
            capture_output=True,
        )
        return {m["id"]: m for m in map(json.loads, p.stdout.decode().splitlines())}[7]["result"]

    def test_pass_with_normalization_and_audit_log(self):
        r = self.get_post(drop_hash(N1, 0))
        v = r["structuredContent"]["verification"]
        self.assertEqual(
            (v["verification"], v["threads_topic_normalization"], v["exact_match"], v["publication_ref"], v["post_id"]),
            ("PASS", True, False, K1, "POST1"),
        )
        self.assertEqual(json.loads(r["content"][0]["text"])["verification"], v)  # mirrored in the text content
        self.assertEqual(r["structuredContent"]["text"], drop_hash(N1, 0))  # the remote post is reported untouched
        line = json.loads((self.db.parent / "gate-verifications.ndjson").read_text().splitlines()[-1])
        self.assertEqual((line["verification"], line["post_id"]), ("PASS", "POST1"))

    def test_exact_pass_and_fail(self):
        self.assertEqual(self.get_post(N1)["structuredContent"]["verification"]["verification"], "PASS")
        self.assertEqual(self.get_post(drop_hash(N1, 1))["structuredContent"]["verification"]["verification"], "FAIL")

    def test_unknown_post_not_applicable(self):
        v = self.get_post(N1, "OTHER")["structuredContent"]["verification"]
        self.assertEqual(v["verification"], "NOT_APPLICABLE")


class ReplayOfflineTest(unittest.TestCase):
    """Real threads-mcp service code: replaying a published key never builds a network client."""

    @unittest.skipUnless(BACKEND_PY, "threads_mcp is not importable")
    def test_replay_makes_no_public_write(self):
        code = r"""
import json, sys, tempfile
from pathlib import Path
from cryptography.fernet import Fernet
from threads_mcp.config import Settings
from threads_mcp.ledger import PublicationLedger
from threads_mcp.service import ThreadsService
texts = json.load(open(sys.argv[1]))
tmp = Path(tempfile.mkdtemp())
s = Settings(api_base="https://graph.threads.net/v1.0", token_store_path=tmp/"t.enc", ledger_path=tmp/"l.sqlite3",
             oauth_state_path=tmp/"o.json", encryption_key=Fernet.generate_key().decode())
led = PublicationLedger(s.ledger_path)
K="demo-2026-01-15-post-one-v1"
rec = led.reserve(K, texts["p1"]); claimed = led.claim(K)
steps = (
    ("creating", {}),
    ("container_created", {"creation_id": "c1"}),
    ("publish_started", {"creation_id": "c1"}),
    ("published", {"post_id": "1000000000000001", "permalink": "https://example/p"}),
)
for st, f in steps:
    led.set_status(K, st, owner=claimed.owner, **f)
calls = []
def boom(token): calls.append(1); raise AssertionError("network client requested")
svc = ThreadsService(s, ledger=led, client_factory=boom)
svc._load_token = lambda: (_ for _ in ()).throw(AssertionError("token requested"))
r = svc.publish_text(texts["p1"], K)
out = {"status": r.status, "post_id": r.post_id, "key": r.idempotency_key, "msg": r.message}
print(json.dumps({**out, "client_calls": len(calls)}))
"""
        p = subprocess.run(
            [BACKEND_PY, "-c", code, str(ROOT / "tests/fixtures/approved_texts.json")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(
            (out["status"], out["post_id"], out["key"], out["client_calls"]), ("published", "1000000000000001", K1, 0)
        )
        self.assertIn("Already published", out["msg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
