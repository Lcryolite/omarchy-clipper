use crate::store::{new_id, now, percent_encode_path, storage_error};
use base64::alphabet;
use base64::engine::{DecodePaddingMode, GeneralPurpose, GeneralPurposeConfig};
use base64::Engine as _;
use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde_json::{json, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

const MAX_ASSET_BYTES: u64 = 12 * 1024 * 1024;
const MAX_ENCODED_ASSET_BYTES: usize = 16 * 1024 * 1024 + 16;
#[cfg(unix)]
const O_NOFOLLOW: i32 = 0o400000;

pub fn import(
    connection: &mut Connection,
    assets_dir: &Path,
    assets_display_dir: &Path,
    helper_bin: &Path,
    request: &Value,
) -> Value {
    let Some(data) = request.get("data").and_then(Value::as_str) else {
        return failure("data must be base64 text".to_owned());
    };
    if !request.get("mime").is_some_and(Value::is_string) {
        return failure("mime is required".to_owned());
    }
    if data.encode_utf16().count() > MAX_ENCODED_ASSET_BYTES {
        return failure("Asset exceeds the 12 MiB limit".to_owned());
    }
    let bytes = match decode_asset(data) {
        Ok(bytes) => bytes,
        Err(error) => return failure(error),
    };
    if bytes.len() as u64 > MAX_ASSET_BYTES {
        return failure("Asset exceeds the 12 MiB limit".to_owned());
    }
    let inspection = match crate::helper::request(
        helper_bin,
        &json!({
            "op": "image.inspect",
            "data": request.get("data").cloned().unwrap_or(Value::Null),
            "mime": request.get("mime").cloned().unwrap_or(Value::Null),
        }),
        "image inspection helper",
    ) {
        Ok(response) => response,
        Err(error) => return failure(error),
    };
    if inspection.get("ok").and_then(Value::as_bool) != Some(true) {
        return inspection;
    }
    // Serialize imports across their two committed transactions. Reconciliation does not take
    // this lock, so it can still cancel an intent before the publishing transaction starts.
    let _import_lock = match lock_asset_imports(assets_dir) {
        Ok(lock) => lock,
        Err(error) => return failure(error),
    };
    let cleanup = match resume_pending_cleanup(connection, assets_dir) {
        Ok(cleanup) => cleanup,
        Err(error) => return failure(error),
    };
    let response = match import_inspected(
        connection,
        assets_dir,
        assets_display_dir,
        request,
        &inspection,
        bytes,
    ) {
        Ok(result) => json!({"ok": true, "result": result}),
        Err(error) => failure(error),
    };
    attach_cleanup_status(response, cleanup.warnings)
}

pub fn read(connection: &Connection, assets_dir: &Path, request: &Value) -> Value {
    match read_managed(connection, assets_dir, request) {
        Ok(result) => json!({"ok": true, "result": result}),
        Err(error) => failure(error),
    }
}

pub fn reconcile(connection: &mut Connection, assets_dir: &Path, request: &Value) -> Value {
    let limit = request
        .get("limit")
        .and_then(Value::as_i64)
        .unwrap_or(100)
        .clamp(1, 500) as usize;
    if request.get("confirm").and_then(Value::as_bool) == Some(true) {
        match pending_cleanup_ids(connection, limit) {
            Ok(ids) => {
                let cleanup = remove_orphan_files(connection, assets_dir, PlannedCleanup { ids });
                let result = json!({
                    "mode": "reconcile",
                    "removed": cleanup.removed,
                    "preserved": cleanup.preserved,
                    "warnings": cleanup.warnings,
                    "cleanupPending": cleanup_pending(connection).unwrap_or(true),
                });
                json!({"ok": true, "result": result})
            }
            Err(error) => failure(error),
        }
    } else {
        match inspect_unreferenced(connection, assets_dir, request, limit) {
            Ok(result) => json!({"ok": true, "result": result}),
            Err(error) => failure(error),
        }
    }
}

pub(crate) struct PlannedCleanup {
    ids: Vec<String>,
}

pub(crate) fn plan_orphan_cleanup(
    connection: &Connection,
    candidates: &[String],
) -> Result<PlannedCleanup, String> {
    let mut unique = Vec::new();
    for id in candidates {
        if unique.contains(id) {
            continue;
        }
        unique.push(id.clone());
        if asset_is_protected(connection, id)? {
            connection
                .execute("DELETE FROM asset_cleanup WHERE asset_id=?", params![id])
                .map_err(|error| storage_error("Could not cancel asset cleanup", error))?;
            continue;
        }
        let exists = connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM assets WHERE id=?)",
                params![id],
                |row| row.get::<_, bool>(0),
            )
            .map_err(|error| storage_error("Could not inspect an unused asset", error))?;
        if exists {
            let requested_at = now(connection)?;
            connection
                .execute(
                    "INSERT INTO asset_cleanup(asset_id,filename,requested_at) \
                     SELECT id,filename,? FROM assets WHERE id=? \
                     ON CONFLICT(asset_id) DO NOTHING",
                    params![requested_at, id],
                )
                .map_err(|error| storage_error("Could not retain asset cleanup", error))?;
        }
    }
    Ok(PlannedCleanup { ids: unique })
}

