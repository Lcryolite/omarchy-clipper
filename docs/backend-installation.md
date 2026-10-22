# Rust installation

The standard build and installer install the Rust application together with
the C++ Article Reader, the bounded operation helper, and the native mixed-clip
detail window.

## Build

Build the application components and browser extension:

```sh
make all
make extension
```

The Rust build requires `rustup` with the stable toolchain installed because
the build command pins that toolchain with `cargo +stable`.

`make rust` uses `scripts/build_rust_backend.py`. The script gives Cargo an
isolated home and does not expose the user's Cargo credentials. To require an
already populated dependency cache and disable dependency downloads, run:

```sh
python3 scripts/build_rust_backend.py --offline
```

The installer also requires `wl-copy` and `wl-paste` from `wl-clipboard`.

## Install

Pass the extension ID reported by each browser:

```sh
python3 scripts/install.py \
  --chromium-extension-id aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Use `--prefix` and `--config-dir` to target an isolated user-owned layout. Add
`--dry-run` to validate sources, versions, ownership, paths, and destinations
without writing them.

The fixed component layout is:

```text
PREFIX/
  bin/
    clipper                                  Rust application
    clipper-native                           mixed-clip native detail window
    omarchy-clipper-host-chromium            Chromium native-host launcher
    omarchy-clipper-host-brave               Brave native-host launcher
  libexec/omarchy-clipper/
    clipper-reader                           C++ Article Reader
    clipper-helper                           bounded operation helper
  share/
    .omarchy-clipper-backend.lock            persistent lifetime lock
    omarchy-clipper/
      .install-state.json
      ui/...
```

Before writing, the installer checks one version across the plugin manifest,
browser extension, Rust application, Reader, helper, and native detail window.
It rejects symbolic links, non-user-owned destinations, and files that no
longer match their recorded ownership digest.

An installation recorded by the previous public C++ release can be upgraded
in place. The installer verifies its existing ownership records before
replacing `bin/clipper` and adding the Rust components. It does not copy,
replace, or convert the current Collection.

Each installed application and Reader process holds a shared lock on
`PREFIX/share/.omarchy-clipper-backend.lock`. The lock is outside the
Collection so older public versions do not interpret it as Collection content.
Install, repair, and uninstall require its exclusive lock and fail before
component writes while Clipper is active. Lifecycle commands never replace or
remove the lock file, so later operations synchronize on the same inode.
The `PREFIX/share` parent must be owned by the current user and must not be
writable by group or other users; the lock must be a single-link regular file
owned by the current user with mode `0600`.

The installer writes components atomically and records their SHA-256 ownership
in `.install-state.json`. If a later write fails, it restores the preceding
component files and install state. Collection databases, WAL files, assets,
and user content are outside the install transaction.

## Uninstall

```sh
python3 scripts/uninstall.py
```

Before deleting anything, uninstall refuses if any still-present recorded file
no longer matches its ownership digest. A recorded file that is already
missing is treated as already removed. Uninstall retains the Collection and
the persistent backend lock. Collection deletion remains a separate explicit
`--purge-data-dir` operation. Companion and install-state removal is
transactional: if one of those deletions fails, previously removed companion
files are restored before the command reports failure. Explicit Collection
purge is destructive and is not included in that rollback; a purge failure
reports that Collection cleanup may be incomplete.
