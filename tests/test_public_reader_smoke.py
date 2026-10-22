"""Run the real public Article Reader against an isolated Rust Collection."""

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUST = Path(os.environ.get(
    "CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
READER = ROOT / "build/clipper-reader"
HELPER = ROOT / "build/clipper-helper"
UI_WRAPPER = ROOT / "tests/gui-public-smoke"
BODY = "Public Reader WebEngine sentinel"
NOTE = "Saved through the real Reader note editor"
REOPENED_NOTE = NOTE + " after reopening"


def isolated_environment(root):
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "TMPDIR": str(root / "tmp"),
        "LANG": "C.UTF-8",
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QUICK_BACKEND": "software",
        "QT_FORCE_STDERR_LOGGING": "1",
        # Test subprocesses use only synthetic local content. CI may run as root,
        # where Chromium refuses to start its normal sandbox.
        "QTWEBENGINE_DISABLE_SANDBOX": "1",
        "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --disable-gpu --disable-dev-shm-usage",
        "QSG_RHI_BACKEND": "software",
    }
    for name in (
        "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR", "TMPDIR",
    ):
        Path(environment[name]).mkdir(parents=True, exist_ok=True)
    Path(environment["XDG_RUNTIME_DIR"]).chmod(0o700)
    return environment


def request(binary, data_dir, environment, value, *, backend=None):
    arguments = [str(binary), "--request", "--data-dir", str(data_dir)]
    if binary == RUST:
        arguments.extend(("--helper-bin", str(HELPER)))
    if backend is not None:
        arguments.extend(("--backend-bin", str(backend)))
    completed = subprocess.run(
        arguments,
        input=json.dumps(value, separators=(",", ":")).encode(),
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode(errors="replace"))
    return json.loads(completed.stdout)


def reader_process(data_dir, environment, clip_id, mode):
    arguments = [
        str(READER),
        "--open", clip_id,
        "--backend-bin", str(RUST),
        "--data-dir", str(data_dir),
        "--ui-dir", str(UI_WRAPPER),
        "--smoke-mode", mode,
        "--smoke-body", BODY,
        "--smoke-note", NOTE,
        "--smoke-verify-note", REOPENED_NOTE,
    ]
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=40)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=3)
        raise AssertionError(
            "Reader smoke timed out; stderr:\n" + stderr.decode(errors="replace")
        )
    output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
    if process.returncode != 0:
        raise AssertionError(
            f"Reader smoke exited {process.returncode}; output:\n{output}"
        )
    for marker in ("GUI_SMOKE_BODY_MATCHES:", "GUI_SMOKE_NOTE_SAVED",
                   "GUI_SMOKE_CLOSE_CLICKED"):
        if marker not in output:
            raise AssertionError(f"Reader exited before {marker}; output:\n{output}")
    return output


class PublicReaderSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (RUST, READER, HELPER):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")

    def test_article_body_note_persistence_and_close_button(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-reader-") as temporary:
            root = Path(temporary)
            environment = isolated_environment(root / "environment")
            data_dir = root / "collection"
            begun = request(RUST, data_dir, environment, {
                "op": "capture.begin",
                "title": "Public Reader smoke",
                "sourceUrl": "https://example.test/public-reader-smoke",
                "scope": "article",
                "html": f"<h1>{BODY}</h1><p>Offline captured body.</p>",
                "text": f"{BODY} Offline captured body.",
                "missing": [],
            })
            self.assertTrue(begun["ok"], begun)
            finished = request(RUST, data_dir, environment, {
                "op": "capture.finish",
                "captureId": begun["result"]["captureId"],
                "html": f"<h1>{BODY}</h1><p>Offline captured body.</p>",
                "text": f"{BODY} Offline captured body.",
                "assetIds": [],
                "missing": [],
            })
            self.assertTrue(finished["ok"], finished)
            clip_id = finished["result"]["id"]

            reader_process(data_dir, environment, clip_id, "edit")

            rust_clip = request(
                RUST, data_dir, environment, {"op": "clip.get", "id": clip_id}
            )
            self.assertTrue(rust_clip["ok"], rust_clip)
            self.assertEqual(rust_clip["result"]["note"], NOTE)

            reader_process(data_dir, environment, clip_id, "verify")
            reopened = request(
                RUST, data_dir, environment, {"op": "clip.get", "id": clip_id}
            )
            self.assertTrue(reopened["ok"], reopened)
            self.assertEqual(reopened["result"]["note"], REOPENED_NOTE)


if __name__ == "__main__":
    unittest.main()
