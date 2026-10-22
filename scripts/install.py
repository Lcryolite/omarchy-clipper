#!/usr/bin/env python3
"""Install built Clipper companion components into user-owned locations."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

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
    ownership_record,
    safe_relative,
    source_files as lifecycle_source_files,
    validate_state,
)

ROOT = Path(__file__).resolve().parents[1]
InstallError = LifecycleError


def validate_extension_id(value):
    if value is not None and not re.fullmatch("[a-p]{32}", value):
        raise InstallError("extension ID must contain 32 lowercase letters a through p")


def nearest_existing(path):
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def validate_destination(path, *, directory=False):
    path = absolute(path)
    ensure_no_symlinks(path)
    uid = os.getuid()
    if path.exists():
        status = path.stat()
        expected = stat.S_ISDIR(status.st_mode) if directory else stat.S_ISREG(status.st_mode)
        if not expected:
            kind = "directory" if directory else "regular file"
            raise InstallError(f"destination is not a {kind}: {path}")
        if status.st_uid != uid:
            raise InstallError(f"destination is not owned by the current user: {path}")
        writable = path if directory else path.parent
        if not os.access(writable, os.W_OK | os.X_OK):
            raise InstallError(f"destination is not writable: {path}")
    else:
        ancestor = nearest_existing(path.parent)
        if not ancestor.is_dir() or ancestor.stat().st_uid != uid:
            raise InstallError(f"destination parent is not owned by the current user: {path}")
        if not os.access(ancestor, os.W_OK | os.X_OK):
            raise InstallError(f"destination parent is not writable: {path}")
    return path


def source_files(directory):
    return lifecycle_source_files(directory, required=True, root=ROOT)


def load_state(path):
    if not path.exists():
        return {
            "version": 2,
            "component_files": [],
            "resource_files": [],
            "plugin_files": [],
            "owned_files": [],
        }
    validate_destination(path)
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError("existing install state is invalid") from error
    return validate_state(state, label="existing install state")


def read_manifest(path, label):
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"{label} is unavailable")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise InstallError(f"{label} is invalid")
    return value


def component_version(executable, label):
    try:
        with tempfile.TemporaryDirectory(prefix="clipper-version-check-") as temporary:
            temporary = Path(temporary)
            environment = {
                "HOME": str(temporary / "home"),
                "LANG": "C.UTF-8",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TMPDIR": str(temporary / "tmp"),
                "XDG_CACHE_HOME": str(temporary / "cache"),
                "XDG_CONFIG_HOME": str(temporary / "config"),
                "XDG_DATA_HOME": str(temporary / "data"),
                "XDG_RUNTIME_DIR": str(temporary / "runtime"),
                "XDG_STATE_HOME": str(temporary / "state"),
            }
            for key in ("LD_LIBRARY_PATH", "LIBGL_DRIVERS_PATH", "QT_PLUGIN_PATH"):
                if key in os.environ:
                    environment[key] = os.environ[key]
            for key in (
                "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
            ):
                Path(environment[key]).mkdir(parents=True, exist_ok=True)
            Path(environment["XDG_RUNTIME_DIR"]).chmod(0o700)
            completed = subprocess.run(
                [str(executable), "--version", "--data-dir", str(temporary / "collection")],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=environment,
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError(f"{label} version check failed") from error
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version:
        raise InstallError(f"{label} version check failed")
    return version


def check_components(binary, plugin_manifest, *, binary_version_prefix="", additional_components=()):
    extension_manifest = read_manifest(ROOT / "extension/manifest.json", "browser extension manifest")
    try:
        service_worker = safe_relative(extension_manifest["background"]["service_worker"])
        popup = safe_relative(extension_manifest["action"]["default_popup"])
        plugin_version = plugin_manifest["version"]
        extension_version = extension_manifest["version"]
    except (KeyError, TypeError, LifecycleError) as error:
        raise InstallError("browser extension manifest is invalid") from error
    required = (service_worker, Path("dist/content.js"), popup, Path("dist/popup.js"))
    if any((ROOT / "extension" / path).is_symlink() or not (ROOT / "extension" / path).is_file() for path in required):
        raise InstallError("build the complete browser extension before installing")
    if not shutil.which("wl-copy") or not shutil.which("wl-paste"):
        raise InstallError("wl-clipboard is required before installing")
    if not all(isinstance(value, str) and value for value in (plugin_version, extension_version)):
        raise InstallError("component versions are invalid")
    expected = plugin_version
    binary_version = component_version(binary, "Rust application")
    if binary_version_prefix and binary_version != binary_version_prefix + expected:
        raise InstallError("component versions do not match")
    versions = [plugin_version, extension_version, binary_version.removeprefix(binary_version_prefix)]
    for component, label in additional_components:
        versions.append(component_version(component, label))
    if any(value != expected for value in versions):
        raise InstallError("component versions do not match")


def write_file(path, content, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def copy_file(source, destination, mode=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def remove_stale(base, old_files, new_files, matching_owned):
    for relative in sorted(set(old_files) - set(new_files), reverse=True):
        target = base / safe_relative(relative)
        if target in matching_owned and target.exists():
            target.unlink()


class InstallRollback:
    def __init__(self, targets):
        self.targets = list(dict.fromkeys(targets))
        self.originals = {
            path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in self.targets
            if path.exists()
        }
        self.created_directories = set()
        for target in self.targets:
            parent = target.parent
            while not parent.exists():
                self.created_directories.add(parent)
                parent = parent.parent

    def restore(self):
        for target in reversed(self.targets):
            if target in self.originals:
                content, mode = self.originals[target]
                write_file(target, content, mode)
            elif target.exists():
                target.unlink()
        for directory in sorted(self.created_directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                if directory.exists() and any(directory.iterdir()):
                    continue
                raise


def legacy_installation(prefix, config, resources):
    binary = prefix / "bin/clipper"
    host = prefix / "bin/omarchy-clipper-host"
    reader = resources / "ui/Reader.qml"
    required = (binary, host, reader)
    if not all(path.exists() for path in required):
        return False
    for path in required:
        validate_destination(path)
    try:
        lines = host.read_text().splitlines()
        command = shlex.split(lines[1]) if len(lines) == 2 else []
    except (OSError, ValueError):
        return False
    if len(command) != 6 or command[:3] != ["exec", str(binary), "--native"]:
        return False
    if command[3] != "--allowed-origin" or command[5] != "$@":
        return False
    origin = command[4]
    if not re.fullmatch(r"chrome-extension://[a-p]{32}/", origin):
        return False

    found_manifest = False
    for channel, _ in BROWSERS.values():
        manifest_path = config / channel / "NativeMessagingHosts" / f"{HOST_NAME}.json"
        if not manifest_path.exists():
            continue
        found_manifest = True
        validate_destination(manifest_path)
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        expected = {
            "name": HOST_NAME,
            "description": "Local Omarchy Clipper capture host",
            "path": str(host),
            "type": "stdio",
            "allowed_origins": [origin],
        }
        if manifest != expected:
            return False
    return found_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-id", help="legacy ID used for both supported browsers")
    parser.add_argument("--chromium-extension-id", help="ID shown by Chromium for the unpacked extension")
    parser.add_argument("--brave-extension-id", help="ID shown by Brave for the unpacked extension")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    parser.add_argument("--config-dir", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--migrate-settings", action="store_true", help="Retain current widget settings before disabling the old plugin; no build required")
    args = parser.parse_args()
    rollback = None
    try:
        if args.migrate_settings:
            if args.dry_run:
                raise InstallError("settings migration is an explicit write; omit --dry-run to perform it")
            helper = ROOT / "clipper-settings.py"
            if not helper.is_file():
                helper = ROOT / "plugin/clipper-settings.py"
            result = subprocess.run([sys.executable, str(helper), "migrate", "--config-dir", str(args.config_dir)],
                                    capture_output=True, text=True)
            if result.returncode:
                raise InstallError(result.stderr.strip() or "could not retain Clipper settings")
            print("Clipper settings retained. It is now safe to disable the old widget.")
            return
        for value in (args.extension_id, args.chromium_extension_id, args.brave_extension_id):
            validate_extension_id(value)
        browser_ids = {
            "chromium": args.chromium_extension_id or args.extension_id,
            "brave": args.brave_extension_id or args.extension_id,
        }
        browser_ids = {browser: value for browser, value in browser_ids.items() if value}
        if not browser_ids:
            raise InstallError("configure at least one browser extension ID")

        reader_source = ROOT / "build/clipper-reader"
        helper_source = ROOT / "build/clipper-helper"
        backend_source = ROOT / "backend/target/release/omarchy-clipper-backend"
        native_helper_source = ROOT / "build/clipper-native"
        component_sources = [
            (reader_source, Path("libexec/omarchy-clipper/clipper-reader"), "C++ Reader"),
            (helper_source, Path("libexec/omarchy-clipper/clipper-helper"), "operation helper"),
            (native_helper_source, Path("bin/clipper-native"), "native detail helper"),
        ]
        required_programs = (backend_source, *(source for source, _, _ in component_sources))
        if any(
            source.is_symlink() or not source.is_file() or not os.access(source, os.X_OK)
            for source in required_programs
        ):
            raise InstallError("build the Rust application, Reader components, and extension before installing")

        root_manifest = ROOT / "manifest.json"
        private_manifest = ROOT / "plugin/manifest.json"
        if root_manifest.is_file() and not root_manifest.is_symlink():
            manifest_path = root_manifest
            plugin_source = None
            mode = "companion-only"
        elif private_manifest.is_file() and not private_manifest.is_symlink():
            manifest_path = private_manifest
            plugin_source = ROOT / "plugin"
            mode = "private-plugin-copy"
        else:
            raise InstallError("plugin manifest is unavailable")
        plugin_manifest = read_manifest(manifest_path, "plugin manifest")
        try:
            plugin_id = plugin_manifest["id"]
        except (KeyError, TypeError) as error:
            raise InstallError("plugin manifest is invalid") from error
        if not isinstance(plugin_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", plugin_id):
            raise InstallError("plugin manifest contains an invalid ID")
        check_components(
            backend_source,
            plugin_manifest,
            binary_version_prefix="omarchy-clipper-backend ",
            additional_components=(
                (source, label) for source, _, label in component_sources
            ),
        )

        ui_source = ROOT / "ui"
        ui_files = source_files(ui_source)
        if Path("Reader.qml") not in ui_files:
            raise InstallError("Reader resources are unavailable")
        resource_files = [Path("ui") / path for path in ui_files]
        plugin_files = source_files(plugin_source) if plugin_source else []

        prefix = absolute(args.prefix)
        config = absolute(args.config_dir)
        validate_destination(prefix, directory=True) if prefix.exists() else validate_destination(prefix / ".probe")
        validate_destination(config, directory=True) if config.exists() else validate_destination(config / ".probe")
        binary = prefix / "bin/clipper"
        components = [
            (source, prefix / relative, relative)
            for source, relative, _ in component_sources
        ]
        native_helper = prefix / "bin/clipper-native"
        resources = prefix / "share/omarchy-clipper"
        state_path = resources / STATE_NAME
        plugin = config / "omarchy/plugins" / plugin_id
        _backend_lock = (
            acquire_backend_lock(prefix, create=True) if not args.dry_run else None
        )
        state_exists = state_path.exists()
        existing_state = load_state(state_path)
        recorded_owned = []
        bases = {"prefix": prefix, "config": config}
        for record in existing_state["owned_files"]:
            path = bases[record["base"]] / safe_relative(record["path"])
            recorded_owned.append((record, path))

        copy_plugin = plugin_source is not None
        git_managed_plugin = False
        if plugin.exists():
            validate_destination(plugin, directory=True)
            git_marker = plugin / ".git"
            ensure_no_symlinks(git_marker)
            if git_marker.exists():
                git_managed_plugin = True
                copy_plugin = False
                mode = "companion-only"

        write_targets = [binary]
        write_targets.extend(destination for _, destination, _ in components)
        write_targets.extend(resources / relative for relative in resource_files)
        if copy_plugin:
            write_targets.extend(plugin / relative for relative in plugin_files)
        targets = [state_path, *write_targets]
        targets.extend(
            path for _, path in recorded_owned
            if not (git_managed_plugin and path.is_relative_to(plugin))
        )
        targets.extend(resources / relative for relative in resource_files)
        targets.extend(resources / safe_relative(relative) for relative in existing_state["resource_files"])
        targets.extend(prefix / safe_relative(relative) for relative in existing_state["component_files"])
        if copy_plugin:
            targets.extend(plugin / relative for relative in plugin_files)
            targets.extend(plugin / safe_relative(relative) for relative in existing_state["plugin_files"])
        native_hosts = {}
        for browser, extension_id in browser_ids.items():
            channel, script_name = BROWSERS[browser]
            host = prefix / "bin" / script_name
            manifest = config / channel / "NativeMessagingHosts" / f"{HOST_NAME}.json"
            targets.extend((host, manifest))
            write_targets.extend((host, manifest))
            native_hosts[browser] = {
                "manifest": str(manifest),
                "host": str(host),
                "allowed_origin": f"chrome-extension://{extension_id}/",
            }
        for target in targets:
            validate_destination(target)

        matching_owned = {
            path: record
            for record, path in recorded_owned
            if path.is_file()
            and file_digest(path) == record["sha256"]
            and not (git_managed_plugin and path.is_relative_to(plugin))
        }

        legacy = False
        legacy_owned = []
        if not state_exists:
            legacy_host = prefix / "bin/omarchy-clipper-host"
            known_destinations = list(dict.fromkeys(targets + [legacy_host]))
            if any(path.exists() for path in known_destinations):
                if native_helper.exists():
                    raise InstallError("existing destination is not a recognized Clipper installation")
                legacy = legacy_installation(prefix, config, resources)
                if not legacy:
                    raise InstallError("existing destination is not a recognized Clipper installation")
                legacy_owned.append(legacy_host)
                legacy_owned.extend(
                    config / channel / "NativeMessagingHosts" / f"{HOST_NAME}.json"
                    for channel, _ in BROWSERS.values()
                    if (config / channel / "NativeMessagingHosts" / f"{HOST_NAME}.json").is_file()
                )
        else:
            for target in write_targets:
                if target.exists() and target not in matching_owned:
                    raise InstallError("existing destination does not match recorded ownership")

        plan = {
            "mode": mode,
            "migration": "legacy" if legacy else None,
            "binary": str(binary),
            "components": {
                relative.name: str(destination)
                for _, destination, relative in components
            },
            "native_helper": str(native_helper),
            "resources": str(resources),
            "plugin": str(plugin) if copy_plugin else None,
            "native_hosts": native_hosts,
        }
        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return

        rollback_targets = [state_path, *write_targets]
        rollback_targets.extend(
            target
            for relative in set(existing_state["resource_files"]) - {str(path) for path in resource_files}
            if (target := resources / safe_relative(relative)) in matching_owned
        )
        if copy_plugin:
            rollback_targets.extend(
                target
                for relative in set(existing_state["plugin_files"]) - {str(path) for path in plugin_files}
                if (target := plugin / safe_relative(relative)) in matching_owned
            )
        rollback = InstallRollback(rollback_targets)
        copy_file(backend_source, binary, 0o755)
        for source, destination, _ in components:
            copy_file(source, destination, 0o755)
        for source_relative, destination_relative in zip(ui_files, resource_files):
            copy_file(ui_source / source_relative, resources / destination_relative)
        remove_stale(
            resources,
            existing_state["resource_files"],
            [str(path) for path in resource_files],
            matching_owned,
        )

        installed_plugin_files = list(existing_state["plugin_files"])
        if git_managed_plugin:
            installed_plugin_files = []
        if copy_plugin:
            for relative in plugin_files:
                copy_file(plugin_source / relative, plugin / relative)
            remove_stale(
                plugin,
                installed_plugin_files,
                [str(path) for path in plugin_files],
                matching_owned,
            )
            installed_plugin_files = [str(path) for path in plugin_files]

        installed_hosts = []
        for details in native_hosts.values():
            origin = details["allowed_origin"]
            host = Path(details["host"])
            script = "#!/bin/sh\nexec " + shlex.quote(str(binary)) + " --native --allowed-origin " + shlex.quote(origin) + ' "$@"\n'
            write_file(host, script.encode(), 0o755)
            manifest = {
                "name": HOST_NAME,
                "description": "Local Omarchy Clipper capture host",
                "path": str(host),
                "type": "stdio",
                "allowed_origins": [origin],
            }
            write_file(Path(details["manifest"]), (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
            installed_hosts.extend((host, Path(details["manifest"])))

        owned_records = {
            (record["base"], record["path"]): record
            for path, record in matching_owned.items()
            if path.is_file()
            and file_digest(path) == record["sha256"]
        }
        current_owned = [("prefix", binary)]
        current_owned.extend(("prefix", destination) for _, destination, _ in components)
        current_owned.extend(("prefix", resources / relative) for relative in resource_files)
        if copy_plugin:
            current_owned.extend(("config", plugin / safe_relative(relative)) for relative in installed_plugin_files)
        current_owned.extend(
            ("prefix" if path.is_relative_to(prefix) else "config", path)
            for path in installed_hosts + legacy_owned
            if path.is_file()
        )
        for base_name, path in current_owned:
            base = prefix if base_name == "prefix" else config
            record = ownership_record(path, base_name, base)
            owned_records[(record["base"], record["path"])] = record
        state = {
            "version": 2,
            "component_files": sorted(
                record[1]
                for record in owned_records
                if record[0] == "prefix" and record[1] in {
                    "bin/clipper-native",
                    "libexec/omarchy-clipper/clipper-helper",
                    "libexec/omarchy-clipper/clipper-reader",
                }
            ),
            "resource_files": [str(path) for path in resource_files],
            "plugin_files": installed_plugin_files,
            "owned_files": sorted(owned_records.values(), key=lambda item: (item["base"], item["path"])),
        }
        write_file(state_path, (json.dumps(state, indent=2) + "\n").encode(), 0o600)
        rollback = None
        print(json.dumps(plan, indent=2))
        print(f"Installed companion components for {plugin_id}. Reload each configured browser extension manually.")
    except (InstallError, OSError) as error:
        if rollback is not None:
            try:
                rollback.restore()
            except OSError:
                parser.error("installation failed and the prior installation could not be restored")
        message = str(error) if isinstance(error, InstallError) else "failed to install companion components"
        parser.error(message)


if __name__ == "__main__":
    main()
