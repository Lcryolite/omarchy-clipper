"""Shared path, locking, and ownership rules for Clipper lifecycle commands."""

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat


HOST_NAME = "com.omarchy.clipper"
STATE_NAME = ".install-state.json"
BACKEND_LOCK_NAME = "backend.lock"
BROWSERS = {
    "chromium": ("chromium", "omarchy-clipper-host-chromium"),
    "brave": ("BraveSoftware/Brave-Origin-Nightly", "omarchy-clipper-host-brave"),
}
COMPONENT_FILES = {
    "bin/clipper-native",
    "libexec/omarchy-clipper/clipper-helper",
    "libexec/omarchy-clipper/clipper-reader",
}


class LifecycleError(Exception):
    pass


def acquire_backend_lock(prefix, *, create):
    """Hold the installed backend lifetime lock exclusively during mutation."""
    lock_path = absolute(prefix) / "share/omarchy-clipper" / BACKEND_LOCK_NAME
    ensure_no_symlinks(lock_path)
    if not create and not lock_path.exists():
        return None
    if create:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_no_symlinks(lock_path)

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise LifecycleError(f"cannot open backend lifetime lock: {lock_path}") from error

    lock = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        status = os.fstat(lock.fileno())
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or status.st_nlink != 1 or stat.S_IMODE(status.st_mode) != 0o600):
            raise LifecycleError(
                f"backend lifetime lock must be a private, single-link user-owned file: {lock_path}"
            )
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise LifecycleError(
                    "Clipper is active; close the Reader and browser capture operations "
                    "before installing, repairing, or uninstalling"
                ) from error
            raise LifecycleError(f"cannot lock backend lifetime lock: {lock_path}") from error
        return lock
    except Exception:
        lock.close()
        raise


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
    for key in ("resource_files", "plugin_files", "component_files"):
        values = state.get(key, []) if key == "component_files" else state.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise LifecycleError(f"{label} is invalid")
        for value in values:
            safe_relative(value)
            if key == "component_files" and value not in COMPONENT_FILES:
                raise LifecycleError(f"{label} is invalid")
        state[key] = values
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
