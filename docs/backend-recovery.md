# Rust component repair

Rebuild and reinstall the fixed Rust application layout when an installed
component is missing or a matching build needs to be restored:

```sh
make all
make extension
python3 scripts/install.py \
  --chromium-extension-id aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --dry-run
```

Close Reader windows and wait for browser captures to finish, then run the same
installer command without `--dry-run`. The installer obtains the persistent
exclusive lifecycle lock at `PREFIX/share/.omarchy-clipper-backend.lock`
before replacing any component. Each replacement is atomic, and a later
failure restores the components and install state from before the command.

The repair reuses the current Collection in place. It does not restore, copy,
or convert the database, WAL files, assets, notes, or Trash state.

If a component was changed outside the installer, the ownership check rejects
the repair rather than overwriting it. Restore that component from the last
matching build, then rerun the installer. This ownership check does not apply
to Collection data.