pub(crate) struct CleanupResult {
    pub(crate) removed: Vec<String>,
    pub(crate) preserved: Vec<String>,
    pub(crate) warnings: Vec<String>,
}

pub(crate) fn remove_orphan_files(
    connection: &mut Connection,
    assets_dir: &Path,
    cleanup: PlannedCleanup,
) -> CleanupResult {
    let mut result = CleanupResult {
        removed: Vec::new(),
        preserved: Vec::new(),
        warnings: Vec::new(),
    };
    for id in cleanup.ids {
        match remove_one_orphan(connection, assets_dir, &id) {
            Ok(CleanupOutcome::Removed) => result.removed.push(id),
            Ok(CleanupOutcome::Preserved) => result.preserved.push(id),
            Ok(CleanupOutcome::NotPending) => {}
            Err(_) => result
                .warnings
                .push(format!("Could not remove deferred orphan asset: {id}")),
        }
    }
    result
}

fn resume_pending_cleanup(
    connection: &mut Connection,
    assets_dir: &Path,
) -> Result<CleanupResult, String> {
    let ids = pending_cleanup_ids(connection, 500)?;
    Ok(remove_orphan_files(
        connection,
        assets_dir,
        PlannedCleanup { ids },
    ))
}

pub(crate) fn success_envelope(result: Value, warnings: Vec<String>) -> Value {
    attach_cleanup_status(json!({"ok": true, "result": result}), warnings)
}

pub(crate) fn attach_cleanup_status(mut envelope: Value, warnings: Vec<String>) -> Value {
    if !warnings.is_empty() {
        if let Some(object) = envelope.as_object_mut() {
            object.insert("cleanupPending".to_owned(), Value::Bool(true));
            object.insert(
                "warnings".to_owned(),
                Value::Array(warnings.into_iter().map(Value::String).collect()),
            );
        }
    }
    envelope
}

fn asset_is_protected(connection: &Connection, id: &str) -> Result<bool, String> {
    let linked = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM clip_assets WHERE asset_id=?)",
            params![id],
            |row| row.get::<_, bool>(0),
        )
        .map_err(|error| storage_error("Could not inspect asset ownership", error))?;
    if linked {
        return Ok(true);
    }
    let draft: String = connection
        .query_row("SELECT asset_ids FROM draft WHERE id=1", [], |row| {
            row.get(0)
        })
        .map_err(|error| storage_error("Could not inspect Draft assets", error))?;
    if encoded_ids_contain(&draft, id) {
        return Ok(true);
    }
    let mut statement = connection
        .prepare("SELECT asset_ids FROM captures")
        .map_err(|error| storage_error("Could not inspect pending capture assets", error))?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|error| storage_error("Could not inspect pending capture assets", error))?;
    for encoded in rows {
        let encoded = encoded
            .map_err(|error| storage_error("Could not inspect pending capture assets", error))?;
        if encoded_ids_contain(&encoded, id) {
            return Ok(true);
        }
    }
    Ok(false)
}

