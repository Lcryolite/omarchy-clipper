"""Public-process tests for Rust Trash lifecycle and shared asset ownership."""

import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
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
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def environment(root: Path) -> dict[str, str]:
    result = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
        "HOME": str(root / "home"), "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"), "XDG_RUNTIME_DIR": str(root / "runtime"),
        "TMPDIR": str(root / "tmp"), "QT_QPA_PLATFORM": "offscreen",
        "QT_QPA_PLATFORMTHEME": "",
    }
    for key in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        Path(result[key]).mkdir(parents=True, exist_ok=True)
    Path(result["XDG_RUNTIME_DIR"]).chmod(0o700)
    return result


def call(binary: Path, data_dir: Path, env: dict[str, str], value: dict) -> dict:
    arguments = [str(binary), "--request", "--data-dir", str(data_dir)]
    if binary == CANDIDATE:
        arguments.extend(("--helper-bin", str(HELPER)))
    process = subprocess.run(
        arguments, input=json.dumps(value, separators=(",", ":")).encode(),
        capture_output=True, env=env, timeout=20, check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode(errors="replace"))
    return json.loads(process.stdout)


def rust(data_dir: Path, env: dict[str, str], value: dict) -> dict:
    return call(CANDIDATE, data_dir, env, value)


def article(data_dir: Path, env: dict[str, str]) -> dict:
    begun = rust(data_dir, env, {
        "op": "capture.begin", "title": "Article", "sourceUrl": "https://example.test/a",
        "scope": "article", "html": "<p>Body</p>", "text": "Body", "missing": [],
    })["result"]
    return rust(data_dir, env, {
        "op": "capture.finish", "captureId": begun["captureId"], "html": "<p>Body</p>",
        "text": "Body", "assetIds": [], "missing": [],
    })["result"]


class RustTrashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (HELPER,):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")
        if "CLIPPER_RUST_BIN" not in os.environ:
            subprocess.run([sys.executable, str(ROOT / "scripts/build_rust_backend.py"),
                            "--offline"], cwd=ROOT, check=True, timeout=120)

    def test_mixed_and_article_trash_restore_preserve_content_and_list_filters(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-trash-visible-") as temporary:
            root = Path(temporary)
            env = environment(root / "environment")
            data_dir = root / "collection"
            mixed = rust(data_dir, env, {
                "op": "capture.mixed", "text": "Mixed body", "assetIds": [],
            })["result"]
            captured = article(data_dir, env)
            captured = rust(data_dir, env, {
                "op": "note.save", "id": captured["id"], "note": "Retained note",
                "expectedRevision": captured["revision"],
            })["result"]
            for clip in (mixed, captured):
                trashed = rust(data_dir, env, {"op": "clip.trash", "id": clip["id"]})
                self.assertTrue(trashed["ok"], trashed)
                self.assertNotEqual(trashed["result"]["deletedAt"], "")
                self.assertEqual(rust(data_dir, env, {
                    "op": "clip.list", "trash": False,
                })["result"]["total"], 1)
                self.assertEqual(rust(data_dir, env, {
                    "op": "clip.list", "trash": True,
                })["result"]["items"][0]["id"], clip["id"])
                restored = rust(data_dir, env, {"op": "clip.restore", "id": clip["id"]})
                self.assertTrue(restored["ok"], restored)
                self.assertEqual(restored["result"]["deletedAt"], "")
                if clip["kind"] == "article":
                    self.assertEqual(restored["result"]["note"], "Retained note")
                    self.assertEqual(restored["result"]["text"], "Body")
                    self.assertEqual(restored["result"]["html"], "<p>Body</p>")
                self.assertEqual(rust(data_dir, env, {
                    "op": "clip.get", "id": clip["id"],
                })["result"], restored["result"])

    def test_empty_trash_requires_confirmation_and_preserves_shared_assets(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-trash-assets-") as temporary:
            root = Path(temporary)
            env = environment(root / "environment")
            data_dir = root / "collection"
            asset = rust(data_dir, env, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "shared.png",
            })["result"]
            clips = [rust(data_dir, env, {
                "op": "capture.mixed", "text": f"clip {index}", "assetIds": [asset["id"]],
            })["result"] for index in range(2)]
            draft = rust(data_dir, env, {"op": "draft.get"})["result"]
            rust(data_dir, env, {
                "op": "draft.save", "text": "Draft owner", "assetIds": [asset["id"]],
                "expectedRevision": draft["revision"],
            })
            self.assertEqual(rust(data_dir, env, {"op": "trash.empty", "confirm": False}), {
                "ok": False, "error": "Emptying Trash requires confirm: true",
            })
            rust(data_dir, env, {"op": "clip.trash", "id": clips[0]["id"]})
            self.assertEqual(rust(data_dir, env, {
                "op": "trash.empty", "confirm": True,
            }), {"ok": True, "result": {"removed": 1}})
            self.assertTrue(rust(data_dir, env, {
                "op": "asset.read", "id": asset["id"],
            })["ok"])
            rust(data_dir, env, {"op": "clip.trash", "id": clips[1]["id"]})
            saved = rust(data_dir, env, {"op": "draft.get"})["result"]
            rust(data_dir, env, {
                "op": "draft.save", "text": "", "assetIds": [],
                "expectedRevision": saved["revision"],
            })
            self.assertEqual(rust(data_dir, env, {
                "op": "trash.empty", "confirm": True,
            }), {"ok": True, "result": {"removed": 1}})
            self.assertFalse(rust(data_dir, env, {
                "op": "asset.read", "id": asset["id"],
            })["ok"])

    def test_concurrent_lifecycle_and_failed_empty_never_report_partial_success(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-trash-recovery-") as temporary:
            root = Path(temporary)
            env = environment(root / "environment")
            data_dir = root / "collection"
            clip = rust(data_dir, env, {
                "op": "capture.mixed", "text": "Concurrent", "assetIds": [],
            })["result"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(
                    lambda _: rust(data_dir, env, {"op": "clip.trash", "id": clip["id"]}),
                    range(4),
                ))
            self.assertEqual(sum(response["ok"] for response in responses), 1, responses)
            self.assertEqual(sum(response.get("error") == "Clip is already in Trash"
                                 for response in responses), 3, responses)
            with contextlib.closing(sqlite3.connect(data_dir / "collection.sqlite3")) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_empty BEFORE DELETE ON clips "
                    "WHEN OLD.deleted_at<>'' BEGIN SELECT RAISE(ABORT,'forced failure'); END"
                )
            failed = rust(data_dir, env, {"op": "trash.empty", "confirm": True})
            self.assertFalse(failed["ok"], failed)
            self.assertEqual(rust(data_dir, env, {
                "op": "clip.get", "id": clip["id"],
            })["result"]["deletedAt"] != "", True)
            self.assertEqual(rust(data_dir, env, {
                "op": "clip.restore", "id": clip["id"],
            })["ok"], True)
            with contextlib.closing(sqlite3.connect(data_dir / "collection.sqlite3")) as connection:
                connection.execute("DROP TRIGGER fail_empty")
            rust(data_dir, env, {"op": "clip.trash", "id": clip["id"]})
            with contextlib.closing(sqlite3.connect(data_dir / "collection.sqlite3")) as lock:
                lock.execute("BEGIN IMMEDIATE")
                process = subprocess.Popen(
                    [str(CANDIDATE), "--request", "--data-dir", str(data_dir),
                     "--helper-bin", str(HELPER)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env,
                )
                process.stdin.write(b'{"op":"trash.empty","confirm":true}')
                process.stdin.close()
                time.sleep(0.15)
                self.assertIsNone(process.poll())
                process.terminate()
                process.wait(timeout=15)
                process.stdout.close()
                process.stderr.close()
            self.assertNotEqual(rust(data_dir, env, {
                "op": "clip.get", "id": clip["id"],
            })["result"]["deletedAt"], "")

    def test_untrusted_asset_path_rolls_back_empty_trash_without_touching_external_file(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-trash-filesystem-") as temporary:
            root = Path(temporary)
            env = environment(root / "environment")
            data_dir = root / "collection"
            asset = rust(data_dir, env, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "owned.png",
            })["result"]
            clip = rust(data_dir, env, {
                "op": "capture.mixed", "text": "Owned", "assetIds": [asset["id"]],
            })["result"]
            filename = next((data_dir / "assets").iterdir()).name
            managed = data_dir / "assets" / filename
            original = managed.read_bytes()
            rust(data_dir, env, {"op": "clip.trash", "id": clip["id"]})
            managed.unlink()
            external = root / "outside"
            external.write_text("do not remove", encoding="utf-8")
            managed.symlink_to(external)
            emptied = subprocess.run(
                [str(CANDIDATE), "--request", "--data-dir", str(data_dir),
                 "--helper-bin", str(HELPER)],
                input=json.dumps({"op": "trash.empty", "confirm": True}).encode(),
                capture_output=True, env=env, timeout=20,
            )
            self.assertNotEqual(emptied.returncode, 0)
            self.assertIn(b"unsafe", emptied.stderr)
            self.assertEqual(external.read_text(encoding="utf-8"), "do not remove")
            self.assertTrue(managed.is_symlink())
            managed.unlink()
            managed.write_bytes(original)
            self.assertNotEqual(rust(data_dir, env, {
                "op": "clip.get", "id": clip["id"],
            })["result"]["deletedAt"], "")


if __name__ == "__main__":
    unittest.main()
