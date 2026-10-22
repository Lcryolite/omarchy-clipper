"""Read-only, framing, and browser-boundary tests for the Rust backend."""

import json
import hashlib
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = Path(os.environ.get("CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"))
HELPER = ROOT / "build/clipper-helper"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


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
        arguments,
        input=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
        capture_output=True,
        env=environment,
        timeout=15,
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(
            f"{binary} exited {process.returncode}: {process.stderr.decode(errors='replace')}"
        )
    return json.loads(process.stdout)


def native_process(binary: Path, data_dir: Path, environment: dict[str, str], payload: bytes,
                   origin: str | None = None) -> subprocess.CompletedProcess:
    allowed = origin or "chrome-extension://" + "a" * 32 + "/"
    return subprocess.run(
        [str(binary), "--native", "--allowed-origin", allowed, allowed,
         "--data-dir", str(data_dir)],
        input=payload,
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )


def native_messages(binary: Path, data_dir: Path, environment: dict[str, str],
                    messages: list[dict | bytes]) -> list[dict]:
    payload = bytearray()
    for message in messages:
        encoded = (message if isinstance(message, bytes) else
                   json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode())
        payload.extend(struct.pack("=I", len(encoded)))
        payload.extend(encoded)
    process = native_process(binary, data_dir, environment, bytes(payload))
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode(errors="replace"))
    responses = []
    offset = 0
    while offset < len(process.stdout):
        if offset + 4 > len(process.stdout):
            raise AssertionError("short native response header")
        length = struct.unpack("=I", process.stdout[offset:offset + 4])[0]
        offset += 4
        body = process.stdout[offset:offset + length]
        if len(body) != length:
            raise AssertionError("short native response body")
        responses.append(json.loads(body))
        offset += length
    return responses


