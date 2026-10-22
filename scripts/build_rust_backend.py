#!/usr/bin/env python3
"""Build the locked Rust backend without exposing user Cargo credentials."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline", action="store_true",
        help="require all locked dependencies to exist in the registry cache",
    )
    parser.add_argument("--check", action="store_true", help="typecheck instead of linking")
    parser.add_argument(
        "--registry-cache",
        type=Path,
        default=Path(
            os.environ.get(
                "CLIPPER_CARGO_REGISTRY",
                str(Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry"),
            )
        ),
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="clipper-cargo-") as temporary:
        isolated = Path(temporary)
        cargo_home = isolated / "cargo"
        cargo_home.mkdir()
        home = isolated / "home"
        home.mkdir()
        if args.registry_cache.is_dir():
            (cargo_home / "registry").symlink_to(
                args.registry_cache.resolve(), target_is_directory=True
            )
        elif args.offline:
            parser.error(
                "registry cache is missing; populate the locked dependencies before an offline build"
            )
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "CARGO_HOME": str(cargo_home),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
            "LANG": "C.UTF-8",
        }
        command = [
            "cargo", "+stable", "check" if args.check else "build", "--release", "--locked",
            "--manifest-path", str(ROOT / "backend/Cargo.toml"),
        ]
        if args.offline:
            command.append("--offline")
        subprocess.run(command, cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