enum CleanupOutcome {
    Removed,
    Preserved,
    NotPending,
}

enum CleanupPath {
    Present(PathBuf),
    Missing,
}

fn remove_one_orphan(
    connection: &mut Connection,
    assets_dir: &Path,
    id: &str,
) -> Result<CleanupOutcome, String> {
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| storage_error("Could not start asset cleanup", error))?;
    let pending_filename = transaction
        .query_row(
            "SELECT filename FROM asset_cleanup WHERE asset_id=?",
            params![id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| storage_error("Could not inspect asset cleanup", error))?;
    let Some(pending_filename) = pending_filename else {
        transaction
            .commit()
            .map_err(|error| storage_error("Could not finish asset cleanup", error))?;
        return Ok(CleanupOutcome::NotPending);
    };
    if asset_is_protected(&transaction, id)? {
        transaction
            .execute("DELETE FROM asset_cleanup WHERE asset_id=?", params![id])
            .map_err(|error| storage_error("Could not cancel asset cleanup", error))?;
        transaction
            .commit()
            .map_err(|error| storage_error("Could not finish asset cleanup", error))?;
        return Ok(CleanupOutcome::Preserved);
    }
    let metadata = transaction
        .query_row(
            "SELECT mime,filename FROM assets WHERE id=?",
            params![id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|error| storage_error("Could not inspect an unused asset", error))?;
    if let Some((mime, filename)) = metadata.as_ref() {
        if filename != &pending_filename {
            return Err("Managed asset cleanup metadata is invalid".to_owned());
        }
        checked_cleanup_path(assets_dir, id, Some(mime), filename)?;
        transaction
            .execute("DELETE FROM assets WHERE id=?", params![id])
            .map_err(|error| storage_error("Could not remove unused asset metadata", error))?;
    } else {
        checked_cleanup_path(assets_dir, id, None, &pending_filename)?;
    }
    transaction
        .commit()
        .map_err(|error| storage_error("Could not finish asset cleanup", error))?;

    if let CleanupPath::Present(path) =
        checked_cleanup_path(assets_dir, id, None, &pending_filename)?
    {
        fs::remove_file(path).map_err(|_| "Could not remove an unused managed asset".to_owned())?;
        sync_directory(&confined_assets_directory(assets_dir)?)
            .map_err(|_| "Could not remove an unused managed asset".to_owned())?;
    }
    let finish = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| storage_error("Could not finish asset cleanup", error))?;
    let completed = finish
        .execute("DELETE FROM asset_cleanup WHERE asset_id=?", params![id])
        .map_err(|error| storage_error("Could not finish asset cleanup", error))?;
    finish
        .commit()
        .map_err(|error| storage_error("Could not finish asset cleanup", error))?;
    Ok(if completed == 1 {
        CleanupOutcome::Removed
    } else {
        CleanupOutcome::NotPending
    })
}

fn checked_cleanup_path(
    assets_dir: &Path,
    id: &str,
    mime: Option<&str>,
    filename: &str,
) -> Result<CleanupPath, String> {
    let stored_extension = filename.rsplit_once('.').map(|(_, extension)| extension);
    let expected_extension = match mime {
        Some(mime) => Some(
            extension_for_mime(mime)
                .ok_or_else(|| "Managed asset metadata is invalid".to_owned())?,
        ),
        None => stored_extension
            .filter(|extension| matches!(*extension, "png" | "jpg" | "gif" | "webp" | "bmp")),
    };
    if expected_extension.is_none()
        || filename != format!("{id}.{}", expected_extension.unwrap())
        || !single_filename(filename)
    {
        return Err("Managed asset metadata is invalid".to_owned());
    }
    let directory = confined_assets_directory(assets_dir)?;
    let path = directory.join(filename);
    match fs::symlink_metadata(&path) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err("Managed asset storage is invalid".to_owned());
            }
            #[cfg(unix)]
            {
                let directory_metadata = fs::metadata(&directory)
                    .map_err(|_| "Managed asset storage is invalid".to_owned())?;
                if metadata.uid() != directory_metadata.uid() || metadata.nlink() != 1 {
                    return Err("Managed asset storage is invalid".to_owned());
                }
            }
            Ok(CleanupPath::Present(path))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(CleanupPath::Missing),
        Err(_) => Err("Managed asset storage is invalid".to_owned()),
    }
}

