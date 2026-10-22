"""Public runtime checks for localization and mixed-window backend routing."""

import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import struct
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUST = Path(os.environ.get(
    "CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
READER = ROOT / "build/clipper-reader"
ORIGIN = "chrome-extension://" + "a" * 32 + "/"


def environment(root: Path) -> dict[str, str]:
    values = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "LANG": "zh_CN.UTF-8",
    }
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        Path(values[name]).mkdir(parents=True, exist_ok=True)
    return values


def rust_request(env: dict[str, str], data_dir: Path, value: dict,
                 binary: Path = RUST) -> dict:
    process = subprocess.run(
        [str(binary), "--request", "--data-dir", str(data_dir)],
        input=json.dumps(value, separators=(",", ":")).encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode(errors="replace"))
    return json.loads(process.stdout)


def installed_rust_probe(root: Path) -> tuple[Path, Path, Path]:
    prefix = root / "prefix"
    binary = prefix / "bin/clipper"
    lock_path = prefix / "share/omarchy-clipper/backend.lock"
    probe = prefix / "libexec/omarchy-clipper/clipper-reader"
    binary.parent.mkdir(parents=True)
    lock_path.parent.mkdir(parents=True)
    probe.parent.mkdir(parents=True)
    shutil.copy2(RUST, binary)
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    probe.write_text(
        "#!/usr/bin/python3\n"
        "import json,os,sys,time\n"
        "with open(os.environ['READER_READY'],'w',encoding='utf-8') as output:\n"
        "    json.dump({'pid':os.getpid(),'arguments':sys.argv[1:]},output)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    return binary, lock_path, probe


def wait_for_probe(path: Path, process: subprocess.Popen | None = None) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        if process is not None and process.poll() is not None:
            raise AssertionError(process.stderr.read().decode(errors="replace"))
        time.sleep(0.01)
    raise AssertionError("Reader probe did not start")


class PublicRuntimeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (RUST, READER):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")

    def test_rust_settings_match_public_language_contract_for_request_and_native(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-settings-") as temporary:
            root = Path(temporary)
            env = environment(root)
            data_dir = root / "collection"
            self.assertEqual(rust_request(env, data_dir, {"op": "settings.get"}), {
                "ok": True,
                "result": {"language": "system", "resolvedLanguage": "zh"},
            })
            self.assertFalse(data_dir.exists())

            settings = Path(env["XDG_CONFIG_HOME"]) / "omarchy-clipper/settings.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({
                "version": 1,
                "settings": {"language": "en", "previewSide": "right"},
            }), encoding="utf-8")
            encoded = json.dumps({"op": "settings.get"}, separators=(",", ":")).encode()
            process = subprocess.run(
                [str(RUST), "--native", "--allowed-origin", ORIGIN, ORIGIN,
                 "--data-dir", str(data_dir)],
                input=struct.pack("=I", len(encoded)) + encoded,
                capture_output=True,
                env=env,
                timeout=20,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
            length = struct.unpack("=I", process.stdout[:4])[0]
            self.assertEqual(json.loads(process.stdout[4:4 + length]), {
                "ok": True,
                "result": {"language": "en", "resolvedLanguage": "en"},
            })
            self.assertFalse(data_dir.exists())

            settings.write_text(json.dumps({
                "version": 1,
                "settings": {"language": "en", "unknown": True},
            }), encoding="utf-8")
            self.assertEqual(rust_request(env, data_dir, {"op": "settings.get"}), {
                "ok": False,
                "error": "Invalid Clipper settings; repair the saved configuration.",
            })

    def test_reader_routes_mixed_clip_to_native_window_with_default_rust_backend(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-mixed-route-") as temporary:
            root = Path(temporary)
            env = environment(root)
            prefix = root / "prefix"
            reader = prefix / "libexec/omarchy-clipper/clipper-reader"
            native = prefix / "bin/clipper-native"
            backend = prefix / "bin/clipper"
            arguments_log = root / "native-arguments.json"
            request_log = root / "backend-request.json"
            reader.parent.mkdir(parents=True)
            native.parent.mkdir(parents=True)
            backend.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(READER, reader)
            lock = prefix / "share/omarchy-clipper/backend.lock"
            lock.parent.mkdir(parents=True)
            lock.touch(mode=0o600)
            lock.chmod(0o600)
            backend.write_text(
                "#!/usr/bin/python3\n"
                "import json,os,sys\n"
                "value=json.load(sys.stdin)\n"
                "open(os.environ['BACKEND_LOG'],'w',encoding='utf-8').write(json.dumps(value))\n"
                "print(json.dumps({'ok':True,'result':{'id':value.get('id'),'kind':'mixed'}}))\n",
                encoding="utf-8",
            )
            native.write_text(
                "#!/usr/bin/python3\n"
                "import json,os,sys\n"
                "open(os.environ['NATIVE_LOG'],'w',encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            backend.chmod(0o755)
            native.chmod(0o755)
            env["BACKEND_LOG"] = str(request_log)
            env["NATIVE_LOG"] = str(arguments_log)
            data_dir = root / "collection"
            ui_dir = root / "ui"
            process = subprocess.run(
                [str(reader), "--open", "mixed-id", "--data-dir", str(data_dir),
                 "--ui-dir", str(ui_dir)],
                capture_output=True,
                env=env,
                timeout=20,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
            self.assertEqual(json.loads(request_log.read_text(encoding="utf-8")), {
                "op": "clip.get", "id": "mixed-id",
            })
            self.assertEqual(json.loads(arguments_log.read_text(encoding="utf-8")), [
                "--open", "mixed-id", "--data-dir", str(data_dir),
                "--ui-dir", str(ui_dir),
            ])
            self.assertFalse((data_dir / "collection.sqlite3").exists())

    def test_reader_request_diagnostic_forwards_to_default_rust_backend(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-reader-request-") as temporary:
            root = Path(temporary)
            env = environment(root)
            data_dir = root / "collection"
            created = rust_request(env, data_dir, {
                "op": "capture.mixed",
                "text": "Reader bridge",
                "assetIds": [],
                "sourceUrl": "https://example.test/reader-bridge",
                "scope": "selection",
            })
            process = subprocess.run(
                [str(READER), "--request", "--data-dir", str(data_dir)],
                input=json.dumps({
                    "op": "clip.get", "id": created["result"]["id"],
                }).encode(),
                capture_output=True,
                env=env,
                timeout=20,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
            response = json.loads(process.stdout)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["text"], "Reader bridge")
            theme = subprocess.run(
                [str(READER), "--request", "--data-dir", str(data_dir)],
                input=b'{"op":"theme.get"}',
                capture_output=True,
                env=env,
                timeout=20,
                check=False,
            )
            self.assertEqual(theme.returncode, 0, theme.stderr.decode(errors="replace"))
            theme_response = json.loads(theme.stdout)
            self.assertTrue(theme_response["ok"], theme_response)
            self.assertIn("background", theme_response["result"])

    def test_reader_does_not_implement_native_messaging(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-reader-native-") as temporary:
            root = Path(temporary)
            env = environment(root)
            data_dir = root / "collection"
            process = subprocess.run(
                [str(READER), "--native", "--data-dir", str(data_dir)],
                capture_output=True,
                env=env,
                timeout=20,
                check=False,
            )
            self.assertEqual(process.returncode, 2)
            self.assertIn(b"provided by the Rust backend", process.stderr)
            self.assertFalse(data_dir.exists())

    def test_installed_rust_open_hands_lifetime_lock_to_exec_reader(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-rust-exec-lock-") as temporary:
            root = Path(temporary)
            env = environment(root)
            binary, lock_path, probe = installed_rust_probe(root)
            marker = root / "reader.json"
            env["READER_READY"] = str(marker)
            process = subprocess.Popen(
                [str(binary), "--open", "probe", "--reader-bin", str(probe),
                 "--data-dir", str(root / "collection")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                reader = wait_for_probe(marker, process)
                self.assertEqual(reader["pid"], process.pid)
                with lock_path.open("rb", buffering=0) as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
                process.stdout.close()
                process.stderr.close()

    def test_installed_rust_clip_open_hands_lifetime_lock_to_detached_reader(self):
        with tempfile.TemporaryDirectory(prefix="clipper-public-rust-spawn-lock-") as temporary:
            root = Path(temporary)
            env = environment(root)
            binary, lock_path, probe = installed_rust_probe(root)
            marker = root / "reader.json"
            env["READER_READY"] = str(marker)
            data_dir = root / "collection"
            created = rust_request(env, data_dir, {
                "op": "capture.mixed",
                "text": "Detached lock probe",
                "assetIds": [],
                "sourceUrl": "https://example.test/lock-probe",
                "scope": "selection",
            }, binary)
            self.assertTrue(created["ok"], created)
            request = subprocess.run(
                [str(binary), "--request", "--data-dir", str(data_dir),
                 "--reader-bin", str(probe)],
                input=json.dumps({
                    "op": "clip.open", "id": created["result"]["id"],
                }).encode(),
                capture_output=True,
                env=env,
                timeout=20,
                check=False,
            )
            self.assertEqual(request.returncode, 0, request.stderr.decode(errors="replace"))
            self.assertTrue(json.loads(request.stdout)["ok"])
            reader = wait_for_probe(marker)
            try:
                with lock_path.open("rb", buffering=0) as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                try:
                    os.kill(reader["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass

if __name__ == "__main__":
    unittest.main()
