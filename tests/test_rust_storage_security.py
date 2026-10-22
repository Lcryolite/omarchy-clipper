"""Public-boundary tests for the Rust Collection storage trust boundary."""

import concurrent.futures
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
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
    }
    for name in (
        "HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TMPDIR",
    ):
        Path(environment[name]).mkdir(parents=True, exist_ok=True)
    Path(environment["XDG_RUNTIME_DIR"]).chmod(0o700)
    return environment


def run_request(data_dir: Path, environment: dict[str, str], *, umask: int = 0o022,
                value: dict | None = None):
    return subprocess.run(
        [str(CANDIDATE), "--request", "--data-dir", str(data_dir)],
        input=json.dumps(value or {"op": "draft.get"}, separators=(",", ":")).encode(),
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
        preexec_fn=lambda: os.umask(umask),
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RustStorageSecurityTests(unittest.TestCase):
    def test_installed_resources_can_share_collection(self):
        with tempfile.TemporaryDirectory(prefix="clipper-installed-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            collection.mkdir(mode=0o700)
            (collection / ".install-state.json").write_text('{}')
            (collection / "ui").mkdir()
            (collection / "extension").mkdir()
            process = run_request(collection, environment)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            self.assertTrue(json.loads(process.stdout)["ok"])
            inspected = run_request(collection, environment, value={"op": "asset.reconcile"})
            self.assertEqual(inspected.returncode, 0, inspected.stderr.decode())
            self.assertTrue(json.loads(inspected.stdout)["ok"])

    def test_internal_backend_lock_is_rejected_as_unrelated(self):
        with tempfile.TemporaryDirectory(prefix="clipper-installed-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            collection.mkdir(mode=0o700)
            (collection / ".install-state.json").write_text('{}')
            lock = collection / "backend.lock"
            lock.touch(mode=0o600)

            process = run_request(collection, environment)

            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"contains unrelated entry backend.lock", process.stderr)

    @classmethod
    def setUpClass(cls):
        if "CLIPPER_RUST_BIN" not in os.environ:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/build_rust_backend.py"), "--offline"],
                cwd=ROOT,
                check=True,
                timeout=120,
            )
        if not CANDIDATE.is_file():
            raise AssertionError(f"required binary is missing: {CANDIDATE}")

    def test_fresh_collection_is_private_independently_of_umask(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"

            process = run_request(collection, environment, umask=0o022)

            self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
            self.assertTrue(json.loads(process.stdout)["ok"])
            self.assertEqual(mode(collection), 0o700)
            self.assertEqual(mode(collection / "assets"), 0o700)
            for path in collection.iterdir():
                if path.is_file():
                    self.assertEqual(mode(path), 0o600, path)

    def test_replaceable_ancestor_is_rejected_without_creating_collection(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            replaceable = root / "replaceable"
            replaceable.mkdir()
            replaceable.chmod(0o777)
            collection = replaceable / "collection"

            process = run_request(collection, environment)

            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"writable ancestor", process.stderr)
            self.assertFalse(collection.exists())

    def test_linked_storage_is_rejected_without_changing_link_targets(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")

            outside_directory = root / "outside-directory"
            outside_directory.mkdir(mode=0o755)
            sentinel = outside_directory / "sentinel"
            sentinel.write_bytes(b"outside must stay unchanged")
            sentinel.chmod(0o644)
            collection_link = root / "collection-link"
            collection_link.symlink_to(outside_directory, target_is_directory=True)
            before = (digest(sentinel), mode(sentinel), mode(outside_directory))

            linked_root = run_request(collection_link, environment)

            self.assertNotEqual(linked_root.returncode, 0)
            self.assertIn(b"symbolic link", linked_root.stderr)
            self.assertEqual((digest(sentinel), mode(sentinel), mode(outside_directory)), before)

            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            database = collection / "collection.sqlite3"
            database.unlink()
            outside_database = root / "outside.sqlite3"
            outside_database.write_bytes(b"database target")
            outside_database.chmod(0o644)
            database.symlink_to(outside_database)
            target_before = (digest(outside_database), mode(outside_database))

            linked_database = run_request(collection, environment)

            self.assertNotEqual(linked_database.returncode, 0)
            self.assertIn(b"symbolic link", linked_database.stderr)
            self.assertEqual((digest(outside_database), mode(outside_database)), target_before)

    def test_hardlinked_database_is_rejected_without_changing_alias(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            database = collection / "collection.sqlite3"
            alias = root / "database-alias"
            os.link(database, alias)
            before = (digest(alias), mode(alias), alias.stat().st_nlink)

            process = run_request(collection, environment)

            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"hard-link alias", process.stderr)
            self.assertEqual((digest(alias), mode(alias), alias.stat().st_nlink), before)

    def test_linked_sqlite_sidecars_are_rejected_without_changing_targets(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())

            wal = collection / "collection.sqlite3-wal"
            wal.unlink(missing_ok=True)
            symlink_target = root / "outside-wal"
            symlink_target.write_bytes(b"outside WAL target")
            symlink_target.chmod(0o644)
            symlink_before = (digest(symlink_target), mode(symlink_target))
            wal.symlink_to(symlink_target)

            linked_wal = run_request(collection, environment)

            self.assertNotEqual(linked_wal.returncode, 0)
            self.assertIn(b"symbolic link", linked_wal.stderr)
            self.assertEqual((digest(symlink_target), mode(symlink_target)), symlink_before)

            wal.unlink()
            shm = collection / "collection.sqlite3-shm"
            shm.unlink(missing_ok=True)
            hardlink_target = root / "outside-shm"
            hardlink_target.write_bytes(b"outside SHM target")
            hardlink_target.chmod(0o644)
            os.link(hardlink_target, shm)
            hardlink_before = (
                digest(hardlink_target),
                mode(hardlink_target),
                hardlink_target.stat().st_nlink,
            )

            linked_shm = run_request(collection, environment)

            self.assertNotEqual(linked_shm.returncode, 0)
            self.assertIn(b"hard-link alias", linked_shm.stderr)
            self.assertEqual(
                (
                    digest(hardlink_target),
                    mode(hardlink_target),
                    hardlink_target.stat().st_nlink,
                ),
                hardlink_before,
            )

    def test_linked_asset_storage_is_rejected_without_changing_targets(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            assets = collection / "assets"
            assets.rmdir()
            outside = root / "outside-assets"
            outside.mkdir(mode=0o755)
            sentinel = outside / "sentinel"
            sentinel.write_bytes(b"asset target")
            sentinel.chmod(0o644)
            before = (digest(sentinel), mode(sentinel), mode(outside))
            assets.symlink_to(outside, target_is_directory=True)

            linked_directory = run_request(collection, environment)

            self.assertNotEqual(linked_directory.returncode, 0)
            self.assertIn(b"symbolic link", linked_directory.stderr)
            self.assertEqual((digest(sentinel), mode(sentinel), mode(outside)), before)

            assets.unlink()
            assets.mkdir(mode=0o700)
            managed = assets / "managed.png"
            managed.write_bytes(b"managed bytes")
            managed.chmod(0o644)
            alias = root / "asset-alias"
            os.link(managed, alias)
            alias_before = (digest(alias), mode(alias), alias.stat().st_nlink)

            linked_file = run_request(collection, environment)

            self.assertNotEqual(linked_file.returncode, 0)
            self.assertIn(b"hard-link alias", linked_file.stderr)
            self.assertEqual((digest(alias), mode(alias), alias.stat().st_nlink), alias_before)

    def test_valid_existing_collection_is_restricted_without_format_migration(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            database = collection / "collection.sqlite3"
            assets = collection / "assets"
            retained = assets / "retained.bin"
            retained.write_bytes(b"retained bytes")
            collection.chmod(0o755)
            assets.chmod(0o755)
            database.chmod(0o644)
            retained.chmod(0o644)
            database_before = digest(database)

            reopened = run_request(collection, environment)

            self.assertEqual(reopened.returncode, 0, reopened.stderr.decode(errors="replace"))
            self.assertTrue(json.loads(reopened.stdout)["ok"])
            self.assertEqual(mode(collection), 0o700)
            self.assertEqual(mode(assets), 0o700)
            self.assertEqual(mode(database), 0o600)
            self.assertEqual(mode(retained), 0o600)
            self.assertEqual(retained.read_bytes(), b"retained bytes")
            self.assertEqual(digest(database), database_before)

    def test_default_reconciliation_inspection_does_not_change_storage(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())

            def snapshot() -> dict[str, tuple[int, int, int, str]]:
                return {
                    str(path.relative_to(collection)): (
                        stat.S_IMODE(path.stat(follow_symlinks=False).st_mode),
                        path.stat(follow_symlinks=False).st_size,
                        path.stat(follow_symlinks=False).st_mtime_ns,
                        digest(path),
                    )
                    for path in collection.rglob("*") if path.is_file()
                }

            before = snapshot()
            inspected = run_request(
                collection,
                environment,
                value={"op": "asset.reconcile", "limit": 100},
            )

            self.assertEqual(inspected.returncode, 0, inspected.stderr.decode(errors="replace"))
            self.assertTrue(json.loads(inspected.stdout)["result"]["readOnly"])
            self.assertEqual(snapshot(), before)

    def test_absent_reconciliation_inspection_creates_nothing(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "missing" / "collection"

            inspected = run_request(
                collection,
                environment,
                value={"op": "asset.reconcile"},
            )

            self.assertEqual(inspected.returncode, 0, inspected.stderr.decode(errors="replace"))
            self.assertEqual(json.loads(inspected.stdout), {
                "ok": True,
                "result": {
                    "mode": "inspect",
                    "items": [],
                    "hasMore": False,
                    "nextAfter": None,
                    "readOnly": True,
                },
            })
            self.assertFalse((root / "missing").exists())

    def test_reconciliation_rejects_active_wal_without_changing_files(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            connection = sqlite3.connect(collection / "collection.sqlite3")
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE draft SET text='pending writer' WHERE id=1")
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                wal = collection / "collection.sqlite3-wal"
                self.assertGreater(wal.stat().st_size, 0)
                self.assertEqual(mode(wal), 0o600)
                self.assertEqual(mode(collection / "collection.sqlite3-shm"), 0o600)
                before = {
                    path.name: (mode(path), path.stat().st_size, path.stat().st_mtime_ns)
                    for path in collection.iterdir() if path.is_file()
                }

                inspected = run_request(
                    collection,
                    environment,
                    value={"op": "asset.reconcile"},
                )

                self.assertNotEqual(inspected.returncode, 0)
                self.assertIn(b"SQLite connections to close", inspected.stderr)
                self.assertEqual({
                    path.name: (mode(path), path.stat().st_size, path.stat().st_mtime_ns)
                    for path in collection.iterdir() if path.is_file()
                }, before)
            finally:
                connection.rollback()
                connection.close()

    def test_reconciliation_rejects_writer_before_wal_has_content(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            connection = sqlite3.connect(collection / "collection.sqlite3")
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE draft SET text='uncommitted writer' WHERE id=1")
                wal = collection / "collection.sqlite3-wal"
                self.assertTrue(not wal.exists() or wal.stat().st_size == 0)
                before = {
                    path.name: (mode(path), path.stat().st_size, path.stat().st_mtime_ns)
                    for path in collection.iterdir() if path.is_file()
                }

                inspected = run_request(
                    collection,
                    environment,
                    value={"op": "asset.reconcile"},
                )

                self.assertNotEqual(inspected.returncode, 0)
                self.assertIn(b"SQLite connections to close", inspected.stderr)
                self.assertEqual({
                    path.name: (mode(path), path.stat().st_size, path.stat().st_mtime_ns)
                    for path in collection.iterdir() if path.is_file()
                }, before)
            finally:
                connection.rollback()
                connection.close()

    def test_reconciliation_rejects_nonempty_rollback_journal_without_changes(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            journal = collection / "collection.sqlite3-journal"
            journal.write_bytes(b"synthetic rollback journal")
            journal.chmod(0o600)
            before = (digest(journal), mode(journal), journal.stat().st_mtime_ns)

            inspected = run_request(
                collection,
                environment,
                value={"op": "asset.reconcile"},
            )

            self.assertNotEqual(inspected.returncode, 0)
            self.assertIn(b"requires an empty rollback journal", inspected.stderr)
            self.assertEqual(
                (digest(journal), mode(journal), journal.stat().st_mtime_ns),
                before,
            )

    def test_collection_path_replacement_after_open_cannot_redirect_writes(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            outside = root / "outside"
            for path in (collection, outside):
                initialized = run_request(path, environment)
                self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())

            def tree_snapshot(path: Path) -> dict[str, tuple[str, int, int]]:
                return {
                    str(item.relative_to(path)): (
                        digest(item), mode(item), item.stat().st_mtime_ns,
                    )
                    for item in path.rglob("*") if item.is_file()
                }

            outside_before = tree_snapshot(outside)
            locker = sqlite3.connect(collection / "collection.sqlite3", timeout=0)
            locker.execute("PRAGMA journal_mode=WAL")
            locker.execute("BEGIN IMMEDIATE")
            database_stat = (collection / "collection.sqlite3").stat()
            database_identity = (database_stat.st_dev, database_stat.st_ino)
            process = subprocess.Popen(
                [str(CANDIDATE), "--request", "--data-dir", str(collection)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps({
                "op": "draft.save",
                "text": "write through held Collection",
                "assetIds": [],
                "expectedRevision": 0,
            }, separators=(",", ":")).encode())
            process.stdin.close()
            process.stdin = None
            deadline = time.monotonic() + 2
            opened = False
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    descriptors = list(Path(f"/proc/{process.pid}/fd").iterdir())
                except (FileNotFoundError, PermissionError):
                    break
                identities = []
                for descriptor in descriptors:
                    try:
                        metadata = descriptor.stat()
                    except (FileNotFoundError, PermissionError):
                        continue
                    identities.append((metadata.st_dev, metadata.st_ino))
                if database_identity in identities:
                    opened = True
                    break
                time.sleep(0.01)
            if not opened:
                locker.rollback()
                locker.close()
                stdout, stderr = process.communicate(timeout=10)
                self.fail(
                    "backend did not reach the controlled open/use boundary: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )

            retained = root / "retained"
            collection.rename(retained)
            collection.symlink_to(outside, target_is_directory=True)
            locker.rollback()
            locker.close()
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            self.assertTrue(json.loads(stdout)["ok"])
            self.assertEqual(tree_snapshot(outside), outside_before)
            self.assertEqual(json.loads(run_request(
                retained,
                environment,
                value={"op": "draft.get"},
            ).stdout)["result"]["text"], "write through held Collection")

    def test_concurrent_rust_writers_share_sqlite_locking(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())

            def save(binary: Path, revision: int, index: int) -> dict:
                process = subprocess.run(
                    [str(binary), "--request", "--data-dir", str(collection)],
                    input=json.dumps({
                        "op": "draft.save",
                        "text": f"writer {revision}-{index}",
                        "assetIds": [],
                        "expectedRevision": revision,
                    }, separators=(",", ":")).encode(),
                    capture_output=True,
                    env=environment,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(
                    process.returncode, 0, process.stderr.decode(errors="replace")
                )
                return json.loads(process.stdout)

            for revision in range(4):
                binaries = (CANDIDATE,) * 4
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    responses = list(executor.map(
                        lambda pair: save(pair[1], revision, pair[0]),
                        enumerate(binaries),
                    ))
                self.assertEqual(sum(response["ok"] for response in responses), 1, responses)
                self.assertEqual(
                    sum(response.get("error") ==
                        "Draft changed; reload it before saving"
                        for response in responses),
                    3,
                    responses,
                )

            rust = json.loads(run_request(
                collection, environment, value={"op": "draft.get"}
            ).stdout)
            final = save(CANDIDATE, 4, 4)
            self.assertTrue(final["ok"], final)
            self.assertEqual(final["result"]["revision"], 5)
            self.assertEqual(rust["result"]["revision"], 4)

    @unittest.skipUnless(os.geteuid() == 0, "requires synthetic foreign ownership support")
    def test_foreign_owned_database_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-storage-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            collection = root / "collection"
            initialized = run_request(collection, environment)
            self.assertEqual(initialized.returncode, 0, initialized.stderr.decode())
            database = collection / "collection.sqlite3"
            before = digest(database)
            os.chown(database, 65534, 65534)
            try:
                process = run_request(collection, environment)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(b"not owned by the current user", process.stderr)
                self.assertEqual(digest(database), before)
                self.assertEqual(mode(database), 0o600)
            finally:
                os.chown(database, 0, 0)


if __name__ == "__main__":
    unittest.main()