fn pending_cleanup_ids(connection: &Connection, limit: usize) -> Result<Vec<String>, String> {
    let mut statement = connection
        .prepare("SELECT asset_id FROM asset_cleanup ORDER BY asset_id LIMIT ?")
        .map_err(|error| storage_error("Could not inspect asset cleanup", error))?;
    let rows = statement
        .query_map(params![limit as i64], |row| row.get::<_, String>(0))
        .map_err(|error| storage_error("Could not inspect asset cleanup", error))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| storage_error("Could not inspect asset cleanup", error))
}

fn cleanup_pending(connection: &Connection) -> Result<bool, String> {
    connection
        .query_row("SELECT EXISTS(SELECT 1 FROM asset_cleanup)", [], |row| {
            row.get::<_, bool>(0)
        })
        .map_err(|error| storage_error("Could not inspect asset cleanup", error))
}

fn inspect_unreferenced(
    connection: &Connection,
    assets_dir: &Path,
    request: &Value,
    limit: usize,
) -> Result<Value, String> {
    let after = request.get("after").and_then(Value::as_str).unwrap_or("");
    let has_cleanup_table = connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_cleanup')",
            [],
            |row| row.get::<_, bool>(0),
        )
        .map_err(|error| storage_error("Could not inspect asset cleanup", error))?;
    let sql = if has_cleanup_table {
        "WITH candidates(id,mime,filename,pending) AS ( \
           SELECT a.id,a.mime,a.filename,ac.asset_id IS NOT NULL FROM assets a \
             LEFT JOIN asset_cleanup ac ON ac.asset_id=a.id \
           UNION ALL \
           SELECT ac.asset_id,NULL,ac.filename,1 FROM asset_cleanup ac \
             WHERE NOT EXISTS(SELECT 1 FROM assets a WHERE a.id=ac.asset_id) \
         ) SELECT id,mime,filename,pending FROM candidates WHERE id>? ORDER BY id LIMIT ?"
    } else {
        "SELECT a.id,a.mime,a.filename,0 FROM assets a WHERE a.id>? ORDER BY a.id LIMIT ?"
    };
    let mut statement = connection
        .prepare(sql)
        .map_err(|error| storage_error("Could not inspect managed assets", error))?;
    let mut rows = statement
        .query(params![after, (limit + 1) as i64])
        .map_err(|error| storage_error("Could not inspect managed assets", error))?;
    let mut items = Vec::new();
    let mut last_scanned = String::new();
    let mut scanned = 0usize;
    let mut has_more = false;
    while let Some(row) = rows
        .next()
        .map_err(|error| storage_error("Could not inspect managed assets", error))?
    {
        let id: String = row
            .get(0)
            .map_err(|error| storage_error("Could not inspect managed assets", error))?;
        if scanned == limit {
            has_more = true;
            break;
        }
        scanned += 1;
        last_scanned = id.clone();
        if asset_is_protected(connection, &id)? {
            continue;
        }
        let mime: Option<String> = row
            .get(1)
            .map_err(|error| storage_error("Could not inspect managed assets", error))?;
        let filename: String = row
            .get(2)
            .map_err(|error| storage_error("Could not inspect managed assets", error))?;
        let pending: bool = row
            .get(3)
            .map_err(|error| storage_error("Could not inspect managed assets", error))?;
        let (status, reason) = if !pending {
            (
                "uncertain",
                "No durable cleanup ownership proves this asset is abandoned",
            )
        } else {
            match checked_cleanup_path(assets_dir, &id, mime.as_deref(), &filename) {
                Ok(_) => ("eligible", "Durable cleanup ownership is unreferenced"),
                Err(_) => ("blocked", "Managed asset path or file is unsafe to remove"),
            }
        };
        items.push(json!({
            "id": id,
            "filename": filename,
            "status": status,
            "reason": reason,
        }));
    }
    Ok(json!({
        "mode": "inspect",
        "items": items,
        "hasMore": has_more,
        "nextAfter": if has_more { Value::String(last_scanned) } else { Value::Null },
        "readOnly": true,
    }))
}

