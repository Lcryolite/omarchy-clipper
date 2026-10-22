"""Public-boundary tests for Rust-managed image assets."""

import base64
import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import struct
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "build/clipper-helper"
CANDIDATE = Path(os.environ.get(
    "CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
ORIGIN = "chrome-extension://" + "a" * 32 + "/"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
ANIMATED_GIF = base64.b64encode(
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    b"\x02\x02D\x01\x00"
    b"!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    b"\x02\x02D\x01\x00;"
).decode()


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


def request(binary: Path, data_dir: Path, environment: dict[str, str], value: dict,
            helper: Path | None = None, timeout: int = 20) -> dict:
    arguments = [str(binary), "--request", "--data-dir", str(data_dir)]
    if helper is not None:
        arguments.extend(("--helper-bin", str(helper)))
    process = subprocess.run(
        arguments,
        input=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
        capture_output=True,
        env=environment,
        timeout=timeout,
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(
            f"{binary} exited {process.returncode}: {process.stderr.decode(errors='replace')}"
        )
    return json.loads(process.stdout)


def request_process(binary: Path, data_dir: Path, environment: dict[str, str], value: dict,
                    helper: Path | None = None, timeout: int = 20) -> subprocess.CompletedProcess:
    arguments = [str(binary), "--request", "--data-dir", str(data_dir)]
    if helper is not None:
        arguments.extend(("--helper-bin", str(helper)))
    return subprocess.run(
        arguments,
        input=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
        capture_output=True,
        env=environment,
        timeout=timeout,
        check=False,
    )


@contextlib.contextmanager
def database_connection(path: Path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def candidate_request(data_dir: Path, environment: dict[str, str], value: dict,
                      helper: Path = HELPER) -> dict:
    return request(CANDIDATE, data_dir, environment, value, helper)


def local_asset_path(url: str) -> Path:
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc:
        raise AssertionError(f"unexpected local asset URL: {url}")
    return Path(unquote(parsed.path))


def stored_asset_path(data_dir: Path, asset_id: str) -> Path:
    with database_connection(data_dir / "collection.sqlite3") as connection:
        filename = connection.execute(
            "SELECT filename FROM assets WHERE id=?", (asset_id,)
        ).fetchone()[0]
    return data_dir / "assets" / filename


def native_request(data_dir: Path, environment: dict[str, str], value: dict) -> dict:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    process = subprocess.run(
        [str(CANDIDATE), "--native", "--allowed-origin", ORIGIN, ORIGIN,
         "--data-dir", str(data_dir), "--helper-bin", str(HELPER)],
        input=struct.pack("=I", len(encoded)) + encoded,
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode(errors="replace"))
    if len(process.stdout) < 4:
        raise AssertionError("short native response header")
    length = struct.unpack("=I", process.stdout[:4])[0]
    body = process.stdout[4:4 + length]
    if len(body) != length or len(process.stdout) != length + 4:
        raise AssertionError("short or trailing native response")
    return json.loads(body)


def capture_begin_request(suffix: str, update_id: str = "") -> dict:
    value = {
        "op": "capture.begin",
        "title": f"Asset ownership {suffix}",
        "sourceUrl": f"https://example.test/asset-ownership/{suffix}",
        "scope": "article body",
        "html": "<p>Captured body</p>",
        "text": "Captured body",
        "missing": [],
    }
    if update_id:
        value["updateId"] = update_id
    return value


class RustAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (HELPER,):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")
        if "CLIPPER_RUST_BIN" not in os.environ:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/build_rust_backend.py"), "--offline"],
                cwd=ROOT, check=True, timeout=120,
            )
        if not CANDIDATE.is_file():
            raise AssertionError(f"required binary is missing: {CANDIDATE}")

    def test_import_is_retrievable_after_restart_without_path_escape(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection with spaces"
            environment = runtime_environment(root / "environment")

            imported = candidate_request(data_dir, environment, {
                "op": "asset.import",
                "data": PNG,
                "mime": "image/png",
                "name": "../../outside portrait.png",
            })
            self.assertTrue(imported["ok"], imported)
            asset = imported["result"]
            self.assertRegex(asset["id"], r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
            self.assertEqual(asset["mime"], "image/png")
            self.assertEqual(asset["name"], "outside portrait.png")
            path = local_asset_path(asset["url"])
            self.assertEqual(path.parent.resolve(), (data_dir / "assets").resolve())
            self.assertEqual(path.read_bytes(), base64.b64decode(PNG, validate=True))

            draft = candidate_request(data_dir, environment, {"op": "draft.get"})["result"]
            saved = candidate_request(data_dir, environment, {
                "op": "draft.save",
                "text": "Managed image",
                "assetIds": [asset["id"]],
                "expectedRevision": draft["revision"],
            })["result"]
            clip = candidate_request(data_dir, environment, {
                "op": "draft.commit", "expectedRevision": saved["revision"],
            })["result"]
            rust_reopened = candidate_request(data_dir, environment, {
                "op": "clip.get", "id": clip["id"],
            })
            self.assertTrue(rust_reopened["ok"], rust_reopened)
            for field in ("id", "kind", "text", "status"):
                self.assertEqual(rust_reopened["result"][field], clip[field])
            self.assertEqual(rust_reopened["result"]["assets"], [asset])

    def test_decoder_contract_and_error_codes(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-validation-") as temporary:
            root = Path(temporary)
            environment = runtime_environment(root / "environment")
            candidate_dir = root / "candidate"
            cases = (
                {"op": "asset.import", "data": "not base64!", "mime": "image/png"},
                {"op": "asset.import", "data": "aW52YWxpZA==", "mime": "image/png"},
                {"op": "asset.import", "data": PNG, "mime": "image/jpeg"},
                {"op": "asset.import", "data": PNG + "=", "mime": "image/png"},
                {"op": "asset.import", "data": PNG[:-3] + " " + PNG[-2:], "mime": "image/png"},
                {"op": "asset.import", "data": PNG.replace("+", "-"), "mime": "image/png"},
                {
                    "op": "asset.import",
                    "data": base64.b64encode(b"P6\n1 1\n255\n\xff\x00\x00").decode(),
                    "mime": "image/x-portable-pixmap",
                },
                {"op": "asset.import", "mime": "image/png"},
                {"op": "asset.import", "data": PNG},
            )
            for value in cases:
                with self.subTest(value={key: str(item)[:32] for key, item in value.items()}):
                    rejected = candidate_request(candidate_dir, environment, value)
                    self.assertFalse(rejected["ok"], rejected)
                    self.assertIn("error", rejected)

            for data in (PNG.removesuffix("="), PNG[:-2] + "J="):
                with self.subTest(accepted_base64=data[-8:]):
                    candidate = candidate_request(candidate_dir, environment, {
                        "op": "asset.import", "data": data,
                        "mime": "image/png", "name": "compatible.png",
                    })
                    self.assertTrue(candidate["ok"], candidate)
                    self.assertEqual(
                        local_asset_path(candidate["result"]["url"]).read_bytes(),
                        base64.b64decode(PNG),
                    )

            animated = candidate_request(candidate_dir, environment, {
                "op": "asset.import", "data": ANIMATED_GIF,
                "mime": "image/gif", "name": "animation.gif",
            })
            self.assertTrue(animated["ok"], animated)
            self.assertEqual(animated["result"]["mime"], "image/gif")
            self.assertEqual(animated["result"]["name"], "animation.gif")

            oversized_data = base64.b64encode(b"\0" * (12 * 1024 * 1024 + 1)).decode()
            oversized_request = {
                "op": "asset.import", "data": oversized_data, "mime": "image/png",
            }
            oversized = candidate_request(candidate_dir, environment, oversized_request)
            self.assertFalse(oversized["ok"], oversized)
            self.assertEqual(oversized["error"], "Asset exceeds the 12 MiB limit")

    def test_concurrent_and_native_imports_publish_complete_unique_assets(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-concurrent-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            value = {
                "op": "asset.import", "data": PNG,
                "mime": "image/png", "name": "same.png",
            }
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                imported = list(executor.map(
                    lambda _: candidate_request(data_dir, environment, value), range(6)
                ))
            self.assertTrue(all(response["ok"] for response in imported), imported)
            ids = {response["result"]["id"] for response in imported}
            paths = {local_asset_path(response["result"]["url"]) for response in imported}
            self.assertEqual(len(ids), 6)
            self.assertEqual(len(paths), 6)
            self.assertTrue(all(
                path.read_bytes() == base64.b64decode(PNG, validate=True) for path in paths
            ))
            self.assertFalse(any(
                path.name.endswith(".importing") for path in (data_dir / "assets").iterdir()
            ))

            native = native_request(data_dir, environment, value)
            self.assertTrue(native["ok"], native)
            self.assertNotIn("url", native["result"])
            self.assertEqual(native["result"]["mime"], "image/png")
            self.assertEqual(native["result"]["name"], "same.png")

    def test_native_capture_import_abort_retry_supersedes_orphans_and_finishes(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-capture-assets-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            begun = native_request(
                data_dir, environment, capture_begin_request("interrupted")
            )["result"]
            first = native_request(data_dir, environment, {
                "op": "asset.import",
                "data": PNG,
                "mime": "image/png",
                "name": "interrupted.png",
                "captureId": begun["captureId"],
            })
            self.assertTrue(first["ok"], first)
            self.assertNotIn("url", first["result"])
            first_id = first["result"]["id"]
            database = data_dir / "collection.sqlite3"
            with sqlite3.connect(database) as connection:
                encoded, filename = connection.execute(
                    "SELECT captures.asset_ids,assets.filename FROM captures "
                    "JOIN assets ON assets.id=? WHERE captures.id=?",
                    (first_id, begun["captureId"]),
                ).fetchone()
            self.assertEqual(json.loads(encoded), [first_id])
            first_path = data_dir / "assets" / filename
            self.assertTrue(first_path.is_file())

            aborted = native_request(data_dir, environment, {
                "op": "capture.abort",
                "captureId": begun["captureId"],
                "reason": "browser capture was interrupted",
            })
            self.assertTrue(aborted["ok"], aborted)
            rejected = native_request(data_dir, environment, {
                "op": "asset.import",
                "data": PNG,
                "mime": "image/png",
                "name": "too-late.png",
                "captureId": begun["captureId"],
            })
            self.assertEqual(rejected, {"ok": False, "error": "Capture not found"})
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM assets").fetchone()[0], 1
                )

            retried = native_request(
                data_dir,
                environment,
                capture_begin_request("retry", begun["clipId"]),
            )
            self.assertTrue(retried["ok"], retried)
            self.assertFalse(first_path.exists())
            with sqlite3.connect(database) as connection:
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM assets WHERE id=?", (first_id,)
                ).fetchone())

            replacement = native_request(data_dir, environment, {
                "op": "asset.import",
                "data": PNG,
                "mime": "image/png",
                "name": "replacement.png",
                "captureId": retried["result"]["captureId"],
            })
            self.assertTrue(replacement["ok"], replacement)
            replacement_id = replacement["result"]["id"]
            finished = native_request(data_dir, environment, {
                "op": "capture.finish",
                "captureId": retried["result"]["captureId"],
                "html": f'<p>Recovered</p><img src="clipper://asset/{replacement_id}">',
                "text": "Recovered",
                "assetIds": [replacement_id],
                "missing": [],
            })
            self.assertTrue(finished["ok"], finished)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT asset_id FROM clip_assets WHERE clip_id=?",
                    (begun["clipId"],),
                ).fetchall(), [(replacement_id,)])
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM captures WHERE clip_id=?",
                    (begun["clipId"],),
                ).fetchone()[0], 0)

    def test_capture_disappearing_during_import_rolls_back_metadata_and_file(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-capture-asset-race-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            begun = native_request(
                data_dir, environment, capture_begin_request("disappears")
            )["result"]
            database = data_dir / "collection.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER remove_capture_after_asset_insert "
                    "AFTER INSERT ON assets BEGIN "
                    "DELETE FROM captures WHERE id='" + begun["captureId"] + "'; END"
                )

            failed = native_request(data_dir, environment, {
                "op": "asset.import",
                "data": PNG,
                "mime": "image/png",
                "name": "rolled-back.png",
                "captureId": begun["captureId"],
            })
            self.assertEqual(failed, {"ok": False, "error": "Capture not found"})
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT asset_ids,status FROM captures WHERE id=?",
                    (begun["captureId"],),
                ).fetchone(), ("[]", "pending"))
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM assets"
                ).fetchone()[0], 0)
            self.assertEqual(list((data_dir / "assets").iterdir()), [])

            missing = candidate_request(root / "missing-capture", environment, {
                "op": "asset.import",
                "data": PNG,
                "mime": "image/png",
                "name": "unknown-capture.png",
                "captureId": "missing-capture",
            })
            self.assertEqual(missing, {"ok": False, "error": "Capture not found"})
            self.assertEqual(list((root / "missing-capture" / "assets").iterdir()), [])

    def test_failed_metadata_write_recovers_temporary_but_preserves_uncertain_file(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-recovery-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            candidate_request(data_dir, environment, {"op": "draft.get"})
            database = data_dir / "collection.sqlite3"
            assets = data_dir / "assets"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_asset_insert BEFORE INSERT ON assets "
                    "BEGIN SELECT RAISE(ABORT,'injected asset failure'); END"
                )

            failed = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
            })
            self.assertFalse(failed["ok"], failed)
            self.assertEqual(list(assets.iterdir()), [])
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM assets").fetchone()[0], 0)
                connection.execute("DROP TRIGGER fail_asset_insert")

            interrupted_id = "00000000-0000-4000-8000-000000000001"
            uncertain_final = assets / f"{interrupted_id}.png"
            interrupted_temporary = assets / f".{interrupted_id}.png.importing"
            unrelated = assets / "leave-me-alone.txt"
            uncertain_final.write_bytes(b"ownership unknown")
            interrupted_temporary.write_bytes(b"partial")
            unrelated.write_bytes(b"unmanaged")

            recovered = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
            })
            self.assertTrue(recovered["ok"], recovered)
            self.assertEqual(uncertain_final.read_bytes(), b"ownership unknown")
            self.assertFalse(interrupted_temporary.exists())
            self.assertEqual(unrelated.read_bytes(), b"unmanaged")

    def test_import_intent_recovers_synthetic_crash_with_concurrent_reconcile(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-import-intent-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            candidate_request(data_dir, environment, {"op": "draft.get"})
            database = data_dir / "collection.sqlite3"
            assets = data_dir / "assets"
            with database_connection(database) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_import_completion BEFORE DELETE ON asset_cleanup "
                    "BEGIN SELECT RAISE(ABORT,'injected import completion failure'); END"
                )

            failed = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "recoverable.png",
            })
            self.assertFalse(failed["ok"], failed)
            with database_connection(database) as connection:
                pending = connection.execute(
                    "SELECT asset_id,filename FROM asset_cleanup"
                ).fetchall()
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM assets"
                ).fetchone()[0], 0)
                connection.execute("DROP TRIGGER fail_import_completion")
            self.assertEqual(len(pending), 1)

            asset_id, filename = pending[0]
            published = assets / filename
            published.write_bytes(base64.b64decode(PNG))
            uncertain = assets / "00000000-0000-4000-8000-000000000001.png"
            uncertain.write_bytes(b"ownership unknown")

            def reconcile(_) -> dict:
                return candidate_request(data_dir, environment, {
                    "op": "asset.reconcile", "limit": 100, "confirm": True,
                })

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(reconcile, range(4)))

            self.assertTrue(all(item["ok"] for item in responses), responses)
            self.assertEqual(sum(
                asset_id in item["result"]["removed"] for item in responses
            ), 1, responses)
            self.assertFalse(published.exists())
            self.assertEqual(uncertain.read_bytes(), b"ownership unknown")
            with database_connection(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM asset_cleanup"
                ).fetchone()[0], 0)

    def test_assets_directory_symlink_cannot_escape_collection(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-confinement-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            outside = root / "outside"
            outside.mkdir()
            environment = runtime_environment(root / "environment")
            candidate_request(data_dir, environment, {"op": "draft.get"})
            (data_dir / "assets").rmdir()
            (data_dir / "assets").symlink_to(outside, target_is_directory=True)

            process = request_process(CANDIDATE, data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
            }, HELPER)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"is a symbolic link", process.stderr)
            self.assertEqual(list(outside.iterdir()), [])

    def test_untrusted_helper_output_and_unwritable_storage_publish_nothing(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-failures-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            candidate_request(data_dir, environment, {"op": "draft.get"})
            assets = data_dir / "assets"
            fake_helper = root / "fake-helper"
            fake_helper.write_text(
                "#!/bin/sh\n"
                "while IFS= read -r line; do :; done\n"
                "printf '%s' '{\"ok\":true,\"result\":{\"mime\":\"image/png\","
                "\"extension\":\"../../escape\",\"width\":1,\"height\":1}}'\n"
            )
            fake_helper.chmod(0o700)
            malicious = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
            }, helper=fake_helper)
            self.assertFalse(malicious["ok"], malicious)
            self.assertEqual(list(assets.iterdir()), [])
            self.assertFalse((root / "escape").exists())

            assets.chmod(0o500)
            repaired = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
            })
            self.assertTrue(repaired["ok"], repaired)
            self.assertEqual(assets.stat().st_mode & 0o777, 0o700)
            with database_connection(data_dir / "collection.sqlite3") as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM assets").fetchone()[0], 1)

    def test_managed_read_is_bounded_and_rejects_untrusted_storage_paths(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-read-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            imported = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "read.png",
            })["result"]
            asset_id = imported["id"]
            asset_path = local_asset_path(imported["url"])
            database = data_dir / "collection.sqlite3"

            read = candidate_request(data_dir, environment, {
                "op": "asset.read", "id": asset_id,
            })
            self.assertEqual(read, {
                "ok": True,
                "result": {"data": PNG, "mime": "image/png"},
            })
            rejected_native = native_request(data_dir, environment, {
                "op": "asset.read", "id": asset_id,
            })
            self.assertFalse(rejected_native["ok"], rejected_native)

            with database_connection(database) as connection:
                connection.execute(
                    "UPDATE assets SET filename=? WHERE id=?", ("../outside.png", asset_id)
                )
            traversal = candidate_request(data_dir, environment, {
                "op": "asset.read", "id": asset_id,
            })
            self.assertFalse(traversal["ok"], traversal)

            with database_connection(database) as connection:
                connection.execute(
                    "UPDATE assets SET filename=?,mime=? WHERE id=?",
                    (asset_path.name, "image/jpeg", asset_id),
                )
            mismatch = candidate_request(data_dir, environment, {
                "op": "asset.read", "id": asset_id,
            })
            self.assertFalse(mismatch["ok"], mismatch)

            with database_connection(database) as connection:
                connection.execute(
                    "UPDATE assets SET mime=? WHERE id=?", ("image/png", asset_id)
                )
            original = asset_path.read_bytes()
            asset_path.unlink()
            outside = root / "outside.png"
            outside.write_bytes(original)
            asset_path.symlink_to(outside)
            process = request_process(CANDIDATE, data_dir, environment, {
                "op": "asset.read", "id": asset_id,
            }, HELPER)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"is a symbolic link", process.stderr)
            self.assertEqual(outside.read_bytes(), original)
            asset_path.unlink()

            asset_path.write_bytes(b"x" * (12 * 1024 * 1024 + 1))
            oversized = candidate_request(data_dir, environment, {
                "op": "asset.read", "id": asset_id,
            })
            self.assertFalse(oversized["ok"], oversized)

    def test_capture_finish_reclaims_only_omitted_owned_assets(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-owned-assets-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            begun = native_request(
                data_dir, environment, capture_begin_request("finish-subset")
            )["result"]
            imported = []
            for name in ("retained.png", "omitted.png", "shared.png"):
                response = native_request(data_dir, environment, {
                    "op": "asset.import", "data": PNG, "mime": "image/png",
                    "name": name, "captureId": begun["captureId"],
                })
                self.assertTrue(response["ok"], response)
                imported.append(response["result"])

            draft = candidate_request(data_dir, environment, {"op": "draft.get"})["result"]
            saved = candidate_request(data_dir, environment, {
                "op": "draft.save", "text": "shared elsewhere",
                "assetIds": [imported[2]["id"]], "expectedRevision": draft["revision"],
            })
            self.assertTrue(saved["ok"], saved)

            finished = native_request(data_dir, environment, {
                "op": "capture.finish", "captureId": begun["captureId"],
                "html": f'<p>Body</p><img src="clipper://asset/{imported[0]["id"]}">',
                "text": "Body", "assetIds": [imported[0]["id"]], "missing": [],
            })
            self.assertTrue(finished["ok"], finished)
            self.assertTrue(stored_asset_path(data_dir, imported[0]["id"]).is_file())
            omitted_path = data_dir / "assets" / f'{imported[1]["id"]}.png'
            self.assertFalse(omitted_path.exists())
            self.assertTrue(stored_asset_path(data_dir, imported[2]["id"]).is_file())

            with database_connection(data_dir / "collection.sqlite3") as connection:
                ids = {row[0] for row in connection.execute("SELECT id FROM assets")}
                self.assertEqual(ids, {imported[0]["id"], imported[2]["id"]})
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM asset_cleanup"
                ).fetchone()[0], 0)

    def test_incomplete_refresh_preserves_article_and_retry_reclaims_new_imports(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-refresh-assets-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            started = candidate_request(
                data_dir, environment, capture_begin_request("refresh-original")
            )["result"]
            original_asset = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "original.png", "captureId": started["captureId"],
            })["result"]
            article = candidate_request(data_dir, environment, {
                "op": "capture.finish", "captureId": started["captureId"],
                "html": f'<p>Original</p><img src="clipper://asset/{original_asset["id"]}">',
                "text": "Original", "assetIds": [original_asset["id"]], "missing": [],
            })["result"]
            article = candidate_request(data_dir, environment, {
                "op": "note.save", "id": article["id"], "note": "Keep note",
                "expectedRevision": article["revision"],
            })["result"]

            update_started = candidate_request(
                data_dir, environment,
                capture_begin_request("refresh-update", article["id"]),
            )["result"]
            attempted = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "attempted.png", "captureId": update_started["captureId"],
            })["result"]
            attempted_path = stored_asset_path(data_dir, attempted["id"])
            incomplete = candidate_request(data_dir, environment, {
                "op": "capture.finish", "captureId": update_started["captureId"],
                "html": f'<p>Partial</p><img src="clipper://asset/{attempted["id"]}">',
                "text": "Partial", "assetIds": [attempted["id"]],
                "missing": [{"url": "https://example.test/missing.png", "reason": "timeout"}],
            })["result"]
            self.assertEqual(incomplete["updateStatus"], "incomplete")
            self.assertEqual(incomplete["text"], "Original")
            self.assertEqual(incomplete["note"], "Keep note")
            self.assertEqual(
                [item["id"] for item in incomplete["assets"]], [original_asset["id"]]
            )
            self.assertTrue(attempted_path.is_file())

            retried = candidate_request(
                data_dir, environment,
                capture_begin_request("refresh-retry", article["id"]),
            )
            self.assertTrue(retried["ok"], retried)
            self.assertFalse(attempted_path.exists())
            reopened = candidate_request(data_dir, environment, {
                "op": "clip.get", "id": article["id"],
            })["result"]
            self.assertEqual(reopened["text"], "Original")
            self.assertEqual(reopened["note"], "Keep note")
            self.assertTrue(stored_asset_path(data_dir, original_asset["id"]).is_file())

    def test_reconciliation_is_read_only_by_default_and_revalidates_references(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-reconcile-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            uncertain = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "legacy-uncertain.png",
            })["result"]
            begun = native_request(
                data_dir, environment, capture_begin_request("cleanup-retry")
            )["result"]
            owned = native_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "owned.png", "captureId": begun["captureId"],
            })["result"]
            owned_path = stored_asset_path(data_dir, owned["id"])
            owned_bytes = owned_path.read_bytes()
            database = data_dir / "collection.sqlite3"
            with database_connection(database) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_owned_cleanup BEFORE DELETE ON assets "
                    "BEGIN SELECT RAISE(ABORT,'injected cleanup failure'); END"
                )
            finished = native_request(data_dir, environment, {
                "op": "capture.finish", "captureId": begun["captureId"],
                "html": "<p>No image retained</p>", "text": "No image retained",
                "assetIds": [], "missing": [],
            })
            self.assertTrue(finished["ok"], finished)
            self.assertTrue(finished.get("cleanupPending"), finished)

            owned_path.unlink()
            owned_path.mkdir()
            owned_path.chmod(0o700)
            with database_connection(database) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = database.stat().st_mtime_ns
            inspected = candidate_request(data_dir, environment, {
                "op": "asset.reconcile", "limit": 100,
            })
            self.assertTrue(inspected["ok"], inspected)
            by_id = {item["id"]: item for item in inspected["result"]["items"]}
            self.assertEqual(by_id[uncertain["id"]]["status"], "uncertain")
            self.assertEqual(by_id[owned["id"]]["status"], "blocked")
            self.assertTrue(owned_path.is_dir())
            self.assertEqual(database.stat().st_mtime_ns, before)

            owned_path.rmdir()
            owned_path.write_bytes(owned_bytes)
            with database_connection(database) as connection:
                connection.execute("DROP TRIGGER fail_owned_cleanup")
            draft = candidate_request(data_dir, environment, {"op": "draft.get"})["result"]
            saved = candidate_request(data_dir, environment, {
                "op": "draft.save", "text": "concurrent owner",
                "assetIds": [owned["id"]], "expectedRevision": draft["revision"],
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
            reconciled = candidate_request(data_dir, environment, {
                "op": "asset.reconcile", "limit": 100, "confirm": True,
            })
            self.assertTrue(reconciled["ok"], reconciled)
            self.assertIn(owned["id"], reconciled["result"]["preserved"])
            self.assertTrue(owned_path.is_file())
            self.assertTrue(local_asset_path(uncertain["url"]).is_file())
            with database_connection(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM asset_cleanup"
                ).fetchone()[0], 0)

    def test_reconciliation_of_absent_collection_is_empty_and_creates_nothing(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-asset-inspect-empty-") as temporary:
            root = Path(temporary)
            data_dir = root / "absent-collection"
            environment = runtime_environment(root / "environment")
            inspected = candidate_request(data_dir, environment, {
                "op": "asset.reconcile", "limit": 25,
            })
            self.assertEqual(inspected, {
                "ok": True,
                "result": {
                    "mode": "inspect", "items": [], "hasMore": False,
                    "nextAfter": None, "readOnly": True,
                },
            })
            self.assertFalse(data_dir.exists())

    def test_reconciliation_recovers_after_metadata_commit_before_file_removal(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-cleanup-crash-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            imported = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png",
                "name": "interrupted-cleanup.png",
            })["result"]
            path = local_asset_path(imported["url"])
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
            self.assertTrue(path.is_file())

            inspected = candidate_request(data_dir, environment, {
                "op": "asset.reconcile", "limit": 100,
            })
            self.assertTrue(inspected["ok"], inspected)
            item = next(
                item for item in inspected["result"]["items"]
                if item["id"] == imported["id"]
            )
            self.assertEqual(item["status"], "eligible")
            reconciled = candidate_request(data_dir, environment, {
                "op": "asset.reconcile", "limit": 100, "confirm": True,
            })
            self.assertEqual(reconciled["result"]["removed"], [imported["id"]])
            self.assertFalse(path.exists())
            with database_connection(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM asset_cleanup"
                ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
