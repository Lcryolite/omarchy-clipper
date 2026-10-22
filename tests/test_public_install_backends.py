"""Exercise Rust installation and lifecycle recovery in synthetic directories."""

import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts/install.py"
LIFECYCLE = ROOT / "scripts/lifecycle.py"
UNINSTALL = ROOT / "scripts/uninstall.py"
EXTENSION_ID = "a" * 32


class PublicBackendInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.prefix = self.root / "prefix"
        self.config = self.root / "config"
        self.tools = self.root / "tools"
        self._stage_source()

    def tearDown(self):
        self.temporary.cleanup()

    def _executable(self, path, version, marker):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"VERSION = {version!r}\n"
            f"MARKER = {marker!r}\n"
            "if '--version' in sys.argv:\n"
            "    print(VERSION)\n"
            "else:\n"
            "    print(MARKER)\n"
        )
        path.chmod(0o755)

    def _stage_source(self):
        (self.source / "scripts").mkdir(parents=True)
        for script in (INSTALL, LIFECYCLE, UNINSTALL):
            shutil.copy2(script, self.source / "scripts" / script.name)
        self._executable(
            self.source / "backend/target/release/omarchy-clipper-backend",
            "omarchy-clipper-backend 0.1.0",
            "rust",
        )
        self._executable(self.source / "build/clipper-reader", "0.1.0", "reader")
        self._executable(self.source / "build/clipper-helper", "0.1.0", "helper")
        self._executable(self.source / "build/clipper-native", "0.1.0", "native")

        (self.source / "extension/dist").mkdir(parents=True)
        for name in ("background.js", "content.js", "popup.js"):
            (self.source / "extension/dist" / name).write_text("// built\n")
        (self.source / "extension/manifest.json").write_text(json.dumps({
            "manifest_version": 3,
            "name": "Omarchy Clipper",
            "version": "0.1.0",
            "background": {"service_worker": "dist/background.js"},
            "action": {"default_popup": "popup.html"},
        }))
        (self.source / "extension/popup.html").write_text("<script src='dist/popup.js'></script>\n")
        (self.source / "manifest.json").write_text(json.dumps({
            "schemaVersion": 1,
            "id": "lcryolite.clipper",
            "version": "0.1.0",
        }))
        (self.source / "ui").mkdir()
        (self.source / "ui/Reader.qml").write_text("// reader\n")

        self.tools.mkdir()
        for command in ("wl-copy", "wl-paste"):
            tool = self.tools / command
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o755)

    def run_cli(self, script, *arguments, check=True):
        environment = dict(os.environ, PATH=f"{self.tools}:{os.environ.get('PATH', '')}")
        return subprocess.run(
            [
                sys.executable,
                str(self.source / "scripts" / script),
                "--prefix", str(self.prefix),
                "--config-dir", str(self.config),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=check,
            env=environment,
        )

    def state(self):
        return json.loads(
            (self.prefix / "share/omarchy-clipper/.install-state.json").read_text()
        )

    def installed_files(self):
        return {
            (name, path.relative_to(base).as_posix()): path.read_bytes()
            for name, base in (("prefix", self.prefix), ("config", self.config))
            if base.exists()
            for path in base.rglob("*")
            if path.is_file()
        }

    def installed_modes(self):
        return {
            (name, path.relative_to(base).as_posix()): stat.S_IMODE(path.stat().st_mode)
            for name, base in (("prefix", self.prefix), ("config", self.config))
            if base.exists()
            for path in base.rglob("*")
            if path.is_file()
        }

    def _ownership_record(self, path, base_name, base):
        return {
            "base": base_name,
            "path": path.relative_to(base).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def _stage_old_public_install(self):
        binary = self.prefix / "bin/clipper"
        native = self.prefix / "bin/clipper-native"
        reader_resource = self.prefix / "share/omarchy-clipper/ui/Reader.qml"
        self._executable(binary, "0.1.0", "old-public")
        native.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.source / "build/clipper-native", native)
        reader_resource.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.source / "ui/Reader.qml", reader_resource)

        origin = f"chrome-extension://{EXTENSION_ID}/"
        host = self.prefix / "bin/omarchy-clipper-host-chromium"
        host.write_text(
            "#!/bin/sh\nexec " + shlex.quote(str(binary))
            + " --native --allowed-origin " + shlex.quote(origin) + ' "$@"\n'
        )
        host.chmod(0o755)
        manifest = (
            self.config / "chromium/NativeMessagingHosts/com.omarchy.clipper.json"
        )
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({
            "name": "com.omarchy.clipper",
            "description": "Local Omarchy Clipper capture host",
            "path": str(host),
            "type": "stdio",
            "allowed_origins": [origin],
        }, indent=2) + "\n")
        manifest.chmod(0o600)

        owned = [
            self._ownership_record(path, base_name, base)
            for path, base_name, base in (
                (binary, "prefix", self.prefix),
                (native, "prefix", self.prefix),
                (reader_resource, "prefix", self.prefix),
                (host, "prefix", self.prefix),
                (manifest, "config", self.config),
            )
        ]
        state_path = self.prefix / "share/omarchy-clipper/.install-state.json"
        state_path.write_text(json.dumps({
            "version": 2,
            "resource_files": ["ui/Reader.qml"],
            "plugin_files": [],
            "owned_files": sorted(owned, key=lambda item: (item["base"], item["path"])),
        }, indent=2) + "\n")
        state_path.chmod(0o600)

    def test_old_public_install_upgrades_to_rust_and_preserves_current_data(self):
        self._stage_old_public_install()
        old_state = self.state()
        self.assertNotIn("component_files", old_state)

        collection = self.root / "collection"
        collection.mkdir()
        database = collection / "collection.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE clips (body TEXT NOT NULL)")
        connection.execute("INSERT INTO clips VALUES ('from-old-public')")
        connection.commit()
        connection.close()
        asset = collection / "assets/current.png"
        asset.parent.mkdir()
        asset.write_bytes(b"current asset")

        self.run_cli("install.py", "--chromium-extension-id", EXTENSION_ID)

        binary = self.prefix / "bin/clipper"
        reader = self.prefix / "libexec/omarchy-clipper/clipper-reader"
        helper = self.prefix / "libexec/omarchy-clipper/clipper-helper"
        native = self.prefix / "bin/clipper-native"
        lock_path = self.prefix / "share/.omarchy-clipper-backend.lock"
        self.assertEqual(subprocess.run([binary], text=True, capture_output=True, check=True).stdout, "rust\n")
        self.assertEqual(subprocess.run([reader], text=True, capture_output=True, check=True).stdout, "reader\n")
        self.assertTrue(helper.is_file())
        self.assertTrue(native.is_file())
        self.assertEqual(self.state()["component_files"], [
            "bin/clipper-native",
            "libexec/omarchy-clipper/clipper-helper",
            "libexec/omarchy-clipper/clipper-reader",
        ])
        lock_inode = lock_path.stat().st_ino
        connection = sqlite3.connect(database)
        connection.execute("INSERT INTO clips VALUES ('written-on-rust')")
        connection.commit()
        connection.close()

        self._executable(
            self.source / "backend/target/release/omarchy-clipper-backend",
            "omarchy-clipper-backend 0.1.0",
            "rust-reinstalled",
        )
        self.run_cli("install.py", "--chromium-extension-id", EXTENSION_ID)
        self.assertEqual(
            subprocess.run([binary], text=True, capture_output=True, check=True).stdout,
            "rust-reinstalled\n",
        )
        self.assertEqual(lock_path.stat().st_ino, lock_inode)
        with sqlite3.connect(database) as reopened:
            rows = [row[0] for row in reopened.execute("SELECT body FROM clips ORDER BY rowid")]
        self.assertEqual(rows, ["from-old-public", "written-on-rust"])
        self.assertEqual(asset.read_bytes(), b"current asset")

        with lock_path.open("r+b", buffering=0) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = self.run_cli("uninstall.py", check=False)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("Clipper is active", blocked.stderr)
            self.assertTrue(binary.is_file())

        self.run_cli("uninstall.py")
        for installed in (binary, reader, helper, native):
            self.assertFalse(installed.exists())
        self.assertTrue(lock_path.is_file())
        self.assertEqual(lock_path.stat().st_ino, lock_inode)
        with sqlite3.connect(database) as reopened:
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM clips").fetchone()[0], 2)
        self.assertEqual(asset.read_bytes(), b"current asset")

    def test_failed_first_upgrade_restores_old_layout_without_locking_collection(self):
        self._stage_old_public_install()
        collection = self.prefix / "share/omarchy-clipper"
        database = collection / "collection.sqlite3"
        database.write_bytes(b"old public collection")
        before = self.installed_files()

        previous_lifecycle = sys.modules.pop("lifecycle", None)
        sys.path.insert(0, str(self.source / "scripts"))
        try:
            spec = importlib.util.spec_from_file_location(
                "synthetic_first_upgrade", self.source / "scripts/install.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            original_copy = module.copy_file
            calls = 0

            def fail_second_copy(source, destination, mode=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic first-upgrade failure")
                return original_copy(source, destination, mode)

            module.copy_file = fail_second_copy
            arguments = [
                str(self.source / "scripts/install.py"),
                "--prefix", str(self.prefix),
                "--config-dir", str(self.config),
                "--chromium-extension-id", EXTENSION_ID,
            ]
            environment = {"PATH": f"{self.tools}:{os.environ.get('PATH', '')}"}
            with mock.patch.object(sys, "argv", arguments), mock.patch.dict(
                os.environ, environment
            ), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    module.main()
        finally:
            sys.path.pop(0)
            sys.modules.pop("lifecycle", None)
            if previous_lifecycle is not None:
                sys.modules["lifecycle"] = previous_lifecycle

        external_lock = self.prefix / "share/.omarchy-clipper-backend.lock"
        after = self.installed_files()
        self.assertEqual(
            {key: value for key, value in after.items()
             if key != ("prefix", "share/.omarchy-clipper-backend.lock")},
            before,
        )
        self.assertTrue(external_lock.is_file())
        self.assertFalse((collection / "backend.lock").exists())
        self.assertEqual(database.read_bytes(), b"old public collection")
        self.assertEqual(
            subprocess.run(
                [self.prefix / "bin/clipper"], text=True,
                capture_output=True, check=True,
            ).stdout,
            "old-public\n",
        )

    def test_unsafe_lifetime_lock_blocks_reinstall_without_changing_components(self):
        self.run_cli("install.py", "--chromium-extension-id", EXTENSION_ID)
        lock = self.prefix / "share/.omarchy-clipper-backend.lock"
        before = self.installed_files()
        share = self.prefix / "share"
        share.chmod(0o777)
        refused = self.run_cli(
            "install.py", "--chromium-extension-id", EXTENSION_ID, check=False
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("lock parent must be", refused.stderr)
        self.assertEqual(self.installed_files(), before)
        share.chmod(0o755)
        lock.chmod(0o644)
        refused = self.run_cli(
            "install.py", "--chromium-extension-id", EXTENSION_ID, check=False
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("private, single-link", refused.stderr)
        self.assertEqual(self.installed_files(), before)
        lock.chmod(0o600)
        alias = self.root / "lock-alias"
        os.link(lock, alias)
        refused = self.run_cli("uninstall.py", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("private, single-link", refused.stderr)
        self.assertEqual(self.installed_files(), before)

    def test_failed_component_update_restores_the_complete_installed_layout(self):
        self.run_cli("install.py", "--chromium-extension-id", EXTENSION_ID)
        before = self.installed_files()
        backend = self.source / "backend/target/release/omarchy-clipper-backend"
        backend.write_text(backend.read_text() + "# updated build\n")

        previous_lifecycle = sys.modules.pop("lifecycle", None)
        sys.path.insert(0, str(self.source / "scripts"))
        try:
            spec = importlib.util.spec_from_file_location(
                "synthetic_clipper_install", self.source / "scripts/install.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            original_copy = module.copy_file
            calls = 0

            def fail_second_copy(source, destination, mode=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic write failure")
                return original_copy(source, destination, mode)

            module.copy_file = fail_second_copy
            arguments = [
                str(self.source / "scripts/install.py"),
                "--prefix", str(self.prefix),
                "--config-dir", str(self.config),
                "--chromium-extension-id", EXTENSION_ID,
            ]
            environment = {"PATH": f"{self.tools}:{os.environ.get('PATH', '')}"}
            with mock.patch.object(sys, "argv", arguments), mock.patch.dict(
                os.environ, environment
            ), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    module.main()
        finally:
            sys.path.pop(0)
            sys.modules.pop("lifecycle", None)
            if previous_lifecycle is not None:
                sys.modules["lifecycle"] = previous_lifecycle

        self.assertEqual(self.installed_files(), before)

    def test_uninstall_refuses_changed_component_before_any_deletion(self):
        self.run_cli("install.py", "--chromium-extension-id", EXTENSION_ID)
        helper = self.prefix / "libexec/omarchy-clipper/clipper-helper"
        helper.write_bytes(helper.read_bytes() + b"\nchanged outside installer\n")
        before = self.installed_files()

        refused = self.run_cli("uninstall.py", check=False)

        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("does not match recorded ownership", refused.stderr)
        self.assertEqual(self.installed_files(), before)

    def test_uninstall_treats_missing_recorded_component_as_already_removed(self):
        self.run_cli("install.py", "--chromium-extension-id", EXTENSION_ID)
        reader = self.prefix / "libexec/omarchy-clipper/clipper-reader"
        reader.unlink()

        self.run_cli("uninstall.py")

        self.assertEqual(
            set(self.installed_files()),
            {("prefix", "share/.omarchy-clipper-backend.lock")},
        )

    def test_failed_uninstall_restores_companions_without_touching_collection(self):
        self.run_cli("install.py", "--chromium-extension-id", EXTENSION_ID)
        collection = self.root / "collection"
        collection.mkdir()
        database = collection / "collection.sqlite3"
        database.write_bytes(b"current synthetic collection")
        asset = collection / "assets/current.png"
        asset.parent.mkdir()
        asset.write_bytes(b"current synthetic asset")
        collection_before = {path.relative_to(collection): path.read_bytes()
                             for path in collection.rglob("*") if path.is_file()}
        installed_before = self.installed_files()
        installed_modes_before = self.installed_modes()

        previous_lifecycle = sys.modules.pop("lifecycle", None)
        sys.path.insert(0, str(self.source / "scripts"))
        stderr = io.StringIO()
        try:
            spec = importlib.util.spec_from_file_location(
                "synthetic_clipper_uninstall", self.source / "scripts/uninstall.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            original_unlink = Path.unlink
            calls = 0

            def fail_second_unlink(path, *arguments, **keywords):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic unlink failure")
                return original_unlink(path, *arguments, **keywords)

            arguments = [
                str(self.source / "scripts/uninstall.py"),
                "--prefix", str(self.prefix),
                "--config-dir", str(self.config),
            ]
            with mock.patch.object(sys, "argv", arguments), mock.patch.object(
                module.Path, "unlink", fail_second_unlink
            ), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as failure:
                    module.main()
            self.assertNotEqual(failure.exception.code, 0)
        finally:
            sys.path.pop(0)
            sys.modules.pop("lifecycle", None)
            if previous_lifecycle is not None:
                sys.modules["lifecycle"] = previous_lifecycle

        self.assertIn("prior installation was restored", stderr.getvalue())
        self.assertEqual(self.installed_files(), installed_before)
        self.assertEqual(self.installed_modes(), installed_modes_before)
        self.assertEqual(
            {path.relative_to(collection): path.read_bytes()
             for path in collection.rglob("*") if path.is_file()},
            collection_before,
        )

    def test_failed_explicit_purge_does_not_claim_collection_rollback(self):
        self.run_cli("install.py", "--chromium-extension-id", EXTENSION_ID)
        collection = self.root / "collection"
        asset_name = "00000000-0000-0000-0000-000000000001.png"
        asset = collection / "assets" / asset_name
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"explicit purge asset")
        database = collection / "collection.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript("""
            CREATE TABLE clips (id TEXT);
            CREATE TABLE draft (id INTEGER);
            CREATE TABLE assets (id TEXT, filename TEXT);
            CREATE TABLE clip_assets (clip_id TEXT, asset_id TEXT);
            CREATE TABLE captures (id TEXT);
        """)
        connection.execute("INSERT INTO assets VALUES (?, ?)", ("asset-id", asset_name))
        connection.commit()
        connection.close()

        previous_lifecycle = sys.modules.pop("lifecycle", None)
        sys.path.insert(0, str(self.source / "scripts"))
        stderr = io.StringIO()
        try:
            spec = importlib.util.spec_from_file_location(
                "synthetic_clipper_purge", self.source / "scripts/uninstall.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            original_unlink = Path.unlink
            purge_calls = 0

            def fail_second_purge_unlink(path, *arguments, **keywords):
                nonlocal purge_calls
                if path.is_relative_to(collection):
                    purge_calls += 1
                    if purge_calls == 2:
                        raise OSError("synthetic purge failure")
                return original_unlink(path, *arguments, **keywords)

            arguments = [
                str(self.source / "scripts/uninstall.py"),
                "--prefix", str(self.prefix),
                "--config-dir", str(self.config),
                "--purge-data-dir", str(collection),
            ]
            with mock.patch.object(sys, "argv", arguments), mock.patch.object(
                module.Path, "unlink", fail_second_purge_unlink
            ), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as failure:
                    module.main()
            self.assertNotEqual(failure.exception.code, 0)
        finally:
            sys.path.pop(0)
            sys.modules.pop("lifecycle", None)
            if previous_lifecycle is not None:
                sys.modules["lifecycle"] = previous_lifecycle

        self.assertIn("Collection removal is incomplete", stderr.getvalue())
        self.assertFalse((self.prefix / "bin/clipper").exists())
        self.assertFalse(
            (self.prefix / "share/omarchy-clipper/.install-state.json").exists()
        )
        self.assertFalse(asset.exists(), "explicit purge removals are not rolled back")
        self.assertTrue(database.exists(), "the injected purge failure leaves later data intact")


if __name__ == "__main__":
    unittest.main()
