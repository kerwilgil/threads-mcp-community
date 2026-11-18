"""threads_publish_text carrying image_url is gated exactly like threads_publish_image (no bypass)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "threads_publication_gate.py"
MOCK = ROOT / "tests" / "mock_backend.py"
sys.path.insert(0, str(ROOT))
import threads_publication_gate as gate  # noqa: E402

IMG = "https://example.com/images/demo-image.jpg"
TXT_IMG, TXT_PLAIN, TXT_TAG = "Imagen aprobada 🚀", "Texto aprobado", "Texto con tag"
K_IMG, K_PLAIN, K_TAG = "demo-2026-02-02-img-v1", "demo-2026-02-02-plain-v1", "demo-2026-02-02-tag-v1"
T = gate.PUBLISH


class TextImageRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.registry = cls.tmp / "approved.json"
        cls.record = cls.tmp / "record.jsonl"
        cls.env = {**os.environ, "THREADS_GATE_REGISTRY": str(cls.registry), "THREADS_GATE_RECORD": str(cls.record)}
        for key, text, extra in (
            (K_IMG, TXT_IMG, ["--image-url", IMG, "--topic-tag", "AIThreads"]),
            (K_PLAIN, TXT_PLAIN, []),
            (K_TAG, TXT_TAG, ["--topic-tag", "AIThreads"]),
        ):
            f = cls.tmp / f"{key}.txt"
            f.write_bytes(text.encode("utf-8"))
            subprocess.run(
                [sys.executable, str(GATE), "register", "--key", key, "--text-file", str(f), *extra],
                env=cls.env, check=True, capture_output=True,
            )
        cls.reg = gate.load_registry(cls.registry)

    def test_text_tool_with_image_forwards_like_image_tool(self):
        args = {"text": TXT_IMG, "image_url": IMG, "topic_tag": "AIThreads", "publication_ref": K_IMG}
        expected = {"text": TXT_IMG, "idempotency_key": K_IMG, "image_url": IMG, "topic_tag": "AIThreads"}
        self.assertEqual(gate.check_publish(args, self.reg, T), expected)
        self.assertEqual(gate.check_publish(args, self.reg, gate.PUBLISH_IMAGE), expected)

    def test_text_tool_with_image_rejects_every_mismatch(self):
        base = {"text": TXT_IMG, "image_url": IMG, "topic_tag": "AIThreads", "publication_ref": K_IMG}
        bad = [
            {**base, "image_url": IMG + "?x=1"},
            {**base, "image_url": ""},
            {**base, "image_url": 1},
            {**base, "topic_tag": "Other"},
            {k: v for k, v in base.items() if k != "topic_tag"},
            {**base, "publication_ref": K_PLAIN},
            {**base, "publication_ref": "kiwi-probe-not-registered-20261112"},
            {**base, "text": TXT_IMG + " "},
            {**base, "idempotency_key": K_IMG},
            {**base, "link_attachment": "https://x.y"},
        ]
        for args in bad:
            with self.assertRaises(gate.GateError, msg=str(args)):
                gate.check_publish(args, self.reg, T)

    def test_text_only_entry_cannot_authorize_image_via_text_tool(self):
        for args in (
            {"text": TXT_PLAIN, "image_url": IMG, "publication_ref": K_PLAIN},
            {"text": TXT_TAG, "image_url": IMG, "topic_tag": "AIThreads", "publication_ref": K_TAG},
            {"text": TXT_PLAIN, "image_url": "", "publication_ref": K_PLAIN},
        ):
            with self.assertRaises(gate.GateError, msg=str(args)):
                gate.check_publish(args, self.reg, T)

    def test_image_entry_cannot_publish_as_text_only(self):
        with self.assertRaises(gate.GateError):
            gate.check_publish({"text": TXT_IMG, "publication_ref": K_IMG, "topic_tag": "AIThreads"}, self.reg, T)

    def test_text_and_tag_only_still_work(self):
        self.assertEqual(
            gate.check_publish({"text": TXT_TAG, "publication_ref": K_TAG, "topic_tag": "AIThreads"}, self.reg, T),
            {"text": TXT_TAG, "idempotency_key": K_TAG, "topic_tag": "AIThreads"},
        )

    def test_transform_advertises_optional_image_url_on_text_tool(self):
        result = {"tools": [{"name": T, "inputSchema": {"properties": {
            "text": {}, "idempotency_key": {}, "topic_tag": {}, "image_url": {}}}}]}
        schema = gate.transform_tools(result)["tools"][0]["inputSchema"]
        self.assertEqual(schema["required"], ["text", "publication_ref"])
        self.assertIn("image_url", schema["properties"])
        self.assertNotIn("idempotency_key", schema["properties"])

    def test_proxy_end_to_end(self):
        self.record.write_text("")

        def call(i, **arguments):
            return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": T, "arguments": arguments}}

        calls = [
            call(1, text="probe", image_url=IMG, topic_tag="AIThreads",
                 publication_ref="kiwi-probe-not-registered-20261112"),
            call(2, text=TXT_PLAIN, image_url=IMG, publication_ref=K_PLAIN),
            call(3, text=TXT_IMG, image_url=IMG, topic_tag="AIThreads", publication_ref=K_IMG),
        ]
        lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}]
        p = subprocess.run(
            [sys.executable, str(GATE), "proxy", "--", sys.executable, str(MOCK)],
            env=self.env, input=("\n".join(json.dumps(r) for r in lines + calls) + "\n").encode(),
            capture_output=True, timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        out = {m["id"]: m for m in map(json.loads, p.stdout.decode().splitlines())}
        for blocked in (1, 2):
            text = out[blocked]["result"]["content"][0]["text"]
            self.assertTrue(out[blocked]["result"]["isError"])
            self.assertIn("BLOCKED", text)
        probe = out[1]["result"]["content"][0]["text"]
        self.assertIn("[gate received fields: image_url, publication_ref, text, topic_tag]", probe)
        self.assertNotIn(IMG, probe)  # names only, never values
        recorded = [json.loads(line) for line in self.record.read_text().splitlines()]
        self.assertEqual(recorded, [{"name": T, "arguments": {
            "text": TXT_IMG, "idempotency_key": K_IMG, "image_url": IMG, "topic_tag": "AIThreads"}}])


if __name__ == "__main__":
    unittest.main()
