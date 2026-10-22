use crate::store::{now, storage_error};
use rusqlite::{params, Connection, TransactionBehavior};
use serde_json::{json, Value};
use std::path::Path;

pub fn finish(
    connection: &mut Connection,
    assets_dir: &Path,
    assets_display_dir: &Path,
    helper_bin: &Path,
    request: &Value,
) -> Value {
    match finish_inner(
        connection,
        assets_dir,
        assets_display_dir,
        helper_bin,
        request,
    ) {
        Ok((result, warnings)) => crate::assets::success_envelope(result, warnings),
        Err(FinishError::Message(error)) => json!({"ok": false, "error": error}),
        Err(FinishError::Envelope(envelope)) => envelope,
    }
}

fn finish_inner(
    connection: &mut Connection,
    assets_dir: &Path,
    assets_display_dir: &Path,
    helper_bin: &Path,
    request: &Value,
) -> Result<(Value, Vec<String>), FinishError> {
    let asset_ids = string_list(request, "assetIds")?;
    require_assets(connection, &asset_ids)?;
    let missing = request
        .get("missing")
        .and_then(Value::as_array)
        .ok_or_else(|| FinishError::message("missing must be an array"))?;
    let html = request.get("html").and_then(Value::as_str).unwrap_or("");
    let helper_response = crate::helper::request(
        helper_bin,
        &json!({
            "op": "html.sanitize_capture",
            "html": html,
            "assetIds": asset_ids,
        }),
        "HTML sanitizer helper",
    )
    .map_err(FinishError::Message)?;
    if helper_response.get("ok").and_then(Value::as_bool) != Some(true) {
        return Err(FinishError::Envelope(helper_response));
    }
    let sanitized = helper_response
        .pointer("/result/html")
        .and_then(Value::as_str)
        .ok_or_else(|| FinishError::message("HTML sanitizer helper returned an invalid response"))?
        .to_owned();
    let capture_id = request
        .get("captureId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let text = request
        .get("text")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if text.trim().is_empty() && sanitized.trim().is_empty() && asset_ids.is_empty() {
        return Err(FinishError::message("Captured content cannot be empty"));
    }
    let encoded_missing = serde_json::to_string(missing).map_err(|error| {
        FinishError::message(format!("Could not encode capture progress: {error}"))
    })?;
    let encoded_assets = serde_json::to_string(&asset_ids).map_err(|error| {
        FinishError::message(format!("Could not encode capture progress: {error}"))
    })?;
    let incomplete = !missing.is_empty();
    let status = if incomplete { "incomplete" } else { "ready" };

    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| {
            FinishError::message(storage_error(
                "Could not start a storage transaction",
                error,
            ))
        })?;
    require_assets(&transaction, &asset_ids)?;
    let capture = capture_record(&transaction, &capture_id)?;
    let is_update = !capture.update_id.is_empty();
    let replaced = asset_ids_for_clip(&transaction, &capture.clip_id)?;

    if !(is_update && incomplete) {
        let created_at = now(&transaction).map_err(FinishError::Message)?;
        let changed = transaction
            .execute(
                "UPDATE clips SET title=?,text=?,html=?,source_url=?,scope=?,status=?,missing=?,created_at=?,revision=revision+1 WHERE id=?",
                params![
                    capture.title,
                    text,
                    sanitized,
                    capture.source_url,
                    capture.scope,
                    status,
                    encoded_missing,
                    created_at,
                    capture.clip_id,
                ],
            )
            .map_err(|error| FinishError::message(storage_error("Could not finish the capture", error)))?;
        if changed != 1 {
            return Err(FinishError::message("Capture target no longer exists"));
        }
        transaction
            .execute(
                "DELETE FROM clip_assets WHERE clip_id=?",
                params![capture.clip_id],
            )
            .map_err(|error| {
                FinishError::message(storage_error("Could not replace captured assets", error))
            })?;
        link_assets(&transaction, &capture.clip_id, &asset_ids)?;
    }

    if incomplete {
        transaction
            .execute(
                "UPDATE captures SET html=?,text=?,missing=?,asset_ids=?,status='incomplete',reason='' WHERE id=?",
                params![sanitized, text, encoded_missing, encoded_assets, capture_id],
            )
            .map_err(|error| FinishError::message(storage_error("Could not retain incomplete capture progress", error)))?;
    } else {
        transaction
            .execute("DELETE FROM captures WHERE id=?", params![capture_id])
            .map_err(|error| {
                FinishError::message(storage_error("Could not complete capture progress", error))
            })?;
    }
    let mut cleanup_candidates = capture.asset_ids;
    if !(is_update && incomplete) {
        cleanup_candidates.extend(replaced);
    }
    let cleanup = crate::assets::plan_orphan_cleanup(&transaction, &cleanup_candidates)
        .map_err(FinishError::message)?;
    let mut result =
        crate::store::clip_result(&transaction, assets_display_dir, &capture.clip_id, true)
            .map_err(FinishError::message)?;
    if is_update && incomplete {
        let object = result
            .as_object_mut()
            .ok_or_else(|| FinishError::message("Could not read the updated capture"))?;
        object.insert(
            "updateStatus".to_owned(),
            Value::String("incomplete".to_owned()),
        );
        object.insert("captureId".to_owned(), Value::String(capture_id));
    }
    transaction.commit().map_err(|error| {
        FinishError::message(storage_error("Could not commit storage changes", error))
    })?;
    let cleanup = crate::assets::remove_orphan_files(connection, assets_dir, cleanup);
    Ok((result, cleanup.warnings))
}