def content_snapshot(data_dir: Path) -> dict[str, str]:
    result = {"collection.sqlite3": hashlib.sha256(
        (data_dir / "collection.sqlite3").read_bytes()).hexdigest()}
    assets = data_dir / "assets"
    if assets.is_dir():
        for path in sorted(item for item in assets.rglob("*") if item.is_file()):
            result[f"assets/{path.relative_to(assets)}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class RustReadonlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HELPER.is_file():
            raise AssertionError(f"required binary is missing: {HELPER}")
        if "CLIPPER_RUST_BIN" not in os.environ:
            subprocess.run([sys.executable, str(ROOT / "scripts/build_rust_backend.py"), "--offline"],
                           cwd=ROOT, check=True, timeout=120)
        if not CANDIDATE.is_file():
            raise AssertionError("the selected Rust backend binary does not exist")

    def seed_query_fixture(self, data_dir: Path, environment: dict[str, str]) -> dict[str, str]:
        note = request(CANDIDATE, data_dir, environment, {
            "op": "capture.mixed",
            "title": "Unicode 中文 note",
            "text": r"literal 100% under_score slash\needle 中文",
            "assetIds": [],
            "sourceUrl": "https://example.test/note",
            "scope": "synthetic",
        })["result"]["id"]
        asset = request(CANDIDATE, data_dir, environment, {
            "op": "asset.import", "data": PNG, "mime": "image/png", "name": "pixel.png",
        })["result"]["id"]
        image = request(CANDIDATE, data_dir, environment, {
            "op": "capture.mixed",
            "title": "Image only",
            "text": "",
            "assetIds": [asset],
            "sourceUrl": "https://example.test/image",
            "scope": "synthetic",
        })["result"]["id"]
        trashed = request(CANDIDATE, data_dir, environment, {
            "op": "capture.mixed",
            "title": "Trashed text",
            "text": "trash marker",
            "assetIds": [],
            "sourceUrl": "https://example.test/trash",
            "scope": "synthetic",
        })["result"]["id"]
        request(CANDIDATE, data_dir, environment, {"op": "clip.trash", "id": trashed})
        article = request(CANDIDATE, data_dir, environment, {
            "op": "capture.begin",
            "title": "Pending article",
            "sourceUrl": "https://example.test/article",
            "scope": "article body",
            "html": "<p>article</p>",
            "text": "article",
            "missing": [],
        })["result"]["clipId"]
        with sqlite3.connect(data_dir / "collection.sqlite3") as connection:
            connection.execute("UPDATE clips SET created_at='2026-01-02T03:04:05.000Z'")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"note": note, "image": image, "trashed": trashed, "article": article}

    def backend_request(self, data_dir: Path, environment: dict[str, str], value: dict):
        return request(CANDIDATE, data_dir, environment, value)

    def test_capture_check_matches_qurl_strict_mode_contract(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-url-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            request(CANDIDATE, data_dir, environment, {"op": "clip.list"})

            source_urls = [
                "https://example.test/path?part=1#section",
                "HTTP://example.test/upper-scheme",
                "https://user:password@example.test/private",
                "https://[::1]/ipv6",
                "https://例子.test/路径",
                "ftp://example.test/not-http",
                "https:///missing-host",
                "https://[not-an-ipv6]/",
                "https://example.test:bad-port/",
                "https://example.test:99999/",
                "https://example.test/%",
                "https://example.test/%ZZ",
                "https://exa mple.test/space",
                "https://example.test\\backslash",
                "https://example.test:/empty-port",
                "https://example.test/{brace}",
                "https://example.test/|pipe",
                "https://example.test/^caret",
                "https://example.test/`backtick",
                "https://example.test/<angle>",
                "https://exam\x00ple.test/control-host",
                "https://example.test/control\x01-path",
                "https://./dot-host",
                "https://../double-dot-host",
                "https://[::1]extra/bad-ipv6-suffix",
                "https://user@@example.test/double-at",
            ]
            expected_ok = [index in {0, 1, 2, 3, 4, 14}
                           for index in range(len(source_urls))]
            for source_url, is_valid in zip(source_urls, expected_ok):
                value = {"op": "capture.check", "sourceUrl": source_url, "scope": "article"}
                with self.subTest(source_url=source_url):
                    response = request(CANDIDATE, data_dir, environment, value)
                    self.assertEqual(response["ok"], is_valid, response)
                    if is_valid:
                        self.assertIsNone(response["result"]["existing"])

    def test_list_search_get_filters_pagination_ties_and_trash_match(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-query-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            ids = self.seed_query_fixture(data_dir, environment)

            all_items = self.backend_request(data_dir, environment, {
                "op": "clip.list", "filter": "all", "query": "", "limit": 100, "offset": 0,
            })["result"]
            self.assertEqual(
                [item["id"] for item in all_items["items"]],
                [ids["article"], ids["image"], ids["note"]],
            )
            self.assertEqual(all_items["total"], 3)

            cases = [
                {"op": "clip.list", "filter": "articles"},
                {"op": "clip.list", "filter": "notes"},
                {"op": "clip.list", "filter": "images"},
                {"op": "clip.list", "filter": "all", "trash": True},
                {"op": "clip.list", "filter": "all", "query": "100%"},
                {"op": "clip.list", "filter": "all", "query": "under_score"},
                {"op": "clip.list", "filter": "all", "query": r"slash\needle"},
                {"op": "clip.list", "filter": "all", "query": "中文"},
                {"op": "clip.list", "filter": "all", "limit": 0, "offset": -5},
                {"op": "clip.list", "filter": "all", "limit": 900, "offset": 1},
                {"op": "clip.list", "filter": "all", "limit": 2.0, "offset": 1.0},
                {"op": "clip.list", "filter": "unknown"},
                {"op": "clip.get", "id": ids["note"]},
                {"op": "clip.get", "id": "does-not-exist"},
                {"op": "capture.check", "sourceUrl": "https://example.test/article",
                 "scope": " \t"},
                {"op": "read.unknown"},
            ]
            results = []
            for value in cases:
                with self.subTest(value=value):
                    results.append(self.backend_request(data_dir, environment, value))
            self.assertEqual([item["id"] for item in results[0]["result"]["items"]], [ids["article"]])
            self.assertEqual([item["id"] for item in results[1]["result"]["items"]], [ids["note"]])
            self.assertEqual([item["id"] for item in results[2]["result"]["items"]], [ids["image"]])
            self.assertEqual([item["id"] for item in results[3]["result"]["items"]], [ids["trashed"]])

    def test_native_origin_rejection_fails_before_storage_mutation(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-origin-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            expected_origin = "chrome-extension://" + "a" * 32 + "/"
            actual_origin = "chrome-extension://" + "b" * 32 + "/"

            def rejected(binary: Path, origin_arguments: list[str]):
                return subprocess.run(
                    [str(binary), "--native", *origin_arguments, "--data-dir", str(data_dir)],
                    input=b"",
                    capture_output=True,
                    env=environment,
                    timeout=10,
                    check=False,
                )

            origin_cases = [
                ["--allowed-origin", expected_origin, actual_origin],
                [],
                ["--allowed-origin", expected_origin],
                ["--allowed-origin", "https://example.test/", "https://example.test/"],
            ]
            for origin_arguments in origin_cases:
                with self.subTest(origin_arguments=origin_arguments):
                    candidate = rejected(CANDIDATE, origin_arguments)
                    self.assertNotEqual(candidate.returncode, 0)
                    self.assertEqual(candidate.stdout, b"")
                    self.assertIn(b"Browser origin is not allowed", candidate.stderr)
                    self.assertFalse(data_dir.exists())

    def test_pending_identity_and_native_metadata_match_without_content_writes(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-native-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            asset_id = request(CANDIDATE, data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "article.png",
            })["result"]["id"]
            begun = request(CANDIDATE, data_dir, environment, {
                "op": "capture.begin", "title": "Completed article",
                "sourceUrl": "https://example.test/completed", "scope": "article body",
                "html": "<p>initial</p>", "text": "initial", "missing": [],
            })["result"]
            completed = request(CANDIDATE, data_dir, environment, {
                "op": "capture.finish", "captureId": begun["captureId"],
                "html": f'<p>secret body</p><img src="clipper://asset/{asset_id}">',
                "text": "secret body", "assetIds": [asset_id], "missing": [],
            })["result"]
            request(CANDIDATE, data_dir, environment, {
                "op": "note.save", "id": begun["clipId"], "note": "private note",
                "expectedRevision": completed["revision"],
            })
            update = request(CANDIDATE, data_dir, environment, {
                "op": "capture.begin", "updateId": begun["clipId"],
                "title": "Updated article",
                "sourceUrl": "https://example.test/completed", "scope": "article body",
                "html": "<p>updated</p>", "text": "updated", "missing": [],
            })["result"]
            pending = request(CANDIDATE, data_dir, environment, {
                "op": "capture.begin", "title": "Pending article",
                "sourceUrl": "https://example.test/pending", "scope": "article body",
                "html": "<p>pending</p>", "text": "pending",
                "missing": [
                    {"url": "https://cdn.test/permission.png",
                     "reason": "Permission is required to retry this public image origin."},
                    {"url": "https://cdn.test/unsupported.png",
                     "reason": "Asset image format is unsupported"},
                    {"url": "https://cdn.test/transient.png", "reason": "offline",
                     "retryable": "not-a-boolean"},
                ],
            })["result"]
            with sqlite3.connect(data_dir / "collection.sqlite3") as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

            request_cases = [
                {"op": "capture.pending"},
                {"op": "capture.check", "sourceUrl": "https://example.test/completed",
                 "scope": "article body"},
                {"op": "capture.check", "sourceUrl": "https://example.test/pending",
                 "scope": "article body"},
                {"op": "capture.check", "sourceUrl": "https://example.test/pending",
                 "scope": "different"},
                {"op": "clip.get", "id": pending["clipId"]},
            ]
            results = []
            for value in request_cases:
                with self.subTest(value=value):
                    results.append(self.backend_request(data_dir, environment, value))
            self.assertEqual(update["clipId"], begun["clipId"])
            self.assertTrue(any(
                item["updateId"] == begun["clipId"]
                for item in results[0]["result"]["items"]
            ))
            self.assertTrue(results[1]["result"]["existing"]["pendingCapture"]["isUpdate"])

            native_cases = [
                request_cases[1],
                request_cases[0],
                {"op": "clip.list"},
                b"not-json",
            ]
            before = content_snapshot(data_dir)
            actual = native_messages(CANDIDATE, data_dir, environment, native_cases)
            self.assertEqual(len(actual), len(native_cases))
            self.assertNotIn("text", actual[0]["result"]["existing"])
            self.assertNotIn("note", actual[0]["result"]["existing"])
            self.assertNotIn("url", actual[0]["result"]["existing"]["assets"][0])
            self.assertEqual(content_snapshot(data_dir), before)
            sidecars = {path.name: path.stat().st_size
                        for path in data_dir.glob("collection.sqlite3-*")}
            self.assertTrue(set(sidecars).issubset({"collection.sqlite3-shm", "collection.sqlite3-wal"}))

    def test_request_and_native_framing_limits(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-framing-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            request(CANDIDATE, data_dir, environment, {"op": "clip.list"})

            oversized = b"{" + b" " * (24 * 1024 * 1024)
            with tempfile.TemporaryFile() as request_file:
                request_file.write(oversized)
                request_file.seek(0)
                oversized_result = subprocess.run(
                    [str(CANDIDATE), "--request", "--data-dir", str(data_dir)],
                    stdin=request_file, capture_output=True, env=environment,
                    timeout=20, check=False,
                )
            self.assertEqual(oversized_result.returncode, 0, oversized_result.stderr)
            self.assertEqual(json.loads(oversized_result.stdout), {
                "ok": False, "error": "Request is too large",
            })

            invalid_frames = [
                struct.pack("=I", 0),
                struct.pack("=I", 24 * 1024 * 1024 + 1),
                b"\x01\x00",
                struct.pack("=I", 5) + b"{}",
            ]
            expected_codes = (2, 2, 0, 2)
            for payload, expected_code in zip(invalid_frames, expected_codes):
                with self.subTest(payload=payload[:8]):
                    candidate = native_process(CANDIDATE, data_dir, environment, payload)
                    self.assertEqual(candidate.returncode, expected_code, candidate.stderr)
                    self.assertEqual(candidate.stdout, b"")


if __name__ == "__main__":
    unittest.main()
