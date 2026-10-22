"""Upgrade a real old public installation to Rust without replacing its data."""

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.test_asset_lifecycle import PNG, environment


ROOT = Path(__file__).resolve().parents[1]
RUST = ROOT / "backend/target/release/omarchy-clipper-backend"
READER = ROOT / "build/clipper-reader"
HELPER = ROOT / "build/clipper-helper"
NATIVE = ROOT / "build/clipper-native"
EXTENSION_ID = "a" * 32


class InstalledBackendRoundtripTests(unittest.TestCase):
    def test_old_public_layout_upgrades_and_rust_reinstall_preserves_current_data(self):
        with tempfile.TemporaryDirectory(prefix="clipper-installed-roundtrip-") as directory:
            root = Path(directory)
            env = environment(root / "environment")
            prefix = root / "prefix"
            env["XDG_DATA_HOME"] = str(prefix / "share")
            binary = prefix / "bin/clipper"
            data = prefix / "share/omarchy-clipper"
            config = root / "config"

            def request(executable, **value):
                arguments = [str(executable), "--request"]
                if executable == RUST:
                    arguments.extend(("--helper-bin", str(HELPER)))
                result = subprocess.run(
                    arguments, input=json.dumps(value).encode(),
                    env=env, capture_output=True, timeout=20,
                )
                self.assertEqual(
                    result.returncode, 0, result.stderr.decode(errors="replace")
                )
                response = json.loads(result.stdout)
                self.assertTrue(response["ok"], response)
                return response["result"]

            asset = request(
                RUST, op="asset.import", data=PNG, mime="image/png", name="synthetic.png"
            )
            clip = request(
                RUST, op="capture.mixed", text="Written before upgrade",
                assetIds=[asset["id"]], sourceUrl="https://example.com/",
                scope="selection",
            )
            lock = prefix / "share/.omarchy-clipper-backend.lock"
            if lock.exists():
                lock.unlink()

            binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(READER, binary)
            native = prefix / "bin/clipper-native"
            shutil.copy2(NATIVE, native)
            resource_files = []
            for source in sorted((ROOT / "ui").rglob("*")):
                if source.is_file():
                    relative = source.relative_to(ROOT / "ui")
                    destination = data / "ui" / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    resource_files.append(Path("ui") / relative)

            origin = f"chrome-extension://{EXTENSION_ID}/"
            host = prefix / "bin/omarchy-clipper-host-chromium"
            host.write_text(
                "#!/bin/sh\nexec " + shlex.quote(str(binary))
                + " --native --allowed-origin " + shlex.quote(origin) + ' "$@"\n'
            )
            host.chmod(0o755)
            host_manifest = (
                config / "chromium/NativeMessagingHosts/com.omarchy.clipper.json"
            )
            host_manifest.parent.mkdir(parents=True)
            host_manifest.write_text(json.dumps({
                "name": "com.omarchy.clipper",
                "description": "Local Omarchy Clipper capture host",
                "path": str(host),
                "type": "stdio",
                "allowed_origins": [origin],
            }, indent=2) + "\n")
            host_manifest.chmod(0o600)

            def owned(path, base_name, base):
                return {
                    "base": base_name,
                    "path": path.relative_to(base).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }

            owned_files = [
                owned(path, base_name, base)
                for path, base_name, base in (
                    (binary, "prefix", prefix),
                    (native, "prefix", prefix),
                    (host, "prefix", prefix),
                    (host_manifest, "config", config),
                    *((data / relative, "prefix", prefix) for relative in resource_files),
                )
            ]
            state_path = data / ".install-state.json"
            state_path.write_text(json.dumps({
                "version": 2,
                "resource_files": [path.as_posix() for path in resource_files],
                "plugin_files": [],
                "owned_files": sorted(
                    owned_files, key=lambda item: (item["base"], item["path"])
                ),
            }, indent=2) + "\n")
            state_path.chmod(0o600)

            def install():
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/install.py"),
                     "--prefix", str(prefix), "--config-dir", str(config),
                     "--chromium-extension-id", EXTENSION_ID],
                    env=env, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            install()
            lock_inode = lock.stat().st_ino
            reopened = request(binary, op="clip.get", id=clip["id"])
            self.assertEqual(reopened["text"], "Written before upgrade")
            self.assertEqual(reopened["assets"][0]["id"], asset["id"])
            request(binary, op="clip.trash", id=clip["id"])

            install()
            restored = request(binary, op="clip.restore", id=clip["id"])
            self.assertEqual(restored["text"], "Written before upgrade")
            self.assertEqual(restored["assets"][0]["id"], asset["id"])
            self.assertEqual(lock.stat().st_ino, lock_inode)
            self.assertTrue(
                (prefix / "libexec/omarchy-clipper/clipper-reader").is_file()
            )


if __name__ == "__main__":
    unittest.main()
