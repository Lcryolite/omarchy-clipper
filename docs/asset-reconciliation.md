# Managed asset reconciliation

Clipper records capture-owned imports until a capture completes, is superseded, or is abandoned. When an owned asset loses its last Clip, Draft, Trash, or capture reference, Clipper records it in `asset_cleanup` before attempting to remove its managed file. A failed or interrupted removal keeps that record for a later retry.

`asset.reconcile` is a bounded maintenance operation at the request protocol boundary. Without `confirm: true`, it is read-only:

```json
{"op":"asset.reconcile","limit":100}
```

The report classifies unreferenced assets as:

- `eligible`: durable cleanup ownership proves the asset may be removed after references and its managed path are checked again;
- `blocked`: cleanup is pending, but the stored path or file is unsafe to remove;
- `uncertain`: no durable ownership record proves that a legacy import was abandoned.

The inspection never treats age as ownership and never removes uncertain assets. Use `after` with the returned `nextAfter` value to continue a bounded inspection.
Inspection requires a quiescent, checkpointed Collection. Close every process with an active SQLite connection, including readers, before inspecting. If another connection or a non-empty SQLite WAL is present, inspection fails with a retry message instead of reading an incomplete snapshot. A missing Collection produces an empty report without creating directories, database files, sidecars, or asset storage.

Explicit reconciliation processes at most `limit` durable cleanup records:

```json
{"op":"asset.reconcile","limit":100,"confirm":true}
```

Before running this against an existing real Collection, stop Clipper writers, make a recoverable copy of `collection.sqlite3` (including any `-wal` and `-shm` files) and `assets/`, verify the copy can be read, and obtain explicit authorization for cleanup. Reconciliation rechecks every current reference under a write transaction. It removes only a validated managed filename and never follows links or scans arbitrary files. Repeating the command is safe after a crash or removal failure.

The `asset_cleanup` table is an additive compatibility change and does not change the existing schema version. Write-capable current versions create it with `CREATE TABLE IF NOT EXISTS`; read-only inspection probes `sqlite_master`, so Collections without the table remain readable and all unreferenced legacy assets are reported as `uncertain`. Each cleanup record retains the managed filename independently of `assets`, allowing a restart to finish file removal after metadata was committed.

Asset import also records an `asset_cleanup` row before publishing a managed file. The file and its containing directory are synchronized before the asset metadata and capture ownership are committed together and the recovery row is removed. A crash therefore leaves either a complete asset or a durable cleanup record that explicit reconciliation can safely retry. Files without such a record remain uncertain and are never inferred to be abandoned from their name alone.

Collection and asset access stays anchored to validated directory descriptors for the lifetime of a request or native connection. Replacing the display path after startup cannot redirect database, asset, or reconciliation access.

Older Clipper versions ignore `asset_cleanup`. Before rolling back to an older writer, stop current writers and run explicit reconciliation until `cleanupPending` is false; then open the same Collection with the older version. Returning to a newer version is safe because reconciliation checks all current references before removal and cancels stale cleanup records. Rollback uses the same Collection rather than restoring an older database snapshot, so later writes are preserved.
