"""Shared path and ownership rules for Clipper lifecycle commands."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex


HOST_NAME = "com.omarchy.clipper"
STATE_NAME = ".install-state.json"
BROWSERS = {
    "chromium": ("chromium", "omarchy-clipper-host-chromium"),
    "brave": ("BraveSoftware/Brave-Origin-Nightly", "omarchy-clipper-host-brave"),
}


class LifecycleError(Exception):
    pass


def absolute(path):
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def ensure_no_symlinks(path):
    path = absolute(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LifecycleError(f"path contains a symbolic link: {path}")


def safe_relative(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise LifecycleError("install state contains an unsafe relative path")
    return path


def source_files(directory, *, required=False, root=None):
    if not directory.is_dir() or directory.is_symlink():
        if required:
            name = directory.relative_to(root) if root else directory
            raise LifecycleError(f"required source directory is unavailable: {name}")
        return []
    files = []
    for candidate in sorted(directory.rglob("*")):
        name = candidate.relative_to(root) if root else candidate
        if candidate.is_symlink():
            raise LifecycleError(f"source contains a symbolic link: {name}")
        if candidate.is_file():
            files.append(candidate.relative_to(directory))
        elif not candidate.is_dir():
            raise LifecycleError(f"source contains an unsupported entry: {name}")
    return files


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_state(state, *, label="install state"):
    if not isinstance(state, dict):
        raise LifecycleError(f"{label} is invalid")
    version = state.get("version")
    if version == 1:
        raise LifecycleError(
            "unsupported development install state; installed files were not changed"
        )
    if version != 2:
        raise LifecycleError(f"{label} has an unsupported version")
    for key in ("resource_files", "plugin_files"):
        values = state.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise LifecycleError(f"{label} is invalid")
        for value in values:
            safe_relative(value)
    records = state.get("owned_files")
    if not isinstance(records, list):
        raise LifecycleError(f"{label} is invalid")
    for record in records:
        if not isinstance(record, dict) or set(record) != {"base", "path", "sha256"}:
            raise LifecycleError(f"{label} is invalid")
        if record["base"] not in ("prefix", "config"):
            raise LifecycleError(f"{label} is invalid")
        safe_relative(record["path"])
        if not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
            raise LifecycleError(f"{label} is invalid")
    return state


def ownership_record(path, base_name, base):
    return {
        "base": base_name,
        "path": path.relative_to(base).as_posix(),
        "sha256": file_digest(path),
    }


def recognized_host_pair(host, manifest_path, binary):
    if host.is_symlink() or manifest_path.is_symlink() or not host.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
        host_script = host.read_text()
    except (OSError, json.JSONDecodeError):
        return False
    origins = manifest.get("allowed_origins")
    if not isinstance(origins, list) or len(origins) != 1:
        return False
    origin = origins[0]
    if not isinstance(origin, str) or not re.fullmatch(r"chrome-extension://[a-p]{32}/", origin):
        return False
    expected_manifest = {
        "name": HOST_NAME,
        "description": "Local Omarchy Clipper capture host",
        "path": str(host),
        "type": "stdio",
        "allowed_origins": [origin],
    }
    expected_script = (
        "#!/bin/sh\nexec " + shlex.quote(str(binary)) + " --native --allowed-origin "
        + shlex.quote(origin) + ' "$@"\n'
    )
    return manifest == expected_manifest and host_script == expected_script
