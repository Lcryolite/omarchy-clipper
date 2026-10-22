"""Public-boundary tests for Rust Article capture completion and refresh."""

import concurrent.futures
import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest

from tests.test_rust_capture import (
    CANDIDATE,
    HELPER,
    PNG,
    NativeHost,
    candidate_request,
    request,
    runtime_environment,
)


def begin(title: str, source: str, update_id: str = "") -> dict:
    value = {
        "op": "capture.begin",
        "title": title,
        "sourceUrl": source,
        "scope": "article body",
        "html": f"<h1>{title}</h1>",
        "text": title,
        "missing": [],
    }
    if update_id:
        value["updateId"] = update_id
    return value


def imported_asset(data_dir: Path, environment: dict[str, str], name: str) -> dict:
    response = candidate_request(data_dir, environment, {
        "op": "asset.import", "data": PNG, "mime": "image/png", "name": name,
    })
    if not response["ok"]:
        raise AssertionError(response)
    return response["result"]


class RustArticleTests(unittest.TestCase):
    def test_complete_article_with_managed_image_reopens_and_native_hides_body(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-article-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            asset = imported_asset(data_dir, environment, "diagram.png")
            begun = candidate_request(data_dir, environment, begin(
                "Offline article", "https://example.test/article",
            ))["result"]
            finish_request = {
                "op": "capture.finish",
                "captureId": begun["captureId"],
                "html": (
                    '<h1 class="discarded">Offline article</h1>'
                    '<p>Saved <a href="https://example.test/source">link</a>.</p>'
                    f'<img src="clipper://asset/{asset["id"]}" alt="Diagram">'
                ),
                "text": "Offline article Saved link",
                "assetIds": [asset["id"]],
                "missing": [],
            }
            finished = candidate_request(data_dir, environment, finish_request)
            self.assertTrue(finished["ok"], finished)
            article = finished["result"]
            self.assertEqual(article["status"], "ready")
            self.assertEqual(article["assets"], [asset])
            self.assertNotIn("class=", article["html"])
            self.assertIn(f'clipper://asset/{asset["id"]}', article["html"])
            self.assertEqual(
                candidate_request(data_dir, environment, {
                    "op": "clip.get", "id": article["id"],
                })["result"],
                article,
            )

            native_data = root / "native"
            host = NativeHost(native_data, environment)
            try:
                native_begin = host.request(begin(
                    "Native article", "https://example.test/native-article",
                ))["result"]
                native_finish = host.request({
                    "op": "capture.finish", "captureId": native_begin["captureId"],
                    "html": "<p>Private body</p>", "text": "Private body",
                    "assetIds": [], "missing": [],
                })
                self.assertTrue(native_finish["ok"], native_finish)
                self.assertNotIn("html", native_finish["result"])
                self.assertNotIn("text", native_finish["result"])
                self.assertNotIn("note", native_finish["result"])
            finally:
                host.close()

    def test_incomplete_refresh_retry_preserves_note_revision_and_shared_assets(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-refresh-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            shared = imported_asset(data_dir, environment, "shared.png")
            mixed = candidate_request(data_dir, environment, {
                "op": "capture.mixed", "text": "Shared owner",
                "assetIds": [shared["id"]],
            })
            self.assertTrue(mixed["ok"], mixed)
            first = candidate_request(data_dir, environment, begin(
                "Guide", "https://example.test/guide",
            ))["result"]
            original = candidate_request(data_dir, environment, {
                "op": "capture.finish", "captureId": first["captureId"],
                "html": f'<p>Stable</p><img src="clipper://asset/{shared["id"]}">',
                "text": "Stable", "assetIds": [shared["id"]], "missing": [],
            })["result"]
            refresh = candidate_request(data_dir, environment, begin(
                "Guide v2", "https://example.test/guide", original["id"],
            ))["result"]
            noted = candidate_request(data_dir, environment, {
                "op": "note.save", "id": original["id"], "note": "Keep this note",
                "expectedRevision": original["revision"],
            })["result"]
            partial_asset = imported_asset(data_dir, environment, "partial.png")
            partial_path = Path(partial_asset["url"].removeprefix("file://"))
            incomplete = candidate_request(data_dir, environment, {
                "op": "capture.finish", "captureId": refresh["captureId"],
                "html": f'<p>Partial</p><img src="clipper://asset/{partial_asset["id"]}">',
                "text": "Partial", "assetIds": [partial_asset["id"]],
                "missing": [{"url": "https://cdn.example.test/missing.png", "reason": "offline"}],
            })
            self.assertTrue(incomplete["ok"], incomplete)
            self.assertEqual(incomplete["result"]["updateStatus"], "incomplete")
            self.assertEqual(incomplete["result"]["text"], "Stable")
            self.assertEqual(incomplete["result"]["note"], "Keep this note")
            self.assertEqual(incomplete["result"]["revision"], noted["revision"])
            self.assertTrue(partial_path.is_file())

            retry = candidate_request(data_dir, environment, begin(
                "Guide v3", "https://example.test/guide", original["id"],
            ))
            self.assertTrue(retry["ok"], retry)
            self.assertFalse(partial_path.exists())
            replacement = imported_asset(data_dir, environment, "replacement.png")
            ready = candidate_request(data_dir, environment, {
                "op": "capture.finish", "captureId": retry["result"]["captureId"],
                "html": f'<p>Current</p><img src="clipper://asset/{replacement["id"]}">',
                "text": "Current", "assetIds": [replacement["id"]], "missing": [],
            })
            self.assertTrue(ready["ok"], ready)
            article = ready["result"]
            self.assertEqual(article["note"], "Keep this note")
            self.assertEqual(article["revision"], noted["revision"] + 1)
            self.assertEqual(article["assets"], [replacement])
            self.assertEqual(candidate_request(data_dir, environment, {
                "op": "capture.pending",
            })["result"]["items"], [])
            self.assertTrue(Path(shared["url"].removeprefix("file://")).is_file())
            self.assertEqual(candidate_request(data_dir, environment, {
                "op": "clip.get", "id": article["id"],
            })["result"], article)

    def test_finish_failure_and_concurrent_duplicate_leave_recoverable_state(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-finish-recovery-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            begun = candidate_request(data_dir, environment, begin(
                "Recoverable", "https://example.test/recoverable",
            ))["result"]
            database = data_dir / "collection.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_finish BEFORE UPDATE OF html ON clips "
                    "BEGIN SELECT RAISE(ABORT,'injected finish failure'); END"
                )
                connection.commit()
            finish = {
                "op": "capture.finish", "captureId": begun["captureId"],
                "html": "<p>Recovered</p>", "text": "Recovered",
                "assetIds": [], "missing": [],
            }
            failed = candidate_request(data_dir, environment, finish)
            self.assertFalse(failed["ok"], failed)
            pending = candidate_request(data_dir, environment, {"op": "capture.pending"})
            self.assertEqual(pending["result"]["items"][0]["captureId"], begun["captureId"])
            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP TRIGGER fail_finish")
                connection.commit()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(
                    lambda _: candidate_request(data_dir, environment, finish), range(2)
                ))
            self.assertEqual(sum(response["ok"] for response in responses), 1, responses)
            failure = next(response for response in responses if not response["ok"])
            self.assertEqual(failure["error"], "Capture not found")
            reopened = candidate_request(data_dir, environment, {
                "op": "clip.get", "id": begun["clipId"],
            })
            self.assertTrue(reopened["ok"], reopened)
            self.assertEqual(reopened["result"]["text"], "Recovered")
            self.assertEqual(candidate_request(data_dir, environment, {
                "op": "capture.pending",
            })["result"]["items"], [])

    def test_killed_finish_rolls_back_and_can_be_retried(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-finish-kill-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            begun = candidate_request(data_dir, environment, begin(
                "Interrupted", "https://example.test/interrupted-finish",
            ))["result"]
            database = data_dir / "collection.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TRIGGER stall_finish BEFORE UPDATE OF html ON clips BEGIN "
                    "SELECT max(value) FROM (WITH RECURSIVE counter(value) AS "
                    "(VALUES(0) UNION ALL SELECT value+1 FROM counter WHERE value<100000000) "
                    "SELECT value FROM counter); END"
                )
                connection.commit()
            finish = {
                "op": "capture.finish", "captureId": begun["captureId"],
                "html": "<p>Recovered after kill</p>", "text": "Recovered after kill",
                "assetIds": [], "missing": [],
            }
            process = subprocess.Popen(
                [str(CANDIDATE), "--request", "--data-dir", str(data_dir),
                 "--helper-bin", str(HELPER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment,
            )
            process.stdin.write(json.dumps(finish).encode())
            process.stdin.close()
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP TRIGGER stall_finish")
                connection.commit()

            pending = candidate_request(data_dir, environment, {"op": "capture.pending"})
            self.assertEqual(pending["result"]["items"][0]["captureId"], begun["captureId"])
            before_retry = candidate_request(data_dir, environment, {
                "op": "clip.get", "id": begun["clipId"],
            })
            self.assertEqual(before_retry["result"]["status"], "capturing")
            retried = candidate_request(data_dir, environment, finish)
            self.assertTrue(retried["ok"], retried)
            self.assertEqual(retried["result"]["status"], "ready")

    def test_committed_refresh_reports_deferred_asset_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="clipper-rust-finish-cleanup-") as temporary:
            root = Path(temporary)
            data_dir = root / "collection"
            environment = runtime_environment(root / "environment")
            old_asset = imported_asset(data_dir, environment, "old.png")
            first = candidate_request(data_dir, environment, begin(
                "Cleanup article", "https://example.test/cleanup-article",
            ))["result"]
            article = candidate_request(data_dir, environment, {
                "op": "capture.finish", "captureId": first["captureId"],
                "html": f'<img src="clipper://asset/{old_asset["id"]}">',
                "text": "Old", "assetIds": [old_asset["id"]], "missing": [],
            })["result"]
            replacement = imported_asset(data_dir, environment, "new.png")
            refresh = candidate_request(data_dir, environment, begin(
                "Cleanup article", "https://example.test/cleanup-article", article["id"],
            ))["result"]
            database = data_dir / "collection.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER stall_refresh_asset_delete BEFORE DELETE ON assets "
                    f"WHEN OLD.id='{old_asset['id']}' BEGIN "
                    "SELECT max(value) FROM (WITH RECURSIVE counter(value) AS "
                    "(VALUES(0) UNION ALL SELECT value+1 FROM counter WHERE value<20000000) "
                    "SELECT value FROM counter); END"
                )
            process = subprocess.Popen(
                [str(CANDIDATE), "--request", "--data-dir", str(data_dir),
                 "--helper-bin", str(HELPER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment,
            )
            process.stdin.write(json.dumps({
                "op": "capture.finish", "captureId": refresh["captureId"],
                "html": f'<img src="clipper://asset/{replacement["id"]}">',
                "text": "New", "assetIds": [replacement["id"]], "missing": [],
            }).encode())
            process.stdin.close()
            time.sleep(0.15)
            self.assertIsNone(process.poll(), "finish did not reach asset cleanup")
            (data_dir / "assets").chmod(0o500)
            try:
                stdout = process.stdout.read()
                stderr = process.stderr.read()
                process.wait(timeout=20)
            finally:
                (data_dir / "assets").chmod(0o700)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            process.stdout.close()
            process.stderr.close()
            response = json.loads(stdout)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["text"], "New")
            self.assertEqual(response.get("cleanupPending"), True)
            self.assertEqual(response.get("warnings"), [
                f"Could not remove deferred orphan asset: {old_asset['id']}"
            ])
            old_path = Path(old_asset["url"].removeprefix("file://"))
            self.assertTrue(old_path.exists())
            recovered = imported_asset(data_dir, environment, "recover.png")
            self.assertTrue(recovered["id"])
            self.assertFalse(old_path.exists())


if __name__ == "__main__":
    unittest.main()
