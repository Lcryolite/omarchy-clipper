"""Public-process tests for the bounded Rust capture lifecycle."""

import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "build/clipper-helper"
CANDIDATE = Path(os.environ.get(
    "CLIPPER_RUST_BIN", ROOT / "backend/target/release/omarchy-clipper-backend"
))
ORIGIN = "chrome-extension://" + "a" * 32 + "/"
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


def request(binary: Path, data_dir: Path, environment: dict[str, str], value: dict,
            helper: Path | None = None, timeout: int = 15) -> dict:
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


def candidate_request(data_dir: Path, environment: dict[str, str], value: dict,
                      helper: Path = HELPER, timeout: int = 15) -> dict:
    return request(CANDIDATE, data_dir, environment, value, helper, timeout)


def begin_value(suffix: str, **overrides) -> dict:
    value = {
        "op": "capture.begin",
        "title": f"Capture {suffix}",
        "sourceUrl": f"https://example.test/{suffix}",
        "scope": "article body",
        "html": '<p class="discarded">Saved <a href="https://example.test/source">link</a></p>',
        "text": "Saved link",
        "missing": [],
    }
    value.update(overrides)
    return value


def checkpoint(data_dir: Path):
    with contextlib.closing(sqlite3.connect(data_dir / "collection.sqlite3")) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


def schema(data_dir: Path) -> list[tuple]:
    with contextlib.closing(sqlite3.connect(data_dir / "collection.sqlite3")) as connection:
        return connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_autoindex_%' ORDER BY type,name"
        ).fetchall()


