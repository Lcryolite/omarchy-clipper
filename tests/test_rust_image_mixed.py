"""Public-process tests for Rust image-bearing Drafts and Mixed clips."""

import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "build/clipper-helper"
CANDIDATE = Path(os.environ.get(
    "CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def runtime_environment(root: Path) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
        "HOME": str(root / "home"), "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"), "TMPDIR": str(root / "tmp"),
        "QT_QPA_PLATFORM": "offscreen", "QT_QPA_PLATFORMTHEME": "",
    }
    for name in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                 "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        Path(environment[name]).mkdir(parents=True, exist_ok=True)
    Path(environment["XDG_RUNTIME_DIR"]).chmod(0o700)
    return environment


def request(binary: Path, data_dir: Path, environment: dict[str, str], value: dict) -> dict:
    arguments = [str(binary), "--request", "--data-dir", str(data_dir)]
    if binary == CANDIDATE:
        arguments.extend(("--helper-bin", str(HELPER)))
    process = subprocess.run(
        arguments, input=json.dumps(value, separators=(",", ":")).encode(),
        capture_output=True, env=environment, timeout=20, check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode(errors="replace"))
    return json.loads(process.stdout)


def candidate(data_dir: Path, environment: dict[str, str], value: dict) -> dict:
    return request(CANDIDATE, data_dir, environment, value)


def local_path(url: str) -> Path:
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc:
        raise AssertionError(url)
    return Path(unquote(parsed.path))


class RustImageMixedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (HELPER,):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")
        if "CLIPPER_RUST_BIN" not in os.environ:
            subprocess.run([sys.executable, str(ROOT / "scripts/build_rust_backend.py"),
                            "--offline"], cwd=ROOT, check=True, timeout=120)
        if not CANDIDATE.is_file():
            raise AssertionError(f"required binary is missing: {CANDIDATE}")

    def test_image_draft_commit_and_image_only_mixed_reopen(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-image-draft-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            asset = candidate(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "cover.png",
            })["result"]
            draft = candidate(data_dir, environment, {"op": "draft.get"})["result"]
            saved = candidate(data_dir, environment, {
                "op": "draft.save", "text": "Caption", "assetIds": [asset["id"]],
                "expectedRevision": draft["revision"],
            })
            self.assertTrue(saved["ok"], saved)
            self.assertEqual(saved["result"]["assets"], [asset])
            committed = candidate(data_dir, environment, {
                "op": "draft.commit", "expectedRevision": saved["result"]["revision"],
            })
            self.assertTrue(committed["ok"], committed)
            self.assertEqual(committed["result"]["assets"], [asset])
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.get", "id": committed["result"]["id"],
            }), committed)
            self.assertEqual(candidate(data_dir, environment, {"op": "draft.get"})["result"], {
                "text": "", "assetIds": [], "assets": [], "revision": 2,
            })

            image_only = candidate(data_dir, environment, {
                "op": "capture.mixed", "text": "", "assetIds": [asset["id"]],
                "title": "", "sourceUrl": "", "scope": "",
            })
            self.assertTrue(image_only["ok"], image_only)
            self.assertEqual(image_only["result"]["title"], "Image clip")
            self.assertEqual(image_only["result"]["assets"], [asset])

    def test_image_edit_cleans_only_newly_orphaned_asset(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-image-edit-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            seed = root / "seed"
            first = candidate(seed, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "first.png",
            })["result"]
            second = candidate(seed, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "second.png",
            })["result"]
            clip = candidate(seed, environment, {
                "op": "capture.mixed", "text": "Before", "assetIds": [first["id"]],
                "title": "", "sourceUrl": "", "scope": "",
            })["result"]
            first_path = local_path(first["url"])
            value = {
                "op": "clip.edit", "id": clip["id"], "text": "After",
                "assetIds": [second["id"]], "expectedRevision": clip["revision"],
            }
            actual = candidate(seed, environment, value)
            self.assertTrue(actual["ok"], actual)
            self.assertEqual(actual["result"]["text"], "After")
            self.assertEqual(actual["result"]["assets"][0]["id"], second["id"])
            self.assertFalse(seed.joinpath("assets", first_path.name).exists())
            self.assertEqual(candidate(seed, environment, {
                "op": "clip.get", "id": clip["id"],
            }), actual)

    def test_committed_edit_reports_deferred_cleanup_and_next_import_recovers_it(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-deferred-cleanup-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            orphan = candidate(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "orphan.png",
            })["result"]
            shared = candidate(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "shared.png",
            })["result"]
            replacement = candidate(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "replacement.png",
            })["result"]
            keeper = candidate(data_dir, environment, {
                "op": "capture.mixed", "text": "Keeper", "assetIds": [shared["id"]],
            })["result"]
            edited_clip = candidate(data_dir, environment, {
                "op": "capture.mixed", "text": "Before",
                "assetIds": [orphan["id"], shared["id"]],
            })["result"]
            orphan_path = local_path(orphan["url"])
            shared_path = local_path(shared["url"])
            database = data_dir / "collection.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER stall_orphan_delete BEFORE DELETE ON assets "
                    f"WHEN OLD.id='{orphan['id']}' BEGIN "
                    "SELECT max(value) FROM (WITH RECURSIVE counter(value) AS "
                    "(VALUES(0) UNION ALL SELECT value+1 FROM counter WHERE value<20000000) "
                    "SELECT value FROM counter); END"
                )

            process = subprocess.Popen(
                [str(CANDIDATE), "--request", "--data-dir", str(data_dir),
                 "--helper-bin", str(HELPER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment,
            )
            process.stdin.write(json.dumps({
                "op": "clip.edit", "id": edited_clip["id"], "text": "After",
                "assetIds": [replacement["id"]],
                "expectedRevision": edited_clip["revision"],
            }).encode())
            process.stdin.close()
            time.sleep(0.15)
            self.assertIsNone(process.poll(), "edit did not reach the cleanup transaction")
            (data_dir / "assets").chmod(0o500)
            try:
                stdout = process.stdout.read()
                stderr = process.stderr.read()
                process.wait(timeout=20)
            finally:
                (data_dir / "assets").chmod(0o700)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            process.stdout.close()
            process.stderr.close()
            response = json.loads(stdout)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["text"], "After")
            self.assertEqual(response.get("cleanupPending"), True)
            self.assertEqual(response.get("warnings"), [
                f"Could not remove deferred orphan asset: {orphan['id']}"
            ])

            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM assets WHERE id=?", (orphan["id"],)
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM assets WHERE id=?", (shared["id"],)
                ).fetchone()[0], 1)
            self.assertTrue(orphan_path.exists())
            self.assertTrue(shared_path.exists())
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.get", "id": keeper["id"],
            })["result"]["assets"][0]["id"], shared["id"])

            recovered = candidate(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "recovery-trigger.png",
            })
            self.assertTrue(recovered["ok"], recovered)
            self.assertFalse(orphan_path.exists())
            self.assertTrue(shared_path.exists())

    def test_unknown_assets_revision_conflicts_and_concurrent_image_edits_preserve_clip(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-image-conflict-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            data_dir = root / "collection"
            asset = candidate(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "shared.png",
            })["result"]
            clip = candidate(data_dir, environment, {
                "op": "capture.mixed", "text": "Original", "assetIds": [asset["id"]],
            })["result"]
            before = candidate(data_dir, environment, {"op": "clip.get", "id": clip["id"]})
            unknown = candidate(data_dir, environment, {
                "op": "clip.edit", "id": clip["id"], "text": "Invalid",
                "assetIds": ["missing-asset"], "expectedRevision": clip["revision"],
            })
            self.assertEqual(unknown, {"ok": False, "error": "Unknown asset: missing-asset"})
            self.assertEqual(candidate(data_dir, environment, {
                "op": "clip.get", "id": clip["id"],
            }), before)

            def edit(index: int) -> dict:
                return candidate(data_dir, environment, {
                    "op": "clip.edit", "id": clip["id"], "text": f"writer {index}",
                    "assetIds": [asset["id"]], "expectedRevision": clip["revision"],
                })

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(edit, range(4)))
            self.assertEqual(sum(response["ok"] for response in responses), 1, responses)
            self.assertEqual(sum(response.get("error") ==
                                 "Clip changed; reload it before saving"
                                 for response in responses), 3, responses)
            self.assertTrue(local_path(asset["url"]).is_file())


if __name__ == "__main__":
    unittest.main()