fn encoded_ids_contain(encoded: &str, expected: &str) -> bool {
    serde_json::from_str::<Vec<String>>(encoded)
        .unwrap_or_default()
        .iter()
        .any(|value| value == expected)
}

fn read_managed(
    connection: &Connection,
    assets_dir: &Path,
    request: &Value,
) -> Result<Value, String> {
    let id = request
        .get("id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "id is required".to_owned())?;
    let metadata = connection
        .query_row("SELECT mime,filename FROM assets WHERE id=?", [id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .optional()
        .map_err(|error| storage_error("Could not read asset metadata", error))?
        .ok_or_else(|| "Asset not found".to_owned())?;
    let (mime, filename) = metadata;
    let extension =
        extension_for_mime(&mime).ok_or_else(|| "Stored asset metadata is invalid".to_owned())?;
    if filename != format!("{id}.{extension}") || !single_filename(&filename) {
        return Err("Stored asset metadata is invalid".to_owned());
    }

    let directory = confined_assets_directory(assets_dir)?;
    let path = directory.join(&filename);
    let link_metadata = fs::symlink_metadata(&path).map_err(|_| "Asset not found".to_owned())?;
    if link_metadata.file_type().is_symlink()
        || !link_metadata.is_file()
        || link_metadata.len() > MAX_ASSET_BYTES
    {
        return Err("Stored asset file is invalid".to_owned());
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    options.custom_flags(O_NOFOLLOW);
    let file = options
        .open(&path)
        .map_err(|_| "Could not read the asset".to_owned())?;
    #[cfg(unix)]
    {
        let opened = file
            .metadata()
            .map_err(|_| "Stored asset file is invalid".to_owned())?;
        if opened.dev() != link_metadata.dev()
            || opened.ino() != link_metadata.ino()
            || opened.nlink() != 1
        {
            return Err("Stored asset file is invalid".to_owned());
        }
    }
    let mut bytes = Vec::with_capacity(link_metadata.len() as usize);
    file.take(MAX_ASSET_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "Could not read the asset".to_owned())?;
    if bytes.len() as u64 > MAX_ASSET_BYTES {
        return Err("Stored asset file is invalid".to_owned());
    }
    Ok(json!({
        "data": base64::engine::general_purpose::STANDARD.encode(bytes),
        "mime": mime,
    }))
}

fn import_inspected(
    connection: &mut Connection,
    assets_dir: &Path,
    assets_display_dir: &Path,
    request: &Value,
    inspection: &Value,
    bytes: Vec<u8>,
) -> Result<Value, String> {
    let result = inspection
        .get("result")
        .and_then(Value::as_object)
        .ok_or_else(|| "Image inspection helper returned an invalid response".to_owned())?;
    let mime = result
        .get("mime")
        .and_then(Value::as_str)
        .ok_or_else(|| "Image inspection helper returned an invalid response".to_owned())?;
    let extension = result
        .get("extension")
        .and_then(Value::as_str)
        .ok_or_else(|| "Image inspection helper returned an invalid response".to_owned())?;
    if !matches!(
        (mime, extension),
        ("image/png", "png")
            | ("image/jpeg", "jpg")
            | ("image/gif", "gif")
            | ("image/webp", "webp")
            | ("image/bmp", "bmp")
    ) {
        return Err("Image inspection helper returned an invalid response".to_owned());
    }

    let directory = confined_assets_directory(assets_dir)?;

    let supplied_name = request
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let capture_id = match request.get("captureId") {
        None => None,
        Some(Value::String(value)) if !value.is_empty() => Some(value.as_str()),
        Some(_) => return Err("captureId must be a non-empty string".to_owned()),
    };
    let id = new_id(connection)?;
    let filename = format!("{id}.{extension}");
    let path = directory.join(&filename);
    if path.parent() != Some(directory.as_path()) {
        return Err("Could not store the asset".to_owned());
    }

    let intent = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| storage_error("Could not retain asset import recovery", error))?;
    if let Some(capture_id) = capture_id {
        pending_capture_asset_ids(&intent, capture_id)?;
    }
    let requested_at = now(&intent)?;
    intent
        .execute(
            "INSERT INTO asset_cleanup(asset_id,filename,requested_at) VALUES(?,?,?)",
            params![id, filename, requested_at],
        )
        .map_err(|error| storage_error("Could not retain asset import recovery", error))?;
    intent
        .commit()
        .map_err(|error| storage_error("Could not retain asset import recovery", error))?;

    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| storage_error("Could not store asset metadata", error))?;
    let pending = transaction
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM asset_cleanup WHERE asset_id=? AND filename=?)",
            params![id, filename],
            |row| row.get::<_, bool>(0),
        )
        .map_err(|error| storage_error("Could not verify asset import recovery", error))?;
    if !pending {
        return Err("Asset import recovery was cancelled".to_owned());
    }
    let mut capture_asset_ids = match capture_id {
        Some(id) => Some(pending_capture_asset_ids(&transaction, id)?),
        None => None,
    };
    remove_interrupted_imports(&directory)?;
    let temporary = directory.join(format!(".{filename}.importing"));
    let mut cleanup = Cleanup::new(temporary.clone());
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&temporary)
        .map_err(|_| "Could not store the asset".to_owned())?;
    file.write_all(&bytes)
        .and_then(|_| file.sync_all())
        .map_err(|_| "Could not store the asset".to_owned())?;
    drop(file);
    fs::hard_link(&temporary, &path).map_err(|_| "Could not store the asset".to_owned())?;
    cleanup.published = Some(path.clone());
    fs::remove_file(&temporary).map_err(|_| "Could not store the asset".to_owned())?;
    cleanup.temporary = None;
    sync_directory(&directory)?;

    let name = Path::new(supplied_name)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .map(|value| value.chars().take(255).collect::<String>())
        .unwrap_or_else(|| filename.clone());
    let created_at = now(&transaction)?;
    transaction
        .execute(
            "INSERT INTO assets(id,mime,name,filename,created_at) VALUES(?,?,?,?,?)",
            params![id, mime, name, filename, created_at],
        )
        .map_err(|error| storage_error("Could not store asset metadata", error))?;
    if let (Some(capture_id), Some(asset_ids)) = (capture_id, capture_asset_ids.as_mut()) {
        asset_ids.push(id.clone());
        let encoded = serde_json::to_string(asset_ids)
            .map_err(|error| format!("Could not encode capture progress: {error}"))?;
        let changed = transaction
            .execute(
                "UPDATE captures SET asset_ids=? WHERE id=? AND status='pending'",
                params![encoded, capture_id],
            )
            .map_err(|error| storage_error("Could not retain captured asset ownership", error))?;
        if changed != 1 {
            return Err("Capture not found".to_owned());
        }
    }
    let completed = transaction
        .execute(
            "DELETE FROM asset_cleanup WHERE asset_id=? AND filename=?",
            params![id, filename],
        )
        .map_err(|error| storage_error("Could not complete asset import recovery", error))?;
    if completed != 1 {
        return Err("Could not complete asset import recovery".to_owned());
    }
    transaction
        .commit()
        .map_err(|error| storage_error("Could not store asset metadata", error))?;
    cleanup.published = None;

    Ok(json!({
        "id": id,
        "url": format!("file://{}", percent_encode_path(&assets_display_dir.join(&filename).to_string_lossy())),
        "mime": mime,
        "name": name,
    }))
}