def create_legacy_collection(data_dir: Path):
    """Create the last public Collection schema without Rust cleanup metadata."""
    data_dir.mkdir(mode=0o700)
    (data_dir / "assets").mkdir(mode=0o700)
    database = data_dir / "collection.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE draft (id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL, asset_ids TEXT NOT NULL, revision INTEGER NOT NULL);
            INSERT INTO draft VALUES(1,'legacy draft','[]',7);
            CREATE TABLE assets (id TEXT PRIMARY KEY, mime TEXT NOT NULL, name TEXT NOT NULL, filename TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
            CREATE TABLE clips (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('mixed','article')), title TEXT NOT NULL, text TEXT NOT NULL, html TEXT NOT NULL, note TEXT NOT NULL, source_url TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('capturing','incomplete','ready')), missing TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE clip_assets (clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE, asset_id TEXT NOT NULL REFERENCES assets(id), position INTEGER NOT NULL, PRIMARY KEY(clip_id,asset_id));
            CREATE TABLE captures (id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, update_id TEXT NOT NULL, title TEXT NOT NULL, source_url TEXT NOT NULL, scope TEXT NOT NULL, html TEXT NOT NULL, text TEXT NOT NULL, missing TEXT NOT NULL, asset_ids TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO clips VALUES('legacy-clip','mixed','Legacy fixture','Preserved body','','','https://example.test/legacy','selection','ready','[]','2025-01-02T03:04:05.000Z','',3);
        """)
    database.chmod(0o600)


def asset_hashes(data_dir: Path) -> dict[str, str]:
    assets = data_dir / "assets"
    return {
        str(path.relative_to(assets)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(assets.rglob("*")) if path.is_file()
    }


class NativeHost:
    def __init__(self, data_dir: Path, environment: dict[str, str]):
        self.process = subprocess.Popen(
            [str(CANDIDATE), "--native", "--allowed-origin", ORIGIN, ORIGIN,
             "--data-dir", str(data_dir), "--helper-bin", str(HELPER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    def request(self, value: dict) -> dict:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.process.stdin.write(struct.pack("=I", len(encoded)) + encoded)
        self.process.stdin.flush()
        header = self.process.stdout.read(4)
        if len(header) != 4:
            raise AssertionError(self.process.stderr.read().decode(errors="replace"))
        length = struct.unpack("=I", header)[0]
        body = self.process.stdout.read(length)
        if len(body) != length:
            raise AssertionError("short native response")
        return json.loads(body)

    def close(self):
        self.process.stdin.close()
        returncode = self.process.wait(timeout=15)
        if returncode != 0:
            raise AssertionError(self.process.stderr.read().decode(errors="replace"))
        self.process.stdout.close()
        self.process.stderr.close()


class RustCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (HELPER, CANDIDATE):
            if not binary.is_file():
                raise AssertionError(f"required binary is missing: {binary}")

    def test_fresh_begin_restart_abort_and_legacy_schema_preserve_contract(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-capture-") as temporary:
            root = Path(temporary)
            data_dir = root / "candidate"
            legacy_dir = root / "legacy"
            environment = runtime_environment(root / "environment")

            begun = candidate_request(data_dir, environment, begin_value("fresh"))
            self.assertTrue(begun["ok"], begun)
            self.assertRegex(begun["result"]["captureId"],
                             r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
            self.assertRegex(begun["result"]["clipId"],
                             r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
            self.assertTrue((data_dir / "assets").is_dir())
            with contextlib.closing(sqlite3.connect(
                data_dir / "collection.sqlite3"
            )) as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(connection.execute("SELECT * FROM draft").fetchall(), [(1, "", "[]", 0)])

            create_legacy_collection(legacy_dir)
            legacy = candidate_request(legacy_dir, environment, {
                "op": "clip.get", "id": "legacy-clip",
            })
            self.assertTrue(legacy["ok"], legacy)
            self.assertEqual(legacy["result"]["text"], "Preserved body")
            self.assertEqual(candidate_request(legacy_dir, environment, {
                "op": "draft.get",
            })["result"], {
                "text": "legacy draft", "assetIds": [], "assets": [], "revision": 7,
            })
            candidate_schema = schema(data_dir)
            cleanup_tables = [row for row in candidate_schema if row[1] == "asset_cleanup"]
            self.assertEqual(len(cleanup_tables), 1)
            self.assertEqual(cleanup_tables[0][:3], ("table", "asset_cleanup", "asset_cleanup"))
            self.assertEqual(candidate_schema, schema(legacy_dir))
            pending = candidate_request(data_dir, environment, {"op": "capture.pending"})
            self.assertEqual(pending["result"]["items"][0]["status"], "capturing")

            checkpoint(data_dir)
            reason = "interrupted " + "x" * 1200
            abort = {"op": "capture.abort", "captureId": begun["result"]["captureId"],
                     "reason": reason}
            actual = candidate_request(data_dir, environment, abort)
            self.assertTrue(actual["ok"], actual)
            self.assertEqual(actual["result"]["status"], "incomplete")
            self.assertEqual(len(actual["result"]["reason"]), 1000)
            pending = candidate_request(data_dir, environment, {"op": "capture.pending"})
            self.assertEqual(pending["result"]["items"][0]["status"], "incomplete")
            reopened = candidate_request(data_dir, environment, {
                "op": "clip.get", "id": begun["result"]["clipId"],
            })
            self.assertEqual(reopened["result"]["status"], "incomplete")

    def test_native_begin_pending_abort_and_allowlist_share_one_connection(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-native-write-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            host = NativeHost(data_dir, environment)
            try:
                begun = host.request(begin_value("native"))
                self.assertTrue(begun["ok"], begun)
                pending = host.request({"op": "capture.pending"})
                self.assertEqual(pending["result"]["items"][0]["captureId"],
                                 begun["result"]["captureId"])
                self.assertEqual(host.request({
                    "op": "capture.abort",
                    "captureId": begun["result"]["captureId"],
                    "reason": "browser capture stopped",
                }), {
                    "ok": True,
                    "result": {
                        "captureId": begun["result"]["captureId"],
                        "status": "incomplete",
                        "reason": "browser capture stopped",
                    },
                })
                self.assertEqual(host.request({"op": "clip.list"}), {
                    "ok": False,
                    "error": "Operation is not available to browser clients",
                })
                self.assertEqual(host.request({"op": "capture.pending"})
                                 ["result"]["items"][0]["status"], "incomplete")
            finally:
                host.close()

    def test_installed_layout_defaults_data_and_helper_paths(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-layout-") as temporary:
            root = Path(temporary)
            prefix = root / "prefix"
            executable = prefix / "bin/clipper"
            helper = prefix / "libexec/omarchy-clipper/clipper-helper"
            executable.parent.mkdir(parents=True)
            helper.parent.mkdir(parents=True)
            lock = prefix / "share/omarchy-clipper/backend.lock"
            lock.parent.mkdir(parents=True)
            lock.touch()
            shutil.copy2(CANDIDATE, executable)
            shutil.copy2(HELPER, helper)
            environment = runtime_environment(root / "environment")
            process = subprocess.run(
                [str(executable), "--request"],
                input=json.dumps(begin_value("installed-layout")).encode(),
                capture_output=True, env=environment, timeout=15, check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
            self.assertTrue(json.loads(process.stdout)["ok"])
            data_dir = Path(environment["XDG_DATA_HOME"]) / "omarchy-clipper"
            self.assertTrue((data_dir / "collection.sqlite3").is_file())
            self.assertTrue((data_dir / "assets").is_dir())

    def test_concurrent_duplicate_begin_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-concurrent-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            value = begin_value("same-identity")
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(
                    lambda _: candidate_request(data_dir, environment, value), range(4)
                ))
            self.assertTrue(all(response["ok"] for response in responses), responses)
            created = [response for response in responses if "captureId" in response["result"]]
            duplicates = [response for response in responses if "existing" in response["result"]]
            self.assertEqual(len(created), 1, responses)
            self.assertEqual(len(duplicates), 3, responses)
            clip_id = created[0]["result"]["clipId"]
            self.assertTrue(all(item["result"]["existing"]["id"] == clip_id
                                for item in duplicates))
            pending = candidate_request(data_dir, environment, {"op": "capture.pending"})
            self.assertEqual(len(pending["result"]["items"]), 1)

    def test_update_abort_preserves_article_note_draft_and_managed_asset(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-preserve-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            asset = candidate_request(data_dir, environment, {
                "op": "asset.import", "data": PNG, "mime": "image/png", "name": "pixel.png",
            })["result"]
            draft = candidate_request(data_dir, environment, {
                "op": "draft.save", "text": "Keep this Draft",
                "assetIds": [asset["id"]], "expectedRevision": 0,
            })["result"]
            article_begin = candidate_request(data_dir, environment, begin_value("article"))["result"]
            article = candidate_request(data_dir, environment, {
                "op": "capture.finish", "captureId": article_begin["captureId"],
                "html": "<p>Original body</p>", "text": "Original body",
                "assetIds": [], "missing": [],
            })["result"]
            article = candidate_request(data_dir, environment, {
                "op": "note.save", "id": article["id"], "note": "Keep this Article note",
                "expectedRevision": article["revision"],
            })["result"]
            before_assets = asset_hashes(data_dir)
            with contextlib.closing(sqlite3.connect(
                data_dir / "collection.sqlite3"
            )) as connection:
                before_article = connection.execute(
                    "SELECT * FROM clips WHERE id=?", (article["id"],)
                ).fetchone()
                before_draft = connection.execute("SELECT * FROM draft").fetchone()

            update = candidate_request(data_dir, environment, begin_value(
                "article", updateId=article["id"], title="Replacement title",
                html="<p>Replacement body</p>", text="Replacement body",
            ))
            self.assertTrue(update["ok"], update)
            self.assertEqual(update["result"]["clipId"], article["id"])
            aborted = candidate_request(data_dir, environment, {
                "op": "capture.abort", "captureId": update["result"]["captureId"],
                "reason": "refresh failed",
            })
            self.assertTrue(aborted["ok"], aborted)
            with contextlib.closing(sqlite3.connect(
                data_dir / "collection.sqlite3"
            )) as connection:
                self.assertEqual(connection.execute(
                    "SELECT * FROM clips WHERE id=?", (article["id"],)
                ).fetchone(), before_article)
                self.assertEqual(connection.execute("SELECT * FROM draft").fetchone(), before_draft)
            self.assertEqual(asset_hashes(data_dir), before_assets)
            reopened = candidate_request(data_dir, environment, {
                "op": "clip.get", "id": article["id"],
            })
            self.assertEqual(reopened["result"]["note"], "Keep this Article note")
            self.assertEqual(reopened["result"]["text"], "Original body")
            self.assertEqual(draft["text"], "Keep this Draft")

    def test_abort_rolls_back_if_pending_row_disappears_during_update(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-abort-race-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            begun = candidate_request(
                data_dir, environment, begin_value("abort-finish-race")
            )["result"]
            database = data_dir / "collection.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER remove_capture_during_abort "
                    "BEFORE UPDATE OF status ON captures WHEN NEW.status='aborted' "
                    "BEGIN DELETE FROM captures WHERE id=OLD.id; END"
                )
            response = candidate_request(data_dir, environment, {
                "op": "capture.abort",
                "captureId": begun["captureId"],
                "reason": "late abort",
            })
            self.assertEqual(
                response, {"ok": False, "error": "Capture not found"}
            )
            reopened = candidate_request(data_dir, environment, {
                "op": "clip.get", "id": begun["clipId"],
            })["result"]
            self.assertEqual(reopened["status"], "capturing")
            self.assertEqual(
                reopened["pendingCapture"]["captureId"], begun["captureId"]
            )

    def test_superseding_update_rechecks_active_article_before_insert(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-update-target-race-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            begun = candidate_request(
                data_dir, environment, begin_value("update-target")
            )["result"]
            article = candidate_request(data_dir, environment, {
                "op": "capture.finish", "captureId": begun["captureId"],
                "html": "<p>Stable</p>", "text": "Stable",
                "assetIds": [], "missing": [],
            })["result"]
            pending = candidate_request(data_dir, environment, begin_value(
                "update-target", updateId=article["id"], text="First refresh",
            ))["result"]
            database = data_dir / "collection.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER trash_target_during_supersede "
                    "AFTER DELETE ON captures "
                    f"WHEN OLD.clip_id='{article['id']}' "
                    "BEGIN UPDATE clips SET deleted_at='raced' WHERE id=OLD.clip_id; END"
                )
            rejected = candidate_request(data_dir, environment, begin_value(
                "update-target", updateId=article["id"], text="Second refresh",
            ))
            self.assertEqual(rejected, {
                "ok": False,
                "error": "Update target must be an active article clip",
            })
            reopened = candidate_request(data_dir, environment, {
                "op": "clip.get", "id": article["id"],
            })["result"]
            self.assertEqual(reopened["deletedAt"], "")
            self.assertEqual(
                reopened["pendingCapture"]["captureId"], pending["captureId"]
            )

    def test_validation_helper_transaction_busy_and_kill_fail_without_partial_success(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-recovery-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            candidate_request(data_dir, environment, {"op": "clip.list"})

            invalid_cases = [
                begin_value("bad-title", title=" \t"),
                begin_value("bad-scope", scope=" \t"),
                begin_value("bad-missing", missing={}),
                begin_value("bad-html", html="<script>active()</script>"),
            ]
            for value in invalid_cases:
                with self.subTest(value=value):
                    rejected = candidate_request(data_dir, environment, value)
                    self.assertFalse(rejected["ok"], rejected)

            fake_helper = root / "failing-helper"
            fake_helper.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            fake_helper.chmod(0o700)
            helper_failure = candidate_request(
                data_dir, environment, begin_value("helper-failure"), fake_helper
            )
            self.assertFalse(helper_failure["ok"])
            self.assertIn("HTML sanitizer helper", helper_failure["error"])

            hanging_helper = root / "hanging-helper"
            hanging_helper.write_text(
                "#!/bin/sh\nexec /usr/bin/sleep 10\n", encoding="utf-8"
            )
            hanging_helper.chmod(0o700)
            helper_timeout = candidate_request(
                data_dir, environment, begin_value("helper-timeout"), hanging_helper
            )
            self.assertEqual(helper_timeout, {
                "ok": False,
                "error": "HTML sanitizer helper timed out",
            })

            with contextlib.closing(sqlite3.connect(
                data_dir / "collection.sqlite3"
            )) as connection:
                connection.execute(
                    "CREATE TRIGGER inject_capture_failure BEFORE INSERT ON captures "
                    "WHEN NEW.source_url='https://example.test/transaction-failure' "
                    "BEGIN SELECT RAISE(ABORT,'injected capture failure'); END"
                )
                connection.commit()
            transaction_failure = candidate_request(
                data_dir, environment, begin_value("transaction-failure")
            )
            self.assertFalse(transaction_failure["ok"])
            self.assertIn("Could not persist capture progress", transaction_failure["error"])
            self.assertIsNone(candidate_request(data_dir, environment, {
                "op": "capture.check", "sourceUrl": "https://example.test/transaction-failure",
                "scope": "article body",
            })["result"]["existing"])

            with contextlib.closing(sqlite3.connect(
                data_dir / "collection.sqlite3", timeout=0, isolation_level=None
            )) as lock:
                lock.execute("BEGIN IMMEDIATE")
                busy = subprocess.run(
                    [str(CANDIDATE), "--request", "--data-dir", str(data_dir),
                     "--helper-bin", str(HELPER)],
                    input=json.dumps(begin_value("busy")).encode(),
                    capture_output=True, env=environment, timeout=10, check=False,
                )
                self.assertEqual(busy.returncode, 2)
                self.assertEqual(busy.stdout, b"")
                self.assertEqual(
                    busy.stderr.decode().strip(),
                    "Could not initialize collection storage: "
                    "Collection storage stayed busy for 3 seconds; retry the operation",
                )
                lock.execute("ROLLBACK")

            with contextlib.closing(sqlite3.connect(
                data_dir / "collection.sqlite3"
            )) as connection:
                connection.execute(
                    "CREATE TRIGGER stall_capture_insert BEFORE INSERT ON captures "
                    "WHEN NEW.source_url='https://example.test/killed' BEGIN "
                    "SELECT max(value) FROM (WITH RECURSIVE counter(value) AS "
                    "(VALUES(0) UNION ALL SELECT value+1 FROM counter WHERE value<100000000) "
                    "SELECT value FROM counter); END"
                )
                connection.commit()
            process = subprocess.Popen(
                [str(CANDIDATE), "--request", "--data-dir", str(data_dir),
                 "--helper-bin", str(HELPER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment,
            )
            process.stdin.write(json.dumps(begin_value("killed")).encode())
            process.stdin.close()
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            process.kill()
            process.wait(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            process.stdout.close()
            process.stderr.close()

            for suffix in (
                "helper-failure", "helper-timeout", "transaction-failure", "busy", "killed"
            ):
                existing = candidate_request(data_dir, environment, {
                    "op": "capture.check", "sourceUrl": f"https://example.test/{suffix}",
                    "scope": "article body",
                })["result"]["existing"]
                self.assertIsNone(existing, suffix)


if __name__ == "__main__":
    unittest.main()
