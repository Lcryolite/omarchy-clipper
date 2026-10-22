"""Candidate desktop actions with isolated files and fake session adapters."""

import base64
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = Path(os.environ.get(
    "CLIPPER_RUST_BIN",
    ROOT / "backend/target/release/omarchy-clipper-backend",
))
HELPER = ROOT / "build/clipper-helper"
ORIGIN = "chrome-extension://" + "a" * 32 + "/"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class RustDesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clipper-rust-desktop-")
        self.root = Path(self.temporary.name)
        self.data = self.root / "collection"
        self.commands = self.root / "commands"
        self.commands.mkdir()
        self.environment = {
            "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8",
            "PATH": str(self.commands) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "SECRET_SENTINEL": "must-not-reach-helper",
            "TMPDIR": str(self.root / "tmp"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }
        for key in (
            "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
        ):
            Path(self.environment[key]).mkdir(parents=True)
        Path(self.environment["XDG_RUNTIME_DIR"]).chmod(0o700)
        initialized = self.candidate_request({"op": "clip.list"})
        self.assertTrue(initialized["ok"], initialized)

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, name, source):
        path = self.commands / name
        path.write_text(f"#!/usr/bin/python3\n{source}", encoding="utf-8")
        path.chmod(0o700)
        return path

    def candidate_request(self, value, helper=HELPER):
        completed = subprocess.run(
            [
                str(CANDIDATE), "--request", "--data-dir", str(self.data),
                "--helper-bin", str(helper),
            ],
            input=json.dumps(value),
            text=True,
            capture_output=True,
            env=self.environment,
            timeout=10,
            check=True,
        )
        return json.loads(completed.stdout)

    def native_request(self, value):
        payload = json.dumps(value).encode()
        completed = subprocess.run(
            [
                str(CANDIDATE), "--native", "--data-dir", str(self.data),
                "--helper-bin", str(HELPER), "--allowed-origin", ORIGIN, ORIGIN,
            ],
            input=struct.pack("=I", len(payload)) + payload,
            capture_output=True,
            env=self.environment,
            timeout=10,
            check=True,
        )
        size = struct.unpack("=I", completed.stdout[:4])[0]
        return json.loads(completed.stdout[4:4 + size])

    def test_theme_and_web_link_use_only_allowlisted_session_environment(self):
        colors = Path(self.environment["XDG_STATE_HOME"]) / "omarchy/current/theme/colors.toml"
        colors.parent.mkdir(parents=True)
        colors.write_text(
            'background = "#112233"\naccent = "#abcdef"\nlighter_background = "#223344"\n',
            encoding="utf-8",
        )
        opened = self.root / "opened.json"
        self.command(
            "xdg-open",
            "import json,os,pathlib,sys\n"
            f"pathlib.Path({str(opened)!r}).write_text(json.dumps({{'args':sys.argv[1:],'secret':os.environ.get('SECRET_SENTINEL')}}))\n",
        )

        theme = self.candidate_request({"op": "theme.get"})
        self.assertEqual(theme["result"]["background"], "#112233")
        self.assertEqual(theme["result"]["surface"], "#223344")
        linked = self.candidate_request({"op": "link.open", "url": "https://example.test/path"})
        self.assertEqual(linked, {"ok": True, "result": True})
        self.assertEqual(json.loads(opened.read_text()), {
            "args": ["https://example.test/path"],
            "secret": None,
        })
        rejected = self.candidate_request({"op": "link.open", "url": "file:///tmp/private"})
        self.assertEqual(rejected, {"ok": False, "error": "Only web links can be opened"})

    def test_file_and_clipboard_import_store_validated_managed_images(self):
        source = self.root / "source image.png"
        source.write_bytes(PNG)
        self.command(
            "wl-paste",
            "import sys\n"
            "if '--list-types' in sys.argv:\n print('image/jpeg')\n"
            f"elif sys.argv[-1] == 'image/jpeg':\n sys.stdout.buffer.write({PNG!r})\n"
            "else:\n sys.exit(2)\n",
        )

        imported_file = self.candidate_request({"op": "file.import", "path": str(source)})
        self.assertTrue(imported_file["ok"], imported_file)
        self.assertEqual(imported_file["result"]["mime"], "image/png")
        self.assertEqual(imported_file["result"]["name"], source.name)
        imported_clipboard = self.candidate_request({"op": "clipboard.import"})
        self.assertTrue(imported_clipboard["ok"], imported_clipboard)
        self.assertEqual(imported_clipboard["result"]["mime"], "image/png")

        saved = self.candidate_request({
            "op": "capture.mixed",
            "text": "Imported from isolated desktop adapters",
            "assetIds": [imported_file["result"]["id"], imported_clipboard["result"]["id"]],
            "title": "Desktop import",
            "sourceUrl": "",
        })
        self.assertTrue(saved["ok"], saved)
        clip_id = saved["result"]["id"]
        self.assertEqual(
            self.candidate_request({"op": "clip.get", "id": clip_id})["result"],
            saved["result"],
        )
        managed = list((self.data / "assets").glob("*"))
        self.assertEqual(len(managed), 2)
        self.assertTrue(all(path.read_bytes() == PNG for path in managed))

        invalid = self.root / "invalid.png"
        invalid.write_bytes(b"not an image")
        failed = self.candidate_request({"op": "file.import", "path": str(invalid)})
        self.assertFalse(failed["ok"], failed)
        self.assertEqual(len(list((self.data / "assets").glob("*"))), 2)

    def test_clipboard_copy_checks_clip_ownership_before_fixed_adapter(self):
        copied_bytes = self.root / "copied-bytes"
        copied_type = self.root / "copied-type"
        self.command(
            "wl-copy",
            "import pathlib,sys\n"
            f"pathlib.Path({str(copied_bytes)!r}).write_bytes(sys.stdin.buffer.read())\n"
            f"pathlib.Path({str(copied_type)!r}).write_text(sys.argv[sys.argv.index('--type')+1])\n",
        )
        asset = self.candidate_request({
            "op": "asset.import",
            "data": base64.b64encode(PNG).decode(),
            "mime": "image/png",
            "name": "owned.png",
        })["result"]
        other = self.candidate_request({
            "op": "asset.import",
            "data": base64.b64encode(PNG).decode(),
            "mime": "image/png",
            "name": "other.png",
        })["result"]
        clip = self.candidate_request({
            "op": "capture.mixed",
            "text": "Copy this text",
            "assetIds": [asset["id"]],
            "title": "Copy probe",
            "sourceUrl": "",
        })["result"]

        copied = self.candidate_request({"op": "clipboard.copy", "id": clip["id"]})
        self.assertEqual(copied, {"ok": True, "result": True})
        self.assertEqual(copied_bytes.read_bytes(), b"Copy this text")
        self.assertEqual(copied_type.read_text(), "text/plain;charset=utf-8")
        copied = self.candidate_request({
            "op": "clipboard.copy", "id": clip["id"], "assetId": asset["id"],
        })
        self.assertEqual(copied, {"ok": True, "result": True})
        self.assertEqual(copied_bytes.read_bytes(), PNG)
        self.assertEqual(copied_type.read_text(), "image/png")

        copied_bytes.unlink()
        rejected = self.candidate_request({
            "op": "clipboard.copy", "id": clip["id"], "assetId": other["id"],
        })
        self.assertEqual(rejected, {
            "ok": False,
            "error": "Image does not belong to this clip",
        })
        self.assertFalse(copied_bytes.exists())

    def test_region_capture_imports_locally_and_creates_native_mixed_clip(self):
        self.command(
            "omarchy",
            "import os,pathlib,sys\n"
            "assert sys.argv[1:] == ['capture','screenshot','region','save']\n"
            "target=pathlib.Path(os.environ['OMASNAP_SCREENSHOT_DIR'])/'capture.png'\n"
            f"target.write_bytes({PNG!r})\n",
        )
        local = self.candidate_request({"op": "capture.region"})
        self.assertTrue(local["ok"], local)
        self.assertEqual(local["result"]["mime"], "image/png")

        native = self.native_request({
            "op": "capture.region",
            "sourceUrl": "https://example.test/screenshot",
            "title": "Region capture",
        })
        self.assertTrue(native["ok"], native)
        clip = native["result"]
        self.assertEqual(clip["kind"], "mixed")
        self.assertEqual(clip["scope"], "region")
        self.assertEqual(len(clip["assets"]), 1)
        reopened = self.candidate_request({"op": "clip.get", "id": clip["id"]})["result"]
        for field in ("id", "kind", "scope", "revision"):
            self.assertEqual(reopened[field], clip[field])
        self.assertEqual(
            [asset["id"] for asset in reopened["assets"]],
            [asset["id"] for asset in clip["assets"]],
        )

    def test_region_capture_rejects_before_orphaning_and_reaps_failed_adapter(self):
        invoked = self.root / "capture-invoked"
        self.command(
            "omarchy",
            f"import pathlib\npathlib.Path({str(invoked)!r}).write_text('called')\n",
        )
        rejected = self.native_request({
            "op": "capture.region", "sourceUrl": "file:///tmp/private",
        })
        self.assertEqual(rejected, {"ok": False, "error": "A source URL is required"})
        self.assertFalse(invoked.exists())

        self.command("omarchy", "import sys\nsys.exit(0)\n")
        cancelled = self.candidate_request({"op": "capture.region"})
        self.assertEqual(cancelled, {
            "ok": False,
            "error": "Screenshot was cancelled or produced no image",
        })
        self.assertEqual(list((self.data / "assets").glob("*")), [])

        self.command(
            "omarchy",
            "import os,pathlib\n"
            "target=pathlib.Path(os.environ['OMASNAP_SCREENSHOT_DIR'])/'too-large.png'\n"
            "with target.open('wb') as output:\n output.truncate(12*1024*1024+1)\n",
        )
        oversized = self.candidate_request({"op": "capture.region"})
        self.assertEqual(oversized, {
            "ok": False,
            "error": "Screenshot could not be read or exceeds 12 MiB",
        })
        self.assertEqual(list((self.data / "assets").glob("*")), [])

        pids = self.root / "capture-pids"
        self.command(
            "omarchy",
            "import os,pathlib,subprocess,sys\n"
            "child=subprocess.Popen(['/usr/bin/sleep','30'])\n"
            f"pathlib.Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
            "sys.exit(7)\n",
        )
        failed = self.candidate_request({"op": "capture.region"})
        self.assertEqual(failed, {"ok": False, "error": "omarchy was cancelled or failed"})
        for _ in range(30):
            states = [self.process_state(int(pid)) for pid in pids.read_text().split()]
            if all(state in (None, "Z") for state in states):
                break
            time.sleep(0.05)
        self.assertTrue(all(state in (None, "Z") for state in states), states)
        self.assertEqual(list((self.data / "assets").glob("*")), [])

        self.command(
            "omarchy",
            "import os,pathlib\n"
            "target=pathlib.Path(os.environ['OMASNAP_SCREENSHOT_DIR'])/'capture.png'\n"
            f"target.write_bytes({PNG!r})\n",
        )
        with sqlite3.connect(self.data / "collection.sqlite3") as connection:
            connection.execute(
                "CREATE TRIGGER fail_region BEFORE INSERT ON clips "
                "WHEN NEW.scope='region' BEGIN SELECT RAISE(ABORT,'region failure'); END"
            )
        failed_save = self.native_request({
            "op": "capture.region",
            "sourceUrl": "https://example.test/failing-region",
            "title": "Fail after import",
        })
        self.assertFalse(failed_save["ok"], failed_save)
        self.assertEqual(list((self.data / "assets").glob("*")), [])

        with sqlite3.connect(self.data / "collection.sqlite3") as connection:
            connection.execute(
                "CREATE TRIGGER stall_region_asset_delete BEFORE DELETE ON assets BEGIN "
                "SELECT max(value) FROM (WITH RECURSIVE counter(value) AS "
                "(VALUES(0) UNION ALL SELECT value+1 FROM counter WHERE value<20000000) "
                "SELECT value FROM counter); END"
            )
        payload = json.dumps({
            "op": "capture.region",
            "sourceUrl": "https://example.test/deferred-region-cleanup",
            "title": "Deferred cleanup",
        }).encode()
        process = subprocess.Popen(
            [
                str(CANDIDATE), "--native", "--data-dir", str(self.data),
                "--helper-bin", str(HELPER), "--allowed-origin", ORIGIN, ORIGIN,
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.environment,
        )
        process.stdin.write(struct.pack("=I", len(payload)) + payload)
        process.stdin.close()
        time.sleep(0.15)
        self.assertIsNone(process.poll(), "region cleanup did not reach the delete trigger")
        (self.data / "assets").chmod(0o500)
        try:
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            process.wait(timeout=20)
        finally:
            (self.data / "assets").chmod(0o700)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        process.stdout.close()
        process.stderr.close()
        size = struct.unpack("=I", stdout[:4])[0]
        deferred = json.loads(stdout[4:4 + size])
        self.assertFalse(deferred["ok"], deferred)
        self.assertEqual(deferred.get("cleanupPending"), True)
        self.assertEqual(len(deferred.get("warnings", [])), 1)
        self.assertNotIn(str(self.root), deferred["warnings"][0])
        deferred_files = list((self.data / "assets").glob("*"))
        self.assertEqual(len(deferred_files), 1)

        recovered = self.candidate_request({
            "op": "asset.import",
            "data": base64.b64encode(PNG).decode(),
            "mime": "image/png",
            "name": "recovery.png",
        })
        self.assertTrue(recovered["ok"], recovered)
        self.assertFalse(deferred_files[0].exists())

    def test_timeout_malformed_helper_and_native_policy_leave_no_side_effects(self):
        pids = self.root / "adapter-pids"
        self.command(
            "wl-paste",
            "import os,pathlib,subprocess,time\n"
            "child=subprocess.Popen(['/usr/bin/sleep','30'])\n"
            f"pathlib.Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
            "time.sleep(30)\n",
        )
        started = time.monotonic()
        timed_out = self.candidate_request({"op": "clipboard.import"})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(timed_out, {"ok": False, "error": "wl-paste timed out"})
        for _ in range(30):
            states = [self.process_state(int(pid)) for pid in pids.read_text().split()]
            if all(state in (None, "Z") for state in states):
                break
            time.sleep(0.05)
        self.assertTrue(all(state in (None, "Z") for state in states), states)

        malformed = self.root / "malformed-helper"
        malformed.write_text("#!/bin/sh\nprintf 'not-json\\n'\n", encoding="utf-8")
        malformed.chmod(0o700)
        response = self.candidate_request({"op": "theme.get"}, malformed)
        self.assertEqual(response, {
            "ok": False,
            "error": "Desktop helper returned an invalid response",
        })

        before = set(self.root.rglob("copied-*"))
        for operation in (
            {"op": "theme.get"},
            {"op": "link.open", "url": "https://example.test"},
            {"op": "file.import", "path": str(self.root / "missing")},
            {"op": "clipboard.import"},
            {"op": "clipboard.copy", "id": "missing"},
        ):
            payload = json.dumps(operation).encode()
            completed = subprocess.run(
                [
                    str(CANDIDATE), "--native", "--data-dir", str(self.data),
                    "--helper-bin", str(HELPER), "--allowed-origin", ORIGIN, ORIGIN,
                ],
                input=struct.pack("=I", len(payload)) + payload,
                capture_output=True,
                env=self.environment,
                timeout=5,
                check=True,
            )
            size = struct.unpack("=I", completed.stdout[:4])[0]
            result = json.loads(completed.stdout[4:4 + size])
            self.assertEqual(result, {
                "ok": False,
                "error": "Operation is not available to browser clients",
            })
        self.assertEqual(set(self.root.rglob("copied-*")), before)

    def test_helper_descendants_cannot_hold_runner_pipes_open(self):
        success_pids = self.root / "successful-helper-pids"
        successful = self.root / "successful-forking-helper"
        successful.write_text(
            f"#!{sys.executable}\n"
            "import json,os,pathlib,sys,time\n"
            "json.load(sys.stdin)\n"
            "child=os.fork()\n"
            "if child == 0:\n"
            " time.sleep(30)\n"
            " os._exit(0)\n"
            f"pathlib.Path({str(success_pids)!r}).write_text(f'{{os.getpid()}} {{child}}')\n"
            "print(json.dumps({'ok':True,'result':{}}),flush=True)\n",
            encoding="utf-8",
        )
        successful.chmod(0o700)

        started = time.monotonic()
        response = self.candidate_request({"op": "theme.get"}, successful)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(response, {"ok": True, "result": {}})
        self.assert_processes_stopped(success_pids)

        timeout_pids = self.root / "timed-out-helper-pids"
        timed_out = self.root / "timed-out-forking-helper"
        timed_out.write_text(
            f"#!{sys.executable}\n"
            "import json,os,pathlib,sys,time\n"
            "json.load(sys.stdin)\n"
            "child=os.fork()\n"
            "if child == 0:\n"
            " time.sleep(30)\n"
            " os._exit(0)\n"
            f"pathlib.Path({str(timeout_pids)!r}).write_text(f'{{os.getpid()}} {{child}}')\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        timed_out.chmod(0o700)

        started = time.monotonic()
        response = self.candidate_request({"op": "theme.get"}, timed_out)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(response, {"ok": False, "error": "Desktop helper timed out"})
        self.assert_processes_stopped(timeout_pids)

    def assert_processes_stopped(self, path):
        for _ in range(30):
            states = [self.process_state(int(pid)) for pid in path.read_text().split()]
            if all(state in (None, "Z") for state in states):
                break
            time.sleep(0.05)
        self.assertTrue(all(state in (None, "Z") for state in states), states)

    @staticmethod
    def process_state(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[2]
        except (FileNotFoundError, IndexError, PermissionError):
            return None


if __name__ == "__main__":
    unittest.main()