fn pending_capture_asset_ids(
    transaction: &Transaction<'_>,
    capture_id: &str,
) -> Result<Vec<String>, String> {
    let encoded = transaction
        .query_row(
            "SELECT asset_ids FROM captures WHERE id=? AND status='pending'",
            params![capture_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| storage_error("Could not inspect capture progress", error))?
        .ok_or_else(|| "Capture not found".to_owned())?;
    serde_json::from_str::<Vec<String>>(&encoded)
        .map_err(|error| format!("Could not read captured asset ownership: {error}"))
}

fn decode_asset(data: &str) -> Result<Vec<u8>, String> {
    let decoder = GeneralPurpose::new(
        &alphabet::STANDARD,
        GeneralPurposeConfig::new()
            .with_decode_allow_trailing_bits(true)
            .with_decode_padding_mode(DecodePaddingMode::Indifferent),
    );
    let bytes = decoder
        .decode(data.as_bytes())
        .map_err(|_| "Asset data is not valid base64 image data".to_owned())?;
    if bytes.is_empty() {
        return Err("Asset data is not valid base64 image data".to_owned());
    }
    Ok(bytes)
}

fn failure(error: String) -> Value {
    json!({"ok": false, "error": error})
}

fn confined_assets_directory(assets_dir: &Path) -> Result<PathBuf, String> {
    if !fs::metadata(assets_dir)
        .map_err(|_| "Could not access the asset directory".to_owned())?
        .is_dir()
    {
        return Err("Could not access the asset directory".to_owned());
    }
    Ok(assets_dir.to_path_buf())
}

fn lock_asset_imports(assets_dir: &Path) -> Result<File, String> {
    let directory = File::open(assets_dir)
        .map_err(|_| "Could not coordinate concurrent asset imports".to_owned())?;
    directory
        .lock()
        .map_err(|_| "Could not coordinate concurrent asset imports".to_owned())?;
    Ok(directory)
}

fn single_filename(filename: &str) -> bool {
    let mut components = Path::new(filename).components();
    matches!(components.next(), Some(Component::Normal(_))) && components.next().is_none()
}

fn extension_for_mime(mime: &str) -> Option<&'static str> {
    match mime {
        "image/png" => Some("png"),
        "image/jpeg" => Some("jpg"),
        "image/gif" => Some("gif"),
        "image/webp" => Some("webp"),
        "image/bmp" => Some("bmp"),
        _ => None,
    }
}

