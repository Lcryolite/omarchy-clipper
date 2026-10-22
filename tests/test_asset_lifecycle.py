"""Protocol tests for capture-owned asset cleanup and reconciliation."""

import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get(
    "CLIPPER_PUBLIC_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
HELPER = ROOT / "build/clipper-helper"
ORIGIN = "chrome-extension://" + "a" * 32 + "/"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def environment(root: Path) -> dict[str, str]:
    value = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": str(root / "home"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QPA_PLATFORMTHEME": "",
    }
    for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        Path(value[name]).mkdir(parents=True, exist_ok=True)
    Path(value["XDG_RUNTIME_DIR"]).chmod(0o700)
    return value


def request(data_dir: Path, env: dict[str, str], value: dict) -> dict:
    process = subprocess.run(
        [str(BINARY), "--request", "--data-dir", str(data_dir),
         "--helper-bin", str(HELPER)],
        input=json.dumps(value, separators=(",", ":")).encode(),
        capture_output=True,
        env=env,
        check=False,
        timeout=20,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode(errors="replace"))
    return json.loads(process.stdout)


@contextlib.contextmanager
def database_connection(path: Path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class NativeHost:
    def __init__(self, data_dir: Path, env: dict[str, str]):
        self.process = subprocess.Popen(
            [str(BINARY), "--native", "--allowed-origin", ORIGIN, ORIGIN,
             "--data-dir", str(data_dir), "--helper-bin", str(HELPER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def call(self, value: dict) -> dict:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        self.process.stdin.write(struct.pack("=I", len(encoded)) + encoded)
        self.process.stdin.flush()
        size = struct.unpack("=I", self.process.stdout.read(4))[0]
        return json.loads(self.process.stdout.read(size))

    def close(self):
        self.process.stdin.close()
        code = self.process.wait(timeout=10)
        stderr = self.process.stderr.read().decode(errors="replace")
        self.process.stdout.close()
        self.process.stderr.close()
        if code:
            raise AssertionError(stderr)


def begin(suffix: str) -> dict:
    return {
        "op": "capture.begin", "title": suffix,
        "sourceUrl": f"https://example.test/{suffix}", "scope": "article",
        "html": "<p>Body</p>", "text": "Body", "missing": [],
    }


def asset_path(data_dir: Path, asset_id: str) -> Path:
    with database_connection(data_dir / "collection.sqlite3") as connection:
        filename = connection.execute(
            "SELECT filename FROM assets WHERE id=?", (asset_id,)
        ).fetchone()[0]
    return data_dir / "assets" / filename


def storage_snapshot(data_dir: Path) -> dict[str, tuple]:
    snapshot = {}
    for path in sorted((data_dir, *data_dir.rglob("*"))):
        relative = str(path.relative_to(data_dir)) or "."
        status = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        snapshot[relative] = (
            status.st_mode, status.st_size, status.st_mtime_ns, status.st_ctime_ns, digest,
        )
    return snapshot


def storage_metadata_snapshot(data_dir: Path) -> dict[str, tuple]:
    return {
        str(path.relative_to(data_dir)) or ".": (
            path.lstat().st_mode, path.lstat().st_size,
            path.lstat().st_mtime_ns, path.lstat().st_ctime_ns,
        )
        for path in sorted((data_dir, *data_dir.rglob("*")))
    }


def recoverable_import_intent(data_dir: Path, env: dict[str, str]) -> tuple[str, str]:
    request(data_dir, env, {"op": "draft.get"})
    database = data_dir / "collection.sqlite3"
    with database_connection(database) as connection:
        connection.execute(
            "CREATE TRIGGER fail_import_completion BEFORE DELETE ON asset_cleanup "
            "BEGIN SELECT RAISE(ABORT,'injected import completion failure'); END"
        )
    failed = request(data_dir, env, {
        "op": "asset.import", "data": PNG, "mime": "image/png",
        "name": "recoverable.png",
    })
    if failed["ok"]:
        raise AssertionError(failed)
    with database_connection(database) as connection:
        pending = connection.execute(
            "SELECT asset_id,filename FROM asset_cleanup"
        ).fetchall()
        if connection.execute("SELECT count(*) FROM assets").fetchone()[0] != 0:
            raise AssertionError("failed import committed asset metadata")
        connection.execute("DROP TRIGGER fail_import_completion")
    with database_connection(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if len(pending) != 1:
        raise AssertionError(pending)
    return pending[0]


class AssetLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (BINARY, HELPER):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")

    def test_finish_subset_removes_only_unreferenced_owned_asset(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-owned-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            host = NativeHost(data_dir, env)
            try:
                started = host.call(begin("subset"))["result"]
                assets = []
                for name in ("keep.png", "omit.png", "shared.png"):
                    imported = host.call({
                        "op": "asset.import", "data": PNG, "mime": "image/png",
                        "name": name, "captureId": started["captureId"],
                    })
                    self.assertTrue(imported["ok"], imported)
                    assets.append(imported["result"])
                draft = request(data_dir, env, {"op": "draft.get"})["result"]
                saved = request(data_dir, env, {
                    "op": "draft.save", "text": "shared", "assetIds": [assets[2]["id"]],
                    "expectedRevision": draft["revision"],
                })
                self.assertTrue(saved["ok"], saved)
                finished = host.call({
                    "op": "capture.finish", "captureId": started["captureId"],
                    "html": f'<img src="clipper://asset/{assets[0]["id"]}">',
                    "text": "Body", "assetIds": [assets[0]["id"]], "missing": [],
                })
                self.assertTrue(finished["ok"], finished)
            finally:
                host.close()
            self.assertTrue(asset_path(data_dir, assets[0]["id"]).is_file())
            self.assertFalse((data_dir / "assets" / f'{assets[1]["id"]}.png').exists())
            self.assertTrue(asset_path(data_dir, assets[2]["id"]).is_file())

    def test_incomplete_refresh_preserves_article_and_retry_reclaims_new_imports(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-refresh-assets-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            started = request(data_dir, env, begin("refresh-original"))["result"]
            original_asset = request(data_dir, env, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "original.png", "captureId": started["captureId"],
            })["result"]
            article = request(data_dir, env, {
                "op": "capture.finish", "captureId": started["captureId"],
                "html": f'<p>Original</p><img src="clipper://asset/{original_asset["id"]}">',
                "text": "Original", "assetIds": [original_asset["id"]], "missing": [],
            })["result"]
            article = request(data_dir, env, {
                "op": "note.save", "id": article["id"], "note": "Keep note",
                "expectedRevision": article["revision"],
            })["result"]

            update = begin("refresh-update")
            update["updateId"] = article["id"]
            update_started = request(data_dir, env, update)["result"]
            attempted = request(data_dir, env, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "attempted.png", "captureId": update_started["captureId"],
            })["result"]
            attempted_path = asset_path(data_dir, attempted["id"])
            incomplete = request(data_dir, env, {
                "op": "capture.finish", "captureId": update_started["captureId"],
                "html": f'<p>Partial</p><img src="clipper://asset/{attempted["id"]}">',
                "text": "Partial", "assetIds": [attempted["id"]],
                "missing": [{"url": "https://example.test/missing.png", "reason": "timeout"}],
            })["result"]
            self.assertEqual(incomplete["updateStatus"], "incomplete")
            self.assertEqual(incomplete["text"], "Original")
            self.assertEqual(incomplete["note"], "Keep note")
            self.assertEqual([item["id"] for item in incomplete["assets"]], [original_asset["id"]])
            self.assertTrue(attempted_path.is_file())

            retry = begin("refresh-retry")
            retry["updateId"] = article["id"]
            retried = request(data_dir, env, retry)
            self.assertTrue(retried["ok"], retried)
            self.assertFalse(attempted_path.exists())
            reopened = request(data_dir, env, {"op": "clip.get", "id": article["id"]})["result"]
            self.assertEqual(reopened["text"], "Original")
            self.assertEqual(reopened["note"], "Keep note")
            self.assertTrue(asset_path(data_dir, original_asset["id"]).is_file())

    def test_inspection_is_read_only_and_reconcile_rechecks_new_reference(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-reconcile-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            uncertain = request(data_dir, env, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "legacy.png",
            })["result"]
            host = NativeHost(data_dir, env)
            try:
                started = host.call(begin("failure"))["result"]
                owned = host.call({
                    "op": "asset.import", "data": PNG, "mime": "image/png",
                    "name": "owned.png", "captureId": started["captureId"],
                })["result"]
                path = asset_path(data_dir, owned["id"])
                original = path.read_bytes()
                database = data_dir / "collection.sqlite3"
                with database_connection(database) as connection:
                    connection.execute(
                        "CREATE TRIGGER fail_owned_cleanup BEFORE DELETE ON assets "
                        "BEGIN SELECT RAISE(ABORT,'injected cleanup failure'); END"
                    )
                finished = host.call({
                    "op": "capture.finish", "captureId": started["captureId"],
                    "html": "<p>No image</p>", "text": "No image",
                    "assetIds": [], "missing": [],
                })
                self.assertTrue(finished["ok"], finished)
                self.assertTrue(finished.get("cleanupPending"), finished)
            finally:
                host.close()

            path.unlink()
            path.mkdir()
            with database_connection(database) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = storage_snapshot(data_dir)
            inspected = request(data_dir, env, {"op": "asset.reconcile", "limit": 100})
            self.assertTrue(inspected["ok"], inspected)
            by_id = {item["id"]: item for item in inspected["result"]["items"]}
            self.assertEqual(by_id[uncertain["id"]]["status"], "uncertain")
            self.assertEqual(by_id[owned["id"]]["status"], "blocked")
            self.assertEqual(storage_snapshot(data_dir), before)

            path.rmdir()
            path.write_bytes(original)
            with database_connection(database) as connection:
                connection.execute("DROP TRIGGER fail_owned_cleanup")
            draft = request(data_dir, env, {"op": "draft.get"})["result"]
            saved = request(data_dir, env, {
                "op": "draft.save", "text": "new owner", "assetIds": [owned["id"]],
                "expectedRevision": draft["revision"],
            })
            self.assertEqual(saved, {
                "ok": False, "error": f'Unknown asset: {owned["id"]}',
            })
            with database_connection(database) as connection:
                connection.execute(
                    "UPDATE draft SET text='legacy concurrent owner',asset_ids=?,"
                    "revision=revision+1 WHERE id=1",
                    (json.dumps([owned["id"]]),),
                )
            reconciled = request(data_dir, env, {
                "op": "asset.reconcile", "limit": 100, "confirm": True,
            })
            self.assertTrue(reconciled["ok"], reconciled)
            self.assertIn(owned["id"], reconciled["result"]["preserved"])
            self.assertTrue(path.is_file())
            self.assertTrue(Path(uncertain["url"].removeprefix("file://")).is_file())

    def test_restart_finishes_cleanup_after_metadata_commit(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-cleanup-crash-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            imported = request(data_dir, env, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "interrupted.png",
            })["result"]
            path = Path(imported["url"].removeprefix("file://"))
            database = data_dir / "collection.sqlite3"
            with database_connection(database) as connection:
                filename = connection.execute(
                    "SELECT filename FROM assets WHERE id=?", (imported["id"],)
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO asset_cleanup(asset_id,filename,requested_at) "
                    "VALUES(?,?,datetime('now'))", (imported["id"], filename)
                )
                connection.execute("DELETE FROM assets WHERE id=?", (imported["id"],))
            with database_connection(database) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            inspected = request(data_dir, env, {"op": "asset.reconcile", "limit": 100})
            item = next(
                item for item in inspected["result"]["items"]
                if item["id"] == imported["id"]
            )
            self.assertEqual(item["status"], "eligible")
            reconciled = request(data_dir, env, {
                "op": "asset.reconcile", "limit": 100, "confirm": True,
            })
            self.assertEqual(reconciled["result"]["removed"], [imported["id"]])
            self.assertFalse(path.exists())
            repeated = request(data_dir, env, {
                "op": "asset.reconcile", "limit": 100, "confirm": True,
            })
            self.assertEqual(repeated["result"]["removed"], [])
            self.assertFalse(repeated["result"]["cleanupPending"])

    def test_failed_import_keeps_recoverable_intent_until_reconciliation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-import-intent-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            pending_id, filename = recoverable_import_intent(data_dir, env)

            inspected = request(data_dir, env, {"op": "asset.reconcile"})
            self.assertEqual(inspected["result"]["items"], [{
                "id": pending_id, "filename": filename,
                "status": "eligible",
                "reason": "Durable cleanup ownership is unreferenced",
            }])
            reconciled = request(data_dir, env, {
                "op": "asset.reconcile", "confirm": True,
            })
            self.assertEqual(reconciled["result"]["removed"], [pending_id])
            self.assertFalse(reconciled["result"]["cleanupPending"])

    def test_concurrent_reconciliation_completes_import_intent_once(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-concurrent-cleanup-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            pending_id, filename = recoverable_import_intent(data_dir, env)
            published = data_dir / "assets" / filename
            published.write_bytes(base64.b64decode(PNG))

            def reconcile(_) -> dict:
                return request(data_dir, env, {
                    "op": "asset.reconcile", "confirm": True,
                })

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                reconciled = list(executor.map(reconcile, range(4)))
            self.assertTrue(all(item["ok"] for item in reconciled), reconciled)
            self.assertTrue(all(not item["result"]["warnings"] for item in reconciled), reconciled)
            self.assertEqual(sum(
                pending_id in item["result"]["removed"] for item in reconciled
            ), 1, reconciled)
            self.assertTrue(all(
                not item["result"]["cleanupPending"] for item in reconciled
            ), reconciled)
            self.assertFalse(published.exists())

    def test_native_host_rejects_collection_path_replacement_without_redirecting(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-path-anchor-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            retained = root / "retained"
            outside = root / "outside"
            env = environment(root / "environment")
            request(outside, env, {"op": "draft.get"})
            outside_before = storage_snapshot(outside)

            host = NativeHost(data_dir, env)
            try:
                self.assertTrue(host.call({
                    "op": "capture.check", "sourceUrl": "https://example.test/anchor",
                    "scope": "article",
                })["ok"])
                data_dir.rename(retained)
                data_dir.symlink_to(outside, target_is_directory=True)

                encoded = json.dumps({
                    "op": "asset.import", "data": PNG, "mime": "image/png",
                    "name": "anchored.png",
                }, separators=(",", ":")).encode()
                host.process.stdin.write(struct.pack("=I", len(encoded)) + encoded)
                host.process.stdin.flush()
                self.assertEqual(host.process.stdout.read(4), b"")
                host.process.stdin.close()
                self.assertNotEqual(host.process.wait(timeout=10), 0)
                stderr = host.process.stderr.read()
                self.assertIn(b"symbolic link", stderr)
                self.assertEqual(storage_snapshot(outside), outside_before)
            finally:
                if host.process.poll() is None:
                    host.close()
                else:
                    host.process.stdout.close()
                    host.process.stderr.close()

    def test_inspection_of_absent_collection_creates_nothing(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-inspect-empty-") as temporary:
            root = Path(temporary)
            data_dir = root / "missing"
            env = environment(root / "environment")
            inspected = request(data_dir, env, {"op": "asset.reconcile", "limit": 25})
            self.assertEqual(inspected, {
                "ok": True,
                "result": {
                    "mode": "inspect", "items": [], "hasMore": False,
                    "nextAfter": None, "readOnly": True,
                },
            })
            self.assertFalse(data_dir.exists())

    def test_inspection_preserves_legacy_assets_without_cleanup_state(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-inspect-legacy-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            imported = request(data_dir, env, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "legacy.png",
            })["result"]
            database = data_dir / "collection.sqlite3"
            with database_connection(database) as connection:
                connection.execute("DROP TABLE asset_cleanup")
            with database_connection(database) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = storage_snapshot(data_dir)
            inspected = request(data_dir, env, {"op": "asset.reconcile"})
            by_id = {item["id"]: item for item in inspected["result"]["items"]}
            self.assertEqual(by_id[imported["id"]]["status"], "uncertain")
            self.assertEqual(storage_snapshot(data_dir), before)

    def test_inspection_rejects_active_writer_without_mutation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-active-wal-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            request(data_dir, env, {"op": "draft.get"})
            database = data_dir / "collection.sqlite3"
            with database_connection(database) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE draft SET text='active writer' WHERE id=1")
                before = storage_metadata_snapshot(data_dir)
                process = subprocess.run(
                    [str(BINARY), "--request", "--data-dir", str(data_dir)],
                    input=b'{"op":"asset.reconcile"}', capture_output=True,
                    env=env, check=False, timeout=20,
                )
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(b"SQLite connections to close", process.stderr)
                self.assertEqual(storage_metadata_snapshot(data_dir), before)

    def test_inspection_rejects_nonempty_recovery_wal_without_mutation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-recovery-wal-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            env = environment(root / "environment")
            request(data_dir, env, {"op": "draft.get"})
            wal = data_dir / "collection.sqlite3-wal"
            wal.write_bytes(b"uncheckpointed recovery data")
            wal.chmod(0o600)
            before = storage_snapshot(data_dir)
            process = subprocess.run(
                [str(BINARY), "--request", "--data-dir", str(data_dir)],
                input=b'{"op":"asset.reconcile"}', capture_output=True,
                env=env, check=False, timeout=20,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"checkpointed WAL", process.stderr)
            self.assertEqual(storage_snapshot(data_dir), before)


if __name__ == "__main__":
    unittest.main()
