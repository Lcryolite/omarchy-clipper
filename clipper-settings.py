#!/usr/bin/env python3
"""Manage Clipper preferences independently of the Omarchy bar layout."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile


DEFAULTS = {
    "language": "system", "previewEnabled": False, "previewSide": "right",
    "dataDir": "", "executable": "clipper",
}


def validate(values):
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys():
        raise ValueError("Invalid Clipper settings; repair the saved configuration before continuing.")
    result = {**DEFAULTS, **values}
    if result["language"] not in ("system", "zh", "en"):
        raise ValueError("Language must be system, zh, or en.")
    if type(result["previewEnabled"]) is not bool:
        raise ValueError("previewEnabled must be true or false.")
    if result["previewSide"] not in ("left", "right"):
        raise ValueError("Preview side must be left or right.")
    for key in ("dataDir", "executable"):
        if not isinstance(result[key], str) or "\0" in result[key]:
            raise ValueError(f"{key} must be a string.")
    if result["dataDir"] and not Path(result["dataDir"]).is_absolute():
        raise ValueError("dataDir must be an absolute directory path, or empty for the default.")
    if not result["executable"].strip():
        raise ValueError("executable must not be empty.")
    return result


def legacy_settings(config, supplied):
    path = config / "omarchy/shell.json"
    values = {}
    found = False
    if path.exists():
        document = json.loads(path.read_text())
        for entries in document.get("bar", {}).get("layout", {}).values():
            for entry in entries:
                if isinstance(entry, dict) and entry.get("id") == "lcryolite.clipper":
                    values = {k: v for k, v in entry.items() if k in DEFAULTS}
                    found = True
                    break
            if found:
                break
    # A live widget can supply its effective settings during the first import.
    if supplied is not None:
        if not isinstance(supplied, dict):
            raise ValueError("Legacy settings must be an object.")
        values.update({k: v for k, v in supplied.items() if k in DEFAULTS})
        found = True
    return validate(values), found


def write_settings(path, values):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"version": 1, "settings": values}, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def check_collection(values):
    if values["dataDir"]:
        directory = Path(values["dataDir"])
        if not directory.is_dir() or not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
            raise ValueError("Selected collection directory is unavailable; restore it or explicitly set dataDir. No other collection was opened.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("load", "migrate", "set"))
    parser.add_argument("key", nargs="?")
    parser.add_argument("value", nargs="?", help="JSON value for set")
    parser.add_argument("--legacy-json", help="Effective widget settings for first import only")
    parser.add_argument("--skip-path-checks", action="store_true", help="Inspect preferences without opening the collection")
    parser.add_argument("--config-dir", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
    args = parser.parse_args()
    try:
        config = args.config_dir
        directory = config / "omarchy-clipper"
        path = directory / "settings.json"
        pending_legacy = None
        if args.action == "migrate" and not path.exists():
            pending_legacy, found = legacy_settings(
                config, json.loads(args.legacy_json) if args.legacy_json else None)
            if not found:
                raise ValueError(
                    "No legacy Clipper settings were found. Restore a settings backup or "
                    "explicitly set each preference you know before retrying; Clipper will "
                    "not guess missing settings.")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / "settings.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existed = path.exists()
            if existed:
                document = json.loads(path.read_text())
                version = document.get("version") if isinstance(document, dict) else None
                if (not isinstance(document, dict) or isinstance(version, bool)
                        or not isinstance(version, (int, float)) or version != 1
                        or "settings" not in document):
                    raise ValueError("Invalid Clipper settings format; repair the saved configuration before continuing.")
                values = validate(document["settings"])
            else:
                values = pending_legacy
                if values is None:
                    values, _ = legacy_settings(
                        config, json.loads(args.legacy_json) if args.legacy_json else None)
            if args.action == "set":
                if args.key not in DEFAULTS or args.value is None:
                    raise ValueError("set requires a known setting and a JSON value.")
                values = validate({**values, args.key: json.loads(args.value)})
            if not existed or args.action == "set":
                write_settings(path, values)
            if args.action == "load" and not args.skip_path_checks:
                check_collection(values)
        print(json.dumps(values, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(f"Clipper settings: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
