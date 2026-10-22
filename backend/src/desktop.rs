use crate::helper;
use crate::store::Store;
use serde_json::{json, Value};
use std::path::Path;
use std::time::Duration;

const THEME_ENVIRONMENT: &[&str] = &["HOME", "XDG_STATE_HOME"];
const FILE_ENVIRONMENT: &[&str] = &[];
const LINK_ENVIRONMENT: &[&str] = &[
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_CURRENT_DESKTOP",
];
const CLIPBOARD_ENVIRONMENT: &[&str] = &["PATH", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY"];
const CAPTURE_ENVIRONMENT: &[&str] = &[
    "PATH",
    "HOME",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_STATE_HOME",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "HYPRLAND_INSTANCE_SIGNATURE",
    "SWAYSOCK",
];
const CAPTURE_TIMEOUT: Duration = Duration::from_secs(181);

pub fn dispatch(
    request: &Value,
    store: &mut Store,
    helper_bin: &Path,
    native: bool,
) -> Option<Value> {
    let op = request.get("op").and_then(Value::as_str).unwrap_or("");
    let result = match op {
        "theme.get" => helper_request(
            helper_bin,
            &json!({"op": "desktop.theme_get"}),
            THEME_ENVIRONMENT,
        ),
        "link.open" => helper_request(
            helper_bin,
            &json!({
                "op": "desktop.link_open",
                "url": request.get("url").cloned().unwrap_or(Value::Null),
            }),
            LINK_ENVIRONMENT,
        ),
        "file.import" => import_image(
            store,
            helper_bin,
            &json!({
                "op": "desktop.file_image_read",
                "path": request.get("path").cloned().unwrap_or(Value::Null),
            }),
            FILE_ENVIRONMENT,
        ),
        "clipboard.import" => import_image(
            store,
            helper_bin,
            &json!({"op": "desktop.clipboard_image_read"}),
            CLIPBOARD_ENVIRONMENT,
        ),
        "capture.region" => capture_region(store, helper_bin, request, native),
        "clipboard.copy" => copy_to_clipboard(store, helper_bin, request),
        _ => return None,
    };
    Some(result.unwrap_or_else(failure))
}

fn capture_region(
    store: &mut Store,
    helper_bin: &Path,
    request: &Value,
    native: bool,
) -> Result<Value, String> {
    let response = helper::request_with_options(
        helper_bin,
        &json!({
            "op": "desktop.capture_region",
            "sourceUrl": request.get("sourceUrl").cloned().unwrap_or(Value::String(String::new())),
            "requireSourceUrl": native,
        }),
        "Desktop helper",
        CAPTURE_ENVIRONMENT,
        CAPTURE_TIMEOUT,
    )?;
    if !succeeded(&response) {
        return Ok(response);
    }
    let imported = import_helper_image(store, helper_bin, &response)?;
    if !succeeded(&imported) || !native {
        return Ok(imported);
    }
    let asset_id = imported
        .pointer("/result/id")
        .and_then(Value::as_str)
        .ok_or_else(|| "Collection returned an invalid imported asset".to_owned())?
        .to_owned();
    let captured = store.dispatch(
        &json!({
            "op": "capture.mixed",
            "text": "",
            "assetIds": [asset_id],
            "sourceUrl": request.get("sourceUrl").cloned().unwrap_or(Value::Null),
            "title": request.get("title").cloned().unwrap_or(Value::Null),
            "scope": "region",
        }),
        Some(helper_bin),
    );
    if !succeeded(&captured) {
        store.discard_unreferenced_asset(&asset_id)?;
        return Ok(crate::assets::attach_cleanup_status(
            captured,
            store.take_cleanup_warnings(),
        ));
    }
    Ok(captured)
}

fn import_image(
    store: &mut Store,
    helper_bin: &Path,
    request: &Value,
    environment: &[&str],
) -> Result<Value, String> {
    let response = helper_request(helper_bin, request, environment)?;
    if !succeeded(&response) {
        return Ok(response);
    }
    import_helper_image(store, helper_bin, &response)
}

fn import_helper_image(
    store: &mut Store,
    helper_bin: &Path,
    response: &Value,
) -> Result<Value, String> {
    let result = response
        .get("result")
        .and_then(Value::as_object)
        .ok_or_else(|| "Desktop helper returned an invalid response".to_owned())?;
    let imported = json!({
        "op": "asset.import",
        "data": result.get("data").cloned().unwrap_or(Value::Null),
        "mime": result.get("mime").cloned().unwrap_or(Value::Null),
        "name": result.get("name").cloned().unwrap_or(Value::Null),
    });
    Ok(store.dispatch(&imported, Some(helper_bin)))
}

fn copy_to_clipboard(
    store: &mut Store,
    helper_bin: &Path,
    request: &Value,
) -> Result<Value, String> {
    let found = store.dispatch(
        &json!({
            "op": "clip.get",
            "id": request.get("id").cloned().unwrap_or(Value::Null),
        }),
        None,
    );
    if !succeeded(&found) {
        return Ok(found);
    }
    let clip = found
        .get("result")
        .and_then(Value::as_object)
        .ok_or_else(|| "Collection returned an invalid clip".to_owned())?;
    let asset_id = request.get("assetId").and_then(Value::as_str).unwrap_or("");
    if asset_id.is_empty() {
        return helper_request(
            helper_bin,
            &json!({
                "op": "desktop.clipboard_text_write",
                "text": clip.get("text").cloned().unwrap_or(Value::String(String::new())),
            }),
            CLIPBOARD_ENVIRONMENT,
        );
    }

    let owned = clip
        .get("assets")
        .and_then(Value::as_array)
        .is_some_and(|assets| {
            assets
                .iter()
                .any(|asset| asset.get("id").and_then(Value::as_str) == Some(asset_id))
        });
    if !owned {
        return Ok(failure("Image does not belong to this clip".to_owned()));
    }
    let asset = store.dispatch(&json!({"op": "asset.read", "id": asset_id}), None);
    if !succeeded(&asset) {
        return Ok(asset);
    }
    let payload = asset
        .get("result")
        .and_then(Value::as_object)
        .ok_or_else(|| "Collection returned invalid asset data".to_owned())?;
    helper_request(
        helper_bin,
        &json!({
            "op": "desktop.clipboard_image_write",
            "data": payload.get("data").cloned().unwrap_or(Value::Null),
            "mime": payload.get("mime").cloned().unwrap_or(Value::Null),
        }),
        CLIPBOARD_ENVIRONMENT,
    )
}

fn helper_request(
    helper_bin: &Path,
    request: &Value,
    environment: &[&str],
) -> Result<Value, String> {
    helper::request_with_environment(helper_bin, request, "Desktop helper", environment)
}

fn succeeded(response: &Value) -> bool {
    response.get("ok").and_then(Value::as_bool) == Some(true)
}

fn failure(error: String) -> Value {
    json!({"ok": false, "error": error})
}
