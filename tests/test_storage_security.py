"""Public API tests for the collection storage trust boundary."""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import subprocess
import tempfile
import unittest

if __package__:
    from tests.test_asset_lifecycle import BINARY, NativeHost, begin, environment
else:
    from test_asset_lifecycle import BINARY, NativeHost, begin, environment


def run_request(data_dir: Path, env: dict[str, str], value: dict | None = None,
                *, umask: int = 0o022) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BINARY), "--request", "--data-dir", str(data_dir)],
        input=json.dumps(value or {"op": "draft.get"}, separators=(",", ":")).encode(),
        capture_output=True,
        env=env,
        check=False,
        timeout=20,
        preexec_fn=lambda: os.umask(umask),
    )


def response(process: subprocess.CompletedProcess) -> dict:
    if process.returncode:
        raise AssertionError(process.stderr.decode(errors="replace"))
    return json.loads(process.stdout)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StorageSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            subprocess.run(["make", "-j2"], cwd=BINARY.parents[1], check=True, timeout=120)

    def test_fresh_collection_is_private_with_a_permissive_process_umask(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-storage-") as temporary:
            root = Path(temporary)
            collection = root / "collection"
            process = run_request(collection, environment(root / "environment"), umask=0o022)

            self.assertTrue(response(process)["ok"])
            self.assertEqual(mode(collection), 0o700)
            self.assertEqual(mode(collection / "assets"), 0o700)
            for path in collection.iterdir():
                if path.is_file():
                    self.assertEqual(mode(path), 0o600, path)

    def test_replaceable_ancestor_is_rejected_before_collection_creation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-storage-") as temporary:
            root = Path(temporary)
            ancestor = root / "replaceable"
            ancestor.mkdir()
            ancestor.chmod(0o777)
            collection = ancestor / "collection"

            process = run_request(collection, environment(root / "environment"))

            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"writable ancestor", process.stderr)
            self.assertFalse(collection.exists())

    def test_linked_database_is_rejected_without_changing_the_alias_target(self):
        for link_kind in ("symbolic", "hard"):
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory(
                prefix="clipper-public-storage-"
            ) as temporary:
                root = Path(temporary)
                collection = root / "collection"
                env = environment(root / "environment")
                response(run_request(collection, env))
                database = collection / "collection.sqlite3"
                target = root / "database-target"
                if link_kind == "symbolic":
                    database.unlink()
                    target.write_bytes(b"outside database target")
                    target.chmod(0o644)
                    database.symlink_to(target)
                else:
                    os.link(database, target)
                before = (digest(target), mode(target), target.stat().st_nlink)

                process = run_request(collection, env)

                self.assertNotEqual(process.returncode, 0)
                expected = b"symbolic link" if link_kind == "symbolic" else b"hard-link alias"
                self.assertIn(expected, process.stderr)
                self.assertEqual((digest(target), mode(target), target.stat().st_nlink), before)

    def test_read_only_reconciliation_rejects_a_nonempty_rollback_journal(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-storage-") as temporary:
            root = Path(temporary)
            collection = root / "collection"
            env = environment(root / "environment")
            response(run_request(collection, env))
            journal = collection / "collection.sqlite3-journal"
            journal.write_bytes(b"synthetic rollback journal")
            journal.chmod(0o600)
            before = (digest(journal), mode(journal), journal.stat().st_mtime_ns)

            process = run_request(collection, env, {"op": "asset.reconcile"})

            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"empty rollback journal", process.stderr)
            self.assertEqual(
                (digest(journal), mode(journal), journal.stat().st_mtime_ns), before
            )

    def test_native_host_repairs_database_permissions_between_frames(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-storage-") as temporary:
            root = Path(temporary)
            collection = root / "collection"
            env = environment(root / "environment")
            host = NativeHost(collection, env)
            try:
                self.assertTrue(host.call(begin("descriptor-lifetime"))["ok"])
                database = collection / "collection.sqlite3"
                database.chmod(0o644)
                encoded = json.dumps({
                    "op": "capture.check", "sourceUrl": "https://example.test/permissions",
                    "scope": "article",
                }, separators=(",", ":")).encode()
                host.process.stdin.write(struct.pack("=I", len(encoded)) + encoded)
                host.process.stdin.flush()
                size = struct.unpack("=I", host.process.stdout.read(4))[0]
                self.assertTrue(json.loads(host.process.stdout.read(size))["ok"])
                self.assertEqual(mode(database), 0o600)
            finally:
                (collection / "collection.sqlite3").chmod(0o600)
                host.close()

    def test_native_and_request_writers_preserve_sqlite_integrity(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-storage-") as temporary:
            root = Path(temporary)
            collection = root / "collection"
            env = environment(root / "environment")
            response(run_request(collection, env))
            host = NativeHost(collection, env)
            try:
                for revision in range(6):
                    def save(index: int) -> dict:
                        return response(run_request(collection, env, {
                            "op": "draft.save",
                            "text": f"writer {revision}-{index}",
                            "assetIds": [],
                            "expectedRevision": revision,
                        }))

                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                        native = executor.submit(host.call, begin(f"concurrent-{revision}"))
                        saves = [executor.submit(save, index) for index in range(5)]
                        native_result = native.result()
                        saved = [future.result() for future in saves]
                    self.assertTrue(native_result["ok"], native_result)
                    self.assertEqual(sum(item["ok"] for item in saved), 1, saved)
                    self.assertEqual(
                        sum(item.get("error") == "Draft changed; reload it before saving"
                            for item in saved),
                        4,
                        saved,
                    )
            finally:
                host.close()

            with sqlite3.connect(collection / "collection.sqlite3") as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT revision FROM draft").fetchone()[0], 6)
                self.assertEqual(connection.execute("SELECT count(*) FROM captures").fetchone()[0], 6)

    @unittest.skipUnless(os.geteuid() == 0, "requires synthetic foreign ownership support")
    def test_foreign_owned_database_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-storage-") as temporary:
            root = Path(temporary)
            collection = root / "collection"
            env = environment(root / "environment")
            response(run_request(collection, env))
            database = collection / "collection.sqlite3"
            before = digest(database)
            os.chown(database, 65534, 65534)
            try:
                process = run_request(collection, env)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(b"not owned by the current user", process.stderr)
                self.assertEqual(digest(database), before)
                self.assertEqual(mode(database), 0o600)
            finally:
                os.chown(database, 0, 0)


if __name__ == "__main__":
    unittest.main()
