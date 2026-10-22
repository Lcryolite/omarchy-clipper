"""Public-process tests for Rust Mixed text edits and Article notes."""

import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "build/clipper-helper"
CANDIDATE = Path(os.environ.get(
    "CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
ORIGIN = "chrome-extension://" + "a" * 32 + "/"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def runtime_environment(root: Path) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": str(root / "home"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "TMPDIR": str(root / "tmp"),
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QPA_PLATFORMTHEME": "",
    }
    for name in (
        "HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TMPDIR",
    ):
        Path(environment[name]).mkdir(parents=True, exist_ok=True)
    Path(environment["XDG_RUNTIME_DIR"]).chmod(0o700)
    return environment


def arguments(binary: Path, data_dir: Path) -> list[str]:
    result = [str(binary), "--request", "--data-dir", str(data_dir)]
    if binary == CANDIDATE:
        result.extend(("--helper-bin", str(HELPER)))
    return result


def request(binary: Path, data_dir: Path, environment: dict[str, str], value: dict) -> dict:
    process = subprocess.run(
        arguments(binary, data_dir),
        input=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(
            f"{binary} exited {process.returncode}: {process.stderr.decode(errors='replace')}"
        )
    return json.loads(process.stdout)


def candidate(data_dir: Path, environment: dict[str, str], value: dict) -> dict:
    return request(CANDIDATE, data_dir, environment, value)


def seed_mixed(data_dir: Path, environment: dict[str, str], asset_ids=None) -> dict:
    return candidate(data_dir, environment, {
        "op": "capture.mixed", "text": "# Original title\nOriginal body",
        "assetIds": asset_ids or [], "title": "", "sourceUrl": "", "scope": "",
    })["result"]


def seed_article(data_dir: Path, environment: dict[str, str]) -> dict:
    begun = candidate(data_dir, environment, {
        "op": "capture.begin", "title": "Captured title",
        "sourceUrl": "https://example.test/article", "scope": "article",
        "html": "<article><p>Captured body</p></article>",
        "text": "Captured body", "missing": [],
    })["result"]
    return candidate(data_dir, environment, {
        "op": "capture.finish", "captureId": begun["captureId"],
        "html": "<article><p>Captured body</p></article>",
        "text": "Captured body", "assetIds": [], "missing": [],
    })["result"]


class RustTextEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (HELPER,):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")
        if "CLIPPER_RUST_BIN" not in os.environ:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/build_rust_backend.py"), "--offline"],
                cwd=ROOT,
                check=True,
                timeout=120,
            )
        if not CANDIDATE.is_file():
            raise AssertionError(f"required binary is missing: {CANDIDATE}")

    def test_mixed_edit_does_not_change_draft(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-edit-parity-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            clip = seed_mixed(data_dir, environment)
            draft = candidate(data_dir, environment, {
                "op": "draft.save", "text": "Unrelated Draft", "assetIds": [],
                "expectedRevision": 0,
            })
            value = {
                "op": "clip.edit", "id": clip["id"],
                "text": "### 新标题\nEdited body %_\\", "assetIds": [],
                "expectedRevision": clip["revision"],
            }
            edited = candidate(data_dir, environment, value)
            self.assertTrue(edited["ok"], edited)
            self.assertEqual(edited["result"]["text"], value["text"])
            self.assertEqual(candidate(data_dir, environment, {"op": "draft.get"}), draft)
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.get", "id": clip["id"],
            }), edited)

    def test_article_note_does_not_replace_captured_body(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-note-parity-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            article = seed_article(data_dir, environment)
            value = {
                "op": "note.save", "id": article["id"],
                "note": "My note 中文", "expectedRevision": article["revision"],
            }
            actual = candidate(data_dir, environment, value)
            self.assertTrue(actual["ok"], actual)
            self.assertEqual(actual["result"]["text"], article["text"])
            self.assertEqual(actual["result"]["html"], article["html"])
            self.assertEqual(actual["result"]["note"], "My note 中文")

    def test_concurrent_edit_conflicts_and_failed_transaction_retains_clip(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-edit-recovery-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            clip = seed_mixed(data_dir, environment)

            def edit(index: int) -> dict:
                return candidate(data_dir, environment, {
                    "op": "clip.edit", "id": clip["id"], "text": f"writer {index}",
                    "assetIds": [], "expectedRevision": clip["revision"],
                })

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(edit, range(4)))
            self.assertEqual(sum(response["ok"] for response in responses), 1, responses)
            self.assertEqual(sum(response.get("error") ==
                                 "Clip changed; reload it before saving"
                                 for response in responses), 3, responses)
            before = candidate(data_dir, environment, {"op": "clip.get", "id": clip["id"]})
            with contextlib.closing(sqlite3.connect(data_dir / "collection.sqlite3")) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_edit BEFORE UPDATE ON clips "
                    "WHEN OLD.kind='mixed' BEGIN SELECT RAISE(ABORT,'forced failure'); END"
                )
            failed = candidate(data_dir, environment, {
                "op": "clip.edit", "id": clip["id"], "text": "must roll back",
                "assetIds": [], "expectedRevision": before["result"]["revision"],
            })
            self.assertFalse(failed["ok"], failed)
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.get", "id": clip["id"],
            }), before)

    def test_wrong_kinds_fail_before_mutation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-edit-kind-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            asset = candidate(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "pixel.png",
            })["result"]
            image_clip = seed_mixed(data_dir, environment, [asset["id"]])
            article = seed_article(data_dir, environment)
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.edit", "id": article["id"], "text": "replace article",
                "assetIds": [], "expectedRevision": article["revision"],
            }), {"ok": False, "error": "Captured article bodies cannot be edited"})
            self.assertEqual(candidate(data_dir, environment, {
                "op": "note.save", "id": image_clip["id"], "note": "wrong kind",
                "expectedRevision": image_clip["revision"],
            }), {"ok": False, "error": "Only article clips have separate notes"})

    def test_native_rejects_edit_operations_and_abrupt_waiting_writer_keeps_state(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-edit-boundary-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            clip = seed_mixed(data_dir, environment)
            native = subprocess.Popen(
                [str(CANDIDATE), "--native", "--allowed-origin", ORIGIN, ORIGIN,
                 "--data-dir", str(data_dir), "--helper-bin", str(HELPER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment,
            )
            encoded = json.dumps({
                "op": "clip.edit", "id": clip["id"], "text": "browser edit",
                "assetIds": [], "expectedRevision": clip["revision"],
            }, separators=(",", ":")).encode()
            native.stdin.write(struct.pack("=I", len(encoded)) + encoded)
            native.stdin.flush()
            length = struct.unpack("=I", native.stdout.read(4))[0]
            self.assertEqual(json.loads(native.stdout.read(length)), {
                "ok": False, "error": "Operation is not available to browser clients",
            })
            native.stdin.close()
            self.assertEqual(native.wait(timeout=15), 0)
            native.stdout.close()
            native.stderr.close()

            before = candidate(data_dir, environment, {"op": "clip.get", "id": clip["id"]})
            with contextlib.closing(sqlite3.connect(data_dir / "collection.sqlite3")) as lock:
                lock.execute("BEGIN IMMEDIATE")
                process = subprocess.Popen(
                    arguments(CANDIDATE, data_dir),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment,
                )
                process.stdin.write(json.dumps({
                    "op": "clip.edit", "id": clip["id"], "text": "interrupted",
                    "assetIds": [], "expectedRevision": clip["revision"],
                }).encode())
                process.stdin.close()
                time.sleep(0.15)
                self.assertIsNone(process.poll())
                process.terminate()
                process.wait(timeout=15)
                process.stdout.close()
                process.stderr.close()
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.get", "id": clip["id"],
            }), before)


if __name__ == "__main__":
    unittest.main()
