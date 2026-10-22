"""Image-budget behavior through request and persistent native sessions."""

import base64
import json
import os
from pathlib import Path
import resource
import struct
import subprocess
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get(
    "CLIPPER_IMAGE_TEST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
HELPER = ROOT / "build/clipper-helper"
ORIGIN = "chrome-extension://" + "a" * 32 + "/"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def runtime_environment(root):
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "LANG": "C.UTF-8", "QT_QPA_PLATFORM": "offscreen"}
    for name in ("HOME", "TMPDIR", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                 "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        path = root / name.lower()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment[name] = str(path)
    return environment


def png_header(width, height, depth=8, color=0):
    def chunk(kind, payload):
        return (struct.pack("!I", len(payload)) + kind + payload
                + struct.pack("!I", zlib.crc32(kind + payload)))
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, depth, color, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    ).decode()


def process_limits():
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))


class ImageBudgetTests(unittest.TestCase):
    def invoke(self, root, requests, native=False):
        environment = runtime_environment(root / "environment")
        args = [str(BIN), "--data-dir", str(root / "collection")]
        args += ["--helper-bin", str(HELPER)]
        if native:
            args += ["--native", "--allowed-origin", ORIGIN, ORIGIN]
            messages = [json.dumps(value).encode() for value in requests]
            payload = b"".join(struct.pack("=I", len(value)) + value for value in messages)
        else:
            args += ["--request"]
            payload = json.dumps(requests[0]).encode()
        result = subprocess.run(args, input=payload, capture_output=True,
                                env=environment, timeout=15, preexec_fn=process_limits)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        if not native:
            return [json.loads(result.stdout)]
        responses = []
        remaining = result.stdout
        while remaining:
            size = struct.unpack("=I", remaining[:4])[0]
            responses.append(json.loads(remaining[4:4 + size]))
            remaining = remaining[4 + size:]
        self.assertEqual(len(responses), len(requests))
        return responses

    def test_over_budget_header_is_rejected_before_decoding_or_storing(self):
        for width, height, depth, color in ((8192, 8192, 8, 0), (4097, 4096, 16, 6)):
            with self.subTest(depth=depth), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                response = self.invoke(root, [{
                    "op": "asset.import", "data": png_header(width, height, depth, color),
                    "mime": "image/png", "name": "oversized.png",
                }])[0]
                self.assertFalse(response["ok"], response)
                self.assertEqual(response.get("errorCode"), "IMAGE_DIMENSIONS_UNSUPPORTED", response)
                self.assertEqual(list((root / "collection" / "assets").iterdir()), [])

    def test_native_rejects_bad_images_and_then_imports_valid_image(self):
        invalid = [
            (png_header(8192, 8192), "image/png", "IMAGE_DIMENSIONS_UNSUPPORTED"),
            (base64.b64encode(b"not an image").decode(), "image/png", "IMAGE_DECODE_FAILED"),
            (PNG, "image/jpeg", "IMAGE_MIME_MISMATCH"),
            (base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>').decode(),
             "image/svg+xml", None),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = [{"op": "asset.import", "data": data, "mime": mime}
                        for data, mime, _ in invalid]
            requests.append({"op": "asset.import", "data": PNG, "mime": "image/png"})
            responses = self.invoke(root, requests, native=True)
            for response, (_, _, code) in zip(responses, invalid):
                self.assertFalse(response["ok"], response)
                if code:
                    self.assertEqual(response.get("errorCode"), code, response)
            self.assertTrue(responses[-1]["ok"], responses[-1])
            self.assertEqual(len(list((root / "collection" / "assets").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