struct CaptureRecord {
    clip_id: String,
    update_id: String,
    title: String,
    source_url: String,
    scope: String,
    asset_ids: Vec<String>,
}

fn capture_record(connection: &Connection, id: &str) -> Result<CaptureRecord, FinishError> {
    connection
        .query_row(
            "SELECT clip_id,update_id,title,source_url,scope,asset_ids FROM captures WHERE id=?",
            params![id],
            |row| {
                Ok(CaptureRecord {
                    clip_id: row.get(0)?,
                    update_id: row.get(1)?,
                    title: row.get(2)?,
                    source_url: row.get(3)?,
                    scope: row.get(4)?,
                    asset_ids: serde_json::from_str::<Vec<String>>(&row.get::<_, String>(5)?)
                        .unwrap_or_default(),
                })
            },
        )
        .map_err(|error| match error {
            rusqlite::Error::QueryReturnedNoRows => FinishError::message("Capture not found"),
            other => FinishError::message(storage_error("Could not read capture progress", other)),
        })
}

fn string_list(request: &Value, name: &str) -> Result<Vec<String>, FinishError> {
    let values = request
        .get(name)
        .and_then(Value::as_array)
        .ok_or_else(|| FinishError::message(format!("{name} must be an array")))?;
    let mut result = Vec::new();
    for value in values {
        let value = value
            .as_str()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| FinishError::message(format!("{name} contains an invalid ID")))?;
        if !result.iter().any(|existing| existing == value) {
            result.push(value.to_owned());
        }
    }
    Ok(result)
}

fn require_assets(connection: &Connection, ids: &[String]) -> Result<(), FinishError> {
    let mut statement = connection
        .prepare(concat!(
            "SELECT 1 FROM assets WHERE id=? ",
            "AND NOT EXISTS(SELECT 1 FROM asset_cleanup WHERE asset_id=assets.id)"
        ))
        .map_err(|error| {
            FinishError::message(storage_error("Could not validate an asset", error))
        })?;
    for id in ids {
        match statement.query_row(params![id], |_| Ok(())) {
            Ok(()) => {}
            Err(rusqlite::Error::QueryReturnedNoRows) => {
                return Err(FinishError::message(format!("Unknown asset: {id}")));
            }
            Err(error) => {
                return Err(FinishError::message(storage_error(
                    "Could not validate an asset",
                    error,
                )));
            }
        }
    }
    Ok(())
}

fn link_assets(connection: &Connection, clip_id: &str, ids: &[String]) -> Result<(), FinishError> {
    let mut statement = connection
        .prepare("INSERT INTO clip_assets(clip_id,asset_id,position) VALUES(?,?,?)")
        .map_err(|error| FinishError::message(storage_error("Could not attach an asset", error)))?;
    for (position, id) in ids.iter().enumerate() {
        statement
            .execute(params![clip_id, id, position as i64])
            .map_err(|error| {
                FinishError::message(storage_error("Could not attach an asset", error))
            })?;
    }
    Ok(())
}

fn asset_ids_for_clip(connection: &Connection, clip_id: &str) -> Result<Vec<String>, FinishError> {
    let mut statement = connection
        .prepare("SELECT asset_id FROM clip_assets WHERE clip_id=? ORDER BY position")
        .map_err(|error| {
            FinishError::message(storage_error("Could not inspect captured assets", error))
        })?;
    let rows = statement
        .query_map(params![clip_id], |row| row.get::<_, String>(0))
        .map_err(|error| {
            FinishError::message(storage_error("Could not inspect captured assets", error))
        })?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|error| {
        FinishError::message(storage_error("Could not inspect captured assets", error))
    })
}

enum FinishError {
    Message(String),
    Envelope(Value),
}

impl FinishError {
    fn message(value: impl Into<String>) -> Self {
        Self::Message(value.into())
    }
}

impl From<String> for FinishError {
    fn from(value: String) -> Self {
        Self::Message(value)
    }
}