fn remove_interrupted_imports(directory: &Path) -> Result<(), String> {
    let entries = fs::read_dir(directory).map_err(|_| "Could not store the asset".to_owned())?;
    for entry in entries {
        let entry = entry.map_err(|_| "Could not store the asset".to_owned())?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let interrupted = name
            .strip_prefix('.')
            .and_then(|value| value.strip_suffix(".importing"));
        let Some(candidate) = interrupted else {
            continue;
        };
        if !is_managed_filename(candidate) {
            continue;
        }
        fs::remove_file(entry.path())
            .map_err(|_| "Could not recover interrupted asset imports".to_owned())?;
    }
    Ok(())
}

fn is_managed_filename(filename: &str) -> bool {
    let Some((id, extension)) = filename.rsplit_once('.') else {
        return false;
    };
    if !matches!(extension, "png" | "jpg" | "gif" | "webp" | "bmp") {
        return false;
    }
    id.len() == 36
        && id.bytes().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()
            }
        })
}

fn sync_directory(directory: &Path) -> Result<(), String> {
    File::open(directory)
        .and_then(|file| file.sync_all())
        .map_err(|_| "Could not store the asset".to_owned())
}

struct Cleanup {
    temporary: Option<PathBuf>,
    published: Option<PathBuf>,
}

impl Cleanup {
    fn new(temporary: PathBuf) -> Self {
        Self {
            temporary: Some(temporary),
            published: None,
        }
    }
}

impl Drop for Cleanup {
    fn drop(&mut self) {
        if let Some(path) = self.temporary.take() {
            let _ = fs::remove_file(path);
        }
        if let Some(path) = self.published.take() {
            let _ = fs::remove_file(path);
        }
    }
}
