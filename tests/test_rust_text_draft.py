"""Public-process tests for Rust text Draft and Mixed clip persistence."""

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
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "build/clipper-helper"
CANDIDATE = Path(os.environ.get(
    "CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))


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


def request(binary: Path, data_dir: Path, environment: dict[str, str], value: dict) -> dict:
    arguments = [str(binary), "--request", "--data-dir", str(data_dir)]
    if binary == CANDIDATE:
        arguments.extend(("--helper-bin", str(HELPER)))
    process = subprocess.run(
        arguments,
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


class RustTextDraftTests(unittest.TestCase):
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

    def test_draft_save_restart_and_conflict(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-draft-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            rust_dir = root / "collection"
            initial = {"text": "", "assetIds": [], "assets": [], "revision": 0}
            self.assertEqual(candidate(rust_dir, environment, {"op": "draft.get"}),
                             {"ok": True, "result": initial})
            value = {
                "op": "draft.save", "text": "# 标题\nDraft body %_\\",
                "assetIds": [], "expectedRevision": 0,
            }
            actual = candidate(rust_dir, environment, value)
            self.assertTrue(actual["ok"], actual)
            self.assertEqual(actual["result"]["text"], value["text"])
            self.assertEqual(actual["result"]["revision"], 1)
            self.assertEqual(candidate(rust_dir, environment, {"op": "draft.get"}), actual)
            conflict = candidate(rust_dir, environment, value)
            self.assertEqual(conflict, {
                "ok": False, "error": "Draft changed; reload it before saving",
            })
            self.assertEqual(candidate(rust_dir, environment, {"op": "draft.get"}), actual)

    def test_fresh_read_requests_initialize_only_the_missing_collection(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-fresh-read-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            list_dir = root / "list"
            self.assertEqual(candidate(list_dir, environment, {"op": "clip.list"}), {
                "ok": True, "result": {"items": [], "total": 0},
            })
            self.assertTrue((list_dir / "collection.sqlite3").is_file())
            check_dir = root / "native-check"
            native = subprocess.Popen(
                [str(CANDIDATE), "--native", "--allowed-origin",
                 "chrome-extension://" + "a" * 32 + "/",
                 "chrome-extension://" + "a" * 32 + "/",
                 "--data-dir", str(check_dir), "--helper-bin", str(HELPER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment,
            )
            encoded = json.dumps({
                "op": "capture.check", "sourceUrl": "https://example.test/fresh",
                "scope": "article",
            }, separators=(",", ":")).encode()
            native.stdin.write(struct.pack("=I", len(encoded)) + encoded)
            native.stdin.flush()
            length = struct.unpack("=I", native.stdout.read(4))[0]
            self.assertEqual(json.loads(native.stdout.read(length)), {
                "ok": True, "result": {"existing": None},
            })
            native.stdin.close()
            self.assertEqual(native.wait(timeout=15), 0)
            native.stdout.close()
            native.stderr.close()
            self.assertTrue((check_dir / "collection.sqlite3").is_file())

            broken = root / "broken"
            broken.mkdir()
            (broken / "collection.sqlite3").write_text("not sqlite", encoding="utf-8")
            process = subprocess.run(
                arguments := [str(CANDIDATE), "--request", "--data-dir", str(broken),
                              "--helper-bin", str(HELPER)],
                input=b'{"op":"clip.list"}', capture_output=True, env=environment,
                timeout=15, check=False,
            )
            self.assertTrue(
                process.returncode != 0 or not json.loads(process.stdout)["ok"],
                (arguments, process.stdout, process.stderr),
            )
            self.assertEqual((broken / "collection.sqlite3").read_text(encoding="utf-8"),
                             "not sqlite")

    def test_commit_is_atomic_clears_draft_and_preserves_unrelated_capture(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-commit-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            begun = candidate(data_dir, environment, {
                "op": "capture.begin", "title": "Pending", "sourceUrl": "https://example.test/p",
                "scope": "article", "html": "<p>Body</p>", "text": "Body", "missing": [],
            })
            self.assertTrue(begun["ok"], begun)
            saved = candidate(data_dir, environment, {
                "op": "draft.save", "text": "## Saved title\nBody",
                "assetIds": [], "expectedRevision": 0,
            })
            self.assertTrue(saved["ok"], saved)
            committed = candidate(data_dir, environment, {
                "op": "draft.commit", "expectedRevision": saved["result"]["revision"],
            })
            self.assertTrue(committed["ok"], committed)
            clip = committed["result"]
            self.assertEqual((clip["kind"], clip["title"], clip["text"], clip["revision"]),
                             ("mixed", "Saved title", "## Saved title\nBody", 1))
            self.assertEqual(candidate(data_dir, environment, {"op": "draft.get"})["result"], {
                "text": "", "assetIds": [], "assets": [], "revision": 2,
            })
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.get", "id": clip["id"],
            }), committed)
            pending = candidate(data_dir, environment, {"op": "capture.pending"})
            self.assertEqual(pending["result"]["items"][0]["captureId"],
                             begun["result"]["captureId"])

            with contextlib.closing(sqlite3.connect(data_dir / "collection.sqlite3")) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_mixed BEFORE INSERT ON clips "
                    "WHEN NEW.kind='mixed' BEGIN SELECT RAISE(ABORT,'forced failure'); END"
                )
            failed_saved = candidate(data_dir, environment, {
                "op": "draft.save", "text": "Retained after failure",
                "assetIds": [], "expectedRevision": 2,
            })
            failed = candidate(data_dir, environment, {
                "op": "draft.commit", "expectedRevision": 3,
            })
            self.assertFalse(failed["ok"], failed)
            self.assertEqual(candidate(data_dir, environment, {"op": "draft.get"}), failed_saved)

    def test_concurrent_draft_save_never_overwrites_and_text_mixed_reopens(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-text-concurrency-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            candidate(data_dir, environment, {"op": "draft.get"})

            def save(index: int) -> dict:
                return candidate(data_dir, environment, {
                    "op": "draft.save", "text": f"writer {index}",
                    "assetIds": [], "expectedRevision": 0,
                })

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(save, range(4)))
            self.assertEqual(sum(response["ok"] for response in responses), 1, responses)
            self.assertEqual(sum(response.get("error") ==
                                 "Draft changed; reload it before saving"
                                 for response in responses), 3, responses)

            mixed = candidate(data_dir, environment, {
                "op": "capture.mixed", "text": "Mixed from browser",
                "assetIds": [], "title": "", "sourceUrl": "https://example.test/source",
                "scope": "selection",
            })
            self.assertTrue(mixed["ok"], mixed)
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.get", "id": mixed["result"]["id"],
            }), mixed)

    def test_invalid_asset_lists_fail_before_mutation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-draft-assets-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            initial = candidate(data_dir, environment, {"op": "draft.get"})
            for value in (
                {"op": "draft.save", "text": "image", "assetIds": "asset",
                 "expectedRevision": 0},
                {"op": "capture.mixed", "text": "image", "assetIds": [""]},
            ):
                response = candidate(data_dir, environment, value)
                self.assertFalse(response["ok"], response)
                self.assertIn("assetIds", response["error"])
            self.assertEqual(candidate(data_dir, environment, {"op": "draft.get"}), initial)


if __name__ == "__main__":
    unittest.main()
