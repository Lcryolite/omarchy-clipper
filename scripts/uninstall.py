#!/usr/bin/env python3
"""Remove Clipper-owned companion files while retaining collection data by default."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from urllib.parse import quote

sys.dont_write_bytecode = True

from lifecycle import (
    BROWSERS,
    HOST_NAME,
    STATE_NAME,
    LifecycleError,
    acquire_backend_lock,
    absolute,
    ensure_no_symlinks,
    file_digest,
    recognized_host_pair,
    safe_relative,
    source_files,
    validate_state,
)

ROOT = Path(__file__).resolve().parents[1]
UninstallError = LifecycleError


def validate_existing_file(path):
    ensure_no_symlinks(path)
    if not path.exists():
        return
    status = path.stat()
    if not stat.S_ISREG(status.st_mode):
        raise UninstallError(f"target is not a regular file: {path}")
    if status.st_uid != os.getuid():
        raise UninstallError(f"target is not owned by the current user: {path}")
    if not os.access(path.parent, os.W_OK | os.X_OK):
        raise UninstallError(f"target parent is not writable: {path}")


def load_state(path):
    if not path.exists():
        return None
    validate_existing_file(path)
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise UninstallError("install state is invalid; refusing partial removal") from error
    return validate_state(state)


def plugin_identity():
    for manifest in (ROOT / "manifest.json", ROOT / "plugin/manifest.json"):
        if not manifest.is_file() or manifest.is_symlink():
            continue
        try:
            value = json.loads(manifest.read_text())["id"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]+", value):
            return value
    return "lcryolite.clipper"


def root_layout_plugin_files():
    if not (ROOT / "manifest.json").is_file():
        return []
    files = [Path("manifest.json")]
    files.extend(
        Path(name) for name in ("BarWidget.qml", "Panel.qml", "PlainText.qml")
        if (ROOT / name).is_file()
    )
    files.extend(Path("assets") / path for path in source_files(ROOT / "assets"))
    return files


def same_file(path, source):
    return (
        not path.is_symlink()
        and not source.is_symlink()
        and path.is_file()
        and source.is_file()
        and file_digest(path) == file_digest(source)
    )


def recognized_legacy_files(prefix, config, resources, plugin, git_managed_plugin):
    recognized = []
    binary = prefix / "bin/clipper"
    reader = resources / "ui/Reader.qml"
    source_binary = ROOT / "build/clipper"
    source_reader = ROOT / "ui/Reader.qml"
    companion_matches = same_file(binary, source_binary) and same_file(reader, source_reader)
    if companion_matches:
        host_pairs = []
        legacy_host = prefix / "bin/omarchy-clipper-host"
        for channel, script_name in BROWSERS.values():
            manifest = config / channel / "NativeMessagingHosts" / f"{HOST_NAME}.json"
            current_host = prefix / "bin" / script_name
            if recognized_host_pair(current_host, manifest, binary):
                host_pairs.extend((current_host, manifest))
            elif recognized_host_pair(legacy_host, manifest, binary):
                host_pairs.extend((legacy_host, manifest))
        if host_pairs:
            recognized.extend((binary, *host_pairs))
            for relative in source_files(ROOT / "ui"):
                destination = resources / "ui" / relative
                if same_file(destination, ROOT / "ui" / relative):
                    recognized.append(destination)

    if not git_managed_plugin:
        if (ROOT / "plugin/manifest.json").is_file():
            plugin_source = ROOT / "plugin"
            plugin_files = source_files(plugin_source)
            source_for = lambda relative: plugin_source / relative
        else:
            plugin_files = root_layout_plugin_files()
            source_for = lambda relative: ROOT / relative
        manifest_relative = Path("manifest.json")
        if manifest_relative in plugin_files and same_file(
            plugin / manifest_relative, source_for(manifest_relative)
        ):
            recognized.extend(
                plugin / relative
                for relative in plugin_files
                if same_file(plugin / relative, source_for(relative))
            )
    return list(dict.fromkeys(recognized))


def has_legacy_registration(prefix, config):
    binary = prefix / "bin/clipper"
    hosts = [prefix / "bin/omarchy-clipper-host"]
    hosts.extend(prefix / "bin" / script_name for _, script_name in BROWSERS.values())
    return any(
        recognized_host_pair(
            host,
            config / channel / "NativeMessagingHosts" / f"{HOST_NAME}.json",
            binary,
        )
        for channel, _ in BROWSERS.values()
        for host in hosts
    )


def collection_files(collection, prefix, config):
    collection = absolute(collection)
    ensure_no_symlinks(collection)
    broad_targets = {
        Path("/"), absolute(Path.home()), prefix, config, prefix / "share", config / "omarchy",
    }
    if collection in broad_targets:
        raise UninstallError("purge target is too broad to be a Clipper collection")
    if not collection.is_dir() or collection.stat().st_uid != os.getuid():
        raise UninstallError("purge target is not a user-owned Clipper collection")
    database = collection / "collection.sqlite3"
    validate_existing_file(database)
    if not database.is_file():
        raise UninstallError("purge target is not a verified Clipper collection")
    try:
        uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"clips", "draft", "assets", "clip_assets", "captures"}.issubset(tables):
                raise UninstallError("purge target is not a verified Clipper collection")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
            if not {"id", "filename"}.issubset(columns):
                raise UninstallError("purge target is not a verified Clipper collection")
            filenames = [row[0] for row in connection.execute("SELECT filename FROM assets")]
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as error:
        raise UninstallError("purge target is not a readable Clipper collection") from error

    owned_name = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?:png|jpg|gif|webp|bmp)"
    )
    assets = []
    for filename in filenames:
        if not isinstance(filename, str) or not owned_name.fullmatch(filename):
            raise UninstallError("Clipper collection contains an unsafe asset filename")
        assets.append(collection / "assets" / filename)
    files = assets + [
        database,
        collection / "collection.sqlite3-wal",
        collection / "collection.sqlite3-shm",
        collection / "collection.sqlite3-journal",
    ]
    for path in files:
        validate_existing_file(path)
    return files


def remove_empty_owned_directories(files, boundaries):
    directories = {path.parent for path in files}
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        if directory in boundaries:
            continue
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def restore_file(path, content, mode):
    """Atomically restore one companion file in its original directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class CompanionRemoval:
    """Remove companion files as one recoverable lifecycle operation."""

    def __init__(self, targets):
        self.originals = {
            path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in dict.fromkeys(targets)
            if path.exists()
        }
        self.removed = []

    def remove(self):
        for path in self.originals:
            path.unlink()
            self.removed.append(path)

    def restore(self):
        for path in reversed(self.removed):
            content, mode = self.originals[path]
            restore_file(path, content, mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    parser.add_argument("--config-dir", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
    parser.add_argument("--purge-data-dir", type=Path, help="explicit Clipper collection whose owned data files should be removed")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        prefix = absolute(args.prefix)
        config = absolute(args.config_dir)
        ensure_no_symlinks(prefix)
        ensure_no_symlinks(config)
        resources = prefix / "share/omarchy-clipper"
        state_path = resources / STATE_NAME
        state = load_state(state_path)
        plugin = config / "omarchy/plugins" / plugin_identity()
        git_managed_plugin = (plugin / ".git").exists()
        ensure_no_symlinks(plugin / ".git")

        owned = []
        if state:
            bases = {"prefix": prefix, "config": config}
            for record in state["owned_files"]:
                path = bases[record["base"]] / safe_relative(record["path"])
                if git_managed_plugin and path.is_relative_to(plugin):
                    continue
                validate_existing_file(path)
                if not path.exists():
                    continue
                try:
                    digest = file_digest(path)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise UninstallError(
                        f"installed file could not be verified; installed files were not changed: "
                        f"{path}"
                    ) from error
                if digest != record["sha256"]:
                    raise UninstallError(
                        f"installed file does not match recorded ownership; "
                        f"installed files were not changed: {path}"
                    )
                owned.append(path)
        else:
            owned.extend(recognized_legacy_files(prefix, config, resources, plugin, git_managed_plugin))
            if has_legacy_registration(prefix, config) and (prefix / "bin/clipper") not in owned:
                raise UninstallError(
                    "legacy companion files do not match this source; use the matching older source "
                    "or run the installer once to migrate ownership before uninstalling"
                )
        if state:
            owned.append(state_path)
        purge = collection_files(args.purge_data_dir, prefix, config) if args.purge_data_dir else []
        companions = list(dict.fromkeys(owned))
        candidates = list(dict.fromkeys(companions + purge))
        for path in candidates:
            validate_existing_file(path)

        plan = {
            "remove": [str(path) for path in candidates if path.exists()],
            "purge_data_dir": str(absolute(args.purge_data_dir)) if args.purge_data_dir else None,
        }
        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return

        _backend_lock = acquire_backend_lock(prefix, create=False)
        boundaries = {prefix, config, resources, plugin}
        removal = None
        try:
            removal = CompanionRemoval(companions)
            removal.remove()
        except OSError as error:
            if removal is None or not removal.removed:
                raise UninstallError(
                    "could not prepare companion removal; installed files were not changed"
                ) from error
            try:
                removal.restore()
            except OSError as restore_error:
                raise UninstallError(
                    "companion removal failed and the prior installation could not be restored"
                ) from restore_error
            raise UninstallError(
                "companion removal failed; the prior installation was restored"
            ) from error

        purged = []
        try:
            for path in purge:
                if path.exists():
                    path.unlink()
                    purged.append(path)
        except OSError as error:
            remove_empty_owned_directories(removal.removed + purged, boundaries)
            raise UninstallError(
                "companion files were removed, but Collection removal is incomplete"
            ) from error

        removed = removal.removed + purged
        remove_empty_owned_directories(removed, boundaries)
        print(json.dumps(plan, indent=2))
        if args.purge_data_dir:
            print("Removed Clipper companion files and the selected collection data files.")
        else:
            print("Removed Clipper companion files. Collection data was retained.")
    except UninstallError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
