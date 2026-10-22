"""Exercise built public binaries without a desktop or real Collection."""

import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build/clipper"
ORIGIN = "chrome-extension://" + "a" * 32 + "/"


class ProtocolSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clipper-ci-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.data = root / "collection"
        self.env = {
            "PATH": os.defpath,
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "QT_QPA_PLATFORM": "offscreen",
        }

    def run_binary(self, binary, args, data=None):
        return subprocess.run(
            [str(binary), *args], input=data, capture_output=True,
            env=self.env, timeout=20, check=True,
        ).stdout

    def request(self, **request):
        return json.loads(self.run_binary(
            BINARY, ["--data-dir", str(self.data), "--request"],
            json.dumps(request).encode(),
        ))

    def test_component_versions_match(self):
        version = self.run_binary(BINARY, ["--version"]).strip()
        self.assertTrue(version)
        self.assertEqual(version, self.run_binary(
            ROOT / "build/clipper-native", ["--version"],
        ).strip())

    def test_draft_persists_and_rejects_stale_writes(self):
        initial = self.request(op="draft.get")
        self.assertTrue(initial["ok"], initial)
        revision = initial["result"]["revision"]
        saved = self.request(op="draft.save", text="CI synthetic note",
                             assetIds=[], expectedRevision=revision)
        self.assertTrue(saved["ok"], saved)
        stale = self.request(op="draft.save", text="stale overwrite",
                             assetIds=[], expectedRevision=revision)
        self.assertFalse(stale["ok"], stale)
        current = self.request(op="draft.get")
        self.assertTrue(current["ok"], current)
        self.assertEqual(current["result"]["text"], "CI synthetic note")

    def test_native_denial_does_not_break_next_frame(self):
        requests = [
            {"op": "draft.get"},
            {"op": "capture.check", "sourceUrl": "https://example.com/",
             "scope": "article"},
        ]
        frames = bytearray()
        for request in requests:
            payload = json.dumps(request).encode()
            frames.extend(struct.pack("=I", len(payload)) + payload)
        output = self.run_binary(
            BINARY, ["--data-dir", str(self.data), "--native",
                     "--allowed-origin", ORIGIN, ORIGIN], bytes(frames),
        )
        responses = []
        while output:
            self.assertGreaterEqual(len(output), 4)
            length = struct.unpack("=I", output[:4])[0]
            self.assertGreaterEqual(len(output), 4 + length)
            responses.append(json.loads(output[4:4 + length]))
            output = output[4 + length:]
        self.assertEqual(len(responses), 2)
        self.assertFalse(responses[0]["ok"], responses[0])
        self.assertTrue(responses[1]["ok"], responses[1])
        self.assertIsNone(responses[1]["result"]["existing"])


if __name__ == "__main__":
    unittest.main()
