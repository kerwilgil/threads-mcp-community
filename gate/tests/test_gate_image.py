"""Gate rules for threads_publish_image and topic_tag: approval binds text + image_url + topic_tag."""

import hashlib
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
K_IMG, K_PLAIN, K_TAG = "demo-2026-02-01-img-v1", "demo-2026-02-01-plain-v1", "demo-2026-02-01-tag-v1"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ImageGateTest(unittest.TestCase):
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

    def test_registry_entry_format(self):
        data = json.loads(self.registry.read_text())
        self.assertEqual(data["version"], 1)
        entry = next(e for e in data["publications"] if e["key"] == K_IMG)
        self.assertEqual(entry["image_url"], IMG)
        self.assertEqual(entry["topic_tag"], "AIThreads")
        self.assertEqual(entry["text_sha256"], sha(TXT_IMG))
        self.assertNotIn("image_url", next(e for e in data["publications"] if e["key"] == K_PLAIN))

    def test_image_call_forwarded_with_registry_values(self):
        args = {"text": TXT_IMG, "image_url": IMG, "topic_tag": "AIThreads", "publication_ref": K_IMG}
        self.assertEqual(
            gate.check_publish(args, self.reg, gate.PUBLISH_IMAGE),
            {"text": TXT_IMG, "idempotency_key": K_IMG, "image_url": IMG, "topic_tag": "AIThreads"},
        )

    def test_image_mismatches_rejected(self):
        base = {"text": TXT_IMG, "image_url": IMG, "topic_tag": "AIThreads", "publication_ref": K_IMG}
        bad = [
            {**base, "image_url": IMG + "?x=1"},
            {**base, "topic_tag": "Other"},
            {k: v for k, v in base.items() if k != "topic_tag"},
            {**base, "publication_ref": K_PLAIN},
            {**base, "text": TXT_IMG + " "},
            {**base, "idempotency_key": K_IMG},
            {**base, "link_attachment": "https://x.y"},
            {k: v for k, v in base.items() if k != "image_url"},
        ]
        for args in bad:
            with self.assertRaises(gate.GateError, msg=str(args)):
                gate.check_publish(args, self.reg, gate.PUBLISH_IMAGE)

    def test_tools_cannot_cross_approval_kind(self):
        with self.assertRaises(gate.GateError):  # image approval cannot be published as text-only
            gate.check_publish({"text": TXT_IMG, "publication_ref": K_IMG, "topic_tag": "AIThreads"}, self.reg)
        with self.assertRaises(gate.GateError):  # text approval cannot gain an image
            gate.check_publish(
                {"text": TXT_PLAIN, "image_url": IMG, "publication_ref": K_PLAIN}, self.reg, gate.PUBLISH_IMAGE
            )
        with self.assertRaises(gate.GateError):
            gate.check_publish({"text": TXT_PLAIN, "publication_ref": K_PLAIN}, self.reg, "threads_publish_video")

    def test_text_topic_tag_rules(self):
        self.assertEqual(
            gate.check_publish({"text": TXT_PLAIN, "publication_ref": K_PLAIN}, self.reg),
            {"text": TXT_PLAIN, "idempotency_key": K_PLAIN},
        )
        self.assertEqual(
            gate.check_publish({"text": TXT_PLAIN, "publication_ref": K_PLAIN, "topic_tag": ""}, self.reg),
            {"text": TXT_PLAIN, "idempotency_key": K_PLAIN},
        )
        with self.assertRaises(gate.GateError):
            gate.check_publish({"text": TXT_PLAIN, "publication_ref": K_PLAIN, "topic_tag": "AIThreads"}, self.reg)
        with self.assertRaises(gate.GateError):
            gate.check_publish({"text": TXT_TAG, "publication_ref": K_TAG}, self.reg)
        self.assertEqual(
            gate.check_publish({"text": TXT_TAG, "publication_ref": K_TAG, "topic_tag": "AIThreads"}, self.reg),
            {"text": TXT_TAG, "idempotency_key": K_TAG, "topic_tag": "AIThreads"},
        )

    def test_invalid_registry_media_fields_fail_closed(self):
        for extra in ({"image_url": "http://insecure/x.jpg"}, {"topic_tag": "a.b"}, {"topic_tag": ""}):
            path = self.tmp / "bad.json"
            entry = {"key": K_PLAIN, "text_sha256": sha(TXT_PLAIN), "chars": len(TXT_PLAIN), **extra}
            path.write_text(json.dumps({"version": 1, "publications": [entry]}))
            with self.assertRaises(gate.GateError):
                gate.load_registry(path)

    def test_transform_tools_exposes_image_tool_and_hides_unknown_publish_tools(self):
        result = {"tools": [
            {"name": "threads_publish_image", "inputSchema": {"properties": {
                "text": {}, "image_url": {}, "idempotency_key": {}, "topic_tag": {}}}},
            {"name": "threads_publish_video", "inputSchema": {"properties": {}}},
            {"name": "threads_get_post", "inputSchema": {"properties": {"post_id": {}}}},
        ]}
        tools = {t["name"]: t for t in gate.transform_tools(result)["tools"]}
        self.assertEqual(set(tools), {"threads_publish_image", "threads_get_post"})
        schema = tools["threads_publish_image"]["inputSchema"]
        self.assertEqual(schema["required"], ["text", "image_url", "publication_ref"])
        self.assertNotIn("idempotency_key", schema["properties"])
        self.assertIn("topic_tag", schema["properties"])

    def test_proxy_end_to_end_blocks_and_forwards(self):
        self.record.write_text("")
        def call(i, name, **arguments):
            return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

        calls = [
            call(1, "threads_publish_image", text=TXT_IMG, image_url=IMG + "#evil", topic_tag="AIThreads",
                 publication_ref=K_IMG),
            call(2, "threads_publish_video", text=TXT_IMG, publication_ref=K_IMG),
            call(3, "threads_publish_image", text=TXT_IMG, image_url=IMG, topic_tag="AIThreads", publication_ref=K_IMG),
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
            self.assertTrue(out[blocked]["result"]["isError"])
            self.assertIn("BLOCKED", out[blocked]["result"]["content"][0]["text"])
        recorded = [json.loads(line) for line in self.record.read_text().splitlines()]
        self.assertEqual(recorded, [{"name": "threads_publish_image", "arguments": {
            "text": TXT_IMG, "idempotency_key": K_IMG, "image_url": IMG, "topic_tag": "AIThreads"}}])


if __name__ == "__main__":
    unittest.main()
