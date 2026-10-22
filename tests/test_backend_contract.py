"""Cross-consumer checks for the authoritative Rust backend contract."""

import concurrent.futures
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
READER = ROOT / "build/clipper-reader"
HELPER = ROOT / "build/clipper-helper"
CANDIDATE = Path(os.environ.get(
    "CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
ORIGIN = "chrome-extension://" + "a" * 32 + "/"


def isolated_environment(root: Path) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
        "HOME": str(root / "home"), "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"), "XDG_RUNTIME_DIR": str(root / "runtime"),
        "TMPDIR": str(root / "tmp"), "QT_QPA_PLATFORM": "offscreen",
        "QT_QPA_PLATFORMTHEME": "",
    }
    for name in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                 "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        Path(environment[name]).mkdir(parents=True, exist_ok=True)
    Path(environment["XDG_RUNTIME_DIR"]).chmod(0o700)
    return environment


def request(binary: Path, data_dir: Path, environment: dict[str, str], value: dict,
            extra=()) -> dict:
    arguments = [str(binary), "--request", "--data-dir", str(data_dir), *extra]
    if binary == CANDIDATE:
        arguments.extend(("--helper-bin", str(HELPER)))
    process = subprocess.run(
        arguments, input=json.dumps(value, separators=(",", ":")).encode(),
        capture_output=True, env=environment, timeout=20, check=False,
    )
    if process.returncode != 0:
        raise AssertionError(
            f"{binary} exited {process.returncode}: {process.stderr.decode(errors='replace')}"
        )
    return json.loads(process.stdout)


def rust(data_dir: Path, environment: dict[str, str], value: dict, extra=()) -> dict:
    return request(CANDIDATE, data_dir, environment, value, extra)


def reader_bridge(data_dir: Path, environment: dict[str, str], value: dict) -> dict:
    return request(READER, data_dir, environment, value,
                   ("--backend-bin", str(CANDIDATE)))


def native_message(data_dir: Path, environment: dict[str, str], value: dict) -> dict:
    encoded = json.dumps(value, separators=(",", ":")).encode()
    process = subprocess.run(
        [str(CANDIDATE), "--native", "--allowed-origin", ORIGIN, ORIGIN,
         "--data-dir", str(data_dir), "--helper-bin", str(HELPER)],
        input=struct.pack("=I", len(encoded)) + encoded,
        capture_output=True, env=environment, timeout=20, check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode(errors="replace"))
    length = struct.unpack("=I", process.stdout[:4])[0]
    return json.loads(process.stdout[4:4 + length])


class BackendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (READER, HELPER):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")
        if "CLIPPER_RUST_BIN" not in os.environ:
            subprocess.run([sys.executable, str(ROOT / "scripts/build_rust_backend.py"),
                            "--offline"], cwd=ROOT, check=True, timeout=120)
        if not CANDIDATE.is_file():
            raise AssertionError(f"required binary is missing: {CANDIDATE}")

    def test_native_privacy_boundary_rejects_local_and_reader_operations_before_storage(self):
        denied = (
            "asset.read", "clipboard.copy", "clipboard.import", "draft.get", "draft.save",
            "draft.commit", "file.import", "clip.list", "clip.get", "clip.edit",
            "clip.trash", "note.save", "theme.get", "link.open", "trash.empty",
        )
        with tempfile.TemporaryDirectory(prefix="clipper-contract-native-") as temporary:
            root = Path(temporary)
            environment = isolated_environment(root / "environment")
            for operation in denied:
                with self.subTest(operation=operation):
                    data_dir = root / operation
                    self.assertEqual(native_message(data_dir, environment, {"op": operation}), {
                        "ok": False, "error": "Operation is not available to browser clients",
                    })
                    self.assertFalse(data_dir.exists())

    def test_reader_bridge_and_direct_client_share_one_revision_conflict_domain(self):
        with tempfile.TemporaryDirectory(prefix="clipper-contract-owner-") as temporary:
            root = Path(temporary)
            environment = isolated_environment(root / "environment")
            data_dir = root / "collection"
            clip = rust(data_dir, environment, {
                "op": "capture.mixed", "text": "Original", "assetIds": [],
            })["result"]

            values = (
                (rust, "Direct panel edit"),
                (reader_bridge, "Retained Reader edit"),
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(
                    lambda item: item[0](data_dir, environment, {
                        "op": "clip.edit", "id": clip["id"], "text": item[1],
                        "assetIds": [], "expectedRevision": clip["revision"],
                    }),
                    values,
                ))
            self.assertEqual(sum(response["ok"] for response in responses), 1, responses)
            self.assertEqual(sum(response.get("error") ==
                                 "Clip changed; reload it before saving"
                                 for response in responses), 1, responses)
            winner = next(response["result"] for response in responses if response["ok"])
            self.assertEqual(rust(data_dir, environment, {
                "op": "clip.get", "id": clip["id"],
            })["result"], winner)
            self.assertEqual(reader_bridge(data_dir, environment, {
                "op": "clip.get", "id": clip["id"],
            })["result"], winner)
            self.assertEqual(list(data_dir.glob("collection.sqlite3")),
                             [data_dir / "collection.sqlite3"])

    def test_clip_open_launches_retained_reader_with_rust_backend_once(self):
        with tempfile.TemporaryDirectory(prefix="clipper-contract-reader-") as temporary:
            root = Path(temporary)
            environment = isolated_environment(root / "environment")
            data_dir = root / "collection"
            ui_dir = root / "ui"
            ui_dir.mkdir()
            clip = rust(data_dir, environment, {
                "op": "capture.mixed", "text": "Reader", "assetIds": [],
            })["result"]
            log = root / "reader-arguments.json"
            probe = root / "clipper-reader-probe"
            probe.write_text(
                "#!/usr/bin/python3\nimport json,sys\n"
                f"open({str(log)!r},'w',encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            probe.chmod(0o755)
            response = rust(data_dir, environment, {
                "op": "clip.open", "id": clip["id"],
            }, ("--reader-bin", str(probe), "--ui-dir", str(ui_dir)))
            self.assertEqual(response, {"ok": True, "result": True})
            deadline = time.monotonic() + 5
            while not log.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(log.is_file(), "retained Reader probe was not launched")
            self.assertEqual(json.loads(log.read_text(encoding="utf-8")), [
                "--data-dir", str(data_dir), "--open", clip["id"],
                "--ui-dir", str(ui_dir), "--backend-bin", str(CANDIDATE),
            ])


if __name__ == "__main__":
    unittest.main()
