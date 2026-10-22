use crate::helper::CaptureSanitizer;
use crate::storage_security::SecureStorage;
use rusqlite::types::Value as SqlValue;
use rusqlite::{params, params_from_iter, Connection, ErrorCode, OpenFlags, Row};
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

const STORAGE_BUSY_TIMEOUT: Duration = Duration::from_millis(3000);

pub struct Store {
    connection: Connection,
    assets_dir: PathBuf,
    assets_display_dir: PathBuf,
    _storage: SecureStorage,
    cleanup_warnings: Vec<String>,
}

pub fn supports(op: &str) -> bool {
    matches!(
        op,
        "clip.list"
            | "clip.get"
            | "clip.edit"
            | "clip.trash"
            | "clip.restore"
            | "note.save"
            | "draft.get"
            | "draft.save"
            | "draft.commit"
            | "asset.import"
            | "asset.read"
            | "asset.reconcile"
            | "capture.check"
            | "capture.pending"
            | "capture.begin"
            | "capture.finish"
            | "capture.abort"
            | "capture.mixed"
            | "trash.empty"
    )
}

impl Store {
    pub fn requires_write(request: &Value) -> bool {
        if request.get("op").and_then(Value::as_str) == Some("asset.reconcile") {
            return request.get("confirm").and_then(Value::as_bool) == Some(true);
        }
        matches!(
            request.get("op").and_then(Value::as_str),
            Some(
                "draft.get"
                    | "draft.save"
                    | "draft.commit"
                    | "asset.import"
                    | "clip.edit"
                    | "clip.trash"
                    | "clip.restore"
                    | "note.save"
                    | "capture.begin"
                    | "capture.finish"
                    | "capture.abort"
                    | "capture.mixed"
                    | "trash.empty"
            )
        )
    }

    pub fn open_read(data_dir: &Path) -> Result<Self, String> {
        let Some(storage) = SecureStorage::open_existing(data_dir)? else {
            return Self::open_write(data_dir);
        };
        let connection = Connection::open_with_flags(
            storage.database_path(),
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|error| format!("Could not open the collection database: {error}"))?;
        connection
            .busy_timeout(STORAGE_BUSY_TIMEOUT)
            .map_err(|error| format!("Could not configure the collection database: {error}"))?;

        Ok(Self {
            connection,
            assets_dir: storage.assets_access_path(),
            assets_display_dir: storage.assets_display_path(),
            _storage: storage,
            cleanup_warnings: Vec::new(),
        })
    }

    pub fn open_inspect(data_dir: &Path) -> Result<Option<Self>, String> {
        let Some(storage) = SecureStorage::inspect(data_dir)? else {
            return Ok(None);
        };
        let database = storage.database_path();
        let uri = format!("file:{}?immutable=1", database.to_string_lossy());
        let connection = Connection::open_with_flags(
            uri,
            OpenFlags::SQLITE_OPEN_READ_ONLY
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_URI,
        )
        .map_err(|error| format!("Could not inspect the collection database: {error}"))?;
        Ok(Some(Self {
            connection,
            assets_dir: storage.assets_access_path(),
            assets_display_dir: storage.assets_display_path(),
            _storage: storage,
            cleanup_warnings: Vec::new(),
        }))
    }

    pub fn open_write(data_dir: &Path) -> Result<Self, String> {
        let storage = SecureStorage::open_or_create(data_dir)?;
        let connection = Connection::open_with_flags(
            storage.database_path(),
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_CREATE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|error| format!("Could not open the collection database: {error}"))?;
        connection
            .busy_timeout(STORAGE_BUSY_TIMEOUT)
            .map_err(|error| format!("Could not configure the collection database: {error}"))?;
        connection
            .execute_batch("PRAGMA foreign_keys = ON")
            .map_err(|error| {
                storage_error("Could not enable collection integrity checks", error)
            })?;
        retry_busy("Could not enable concurrent collection storage", || {
            connection.query_row("PRAGMA journal_mode = WAL", [], |_| Ok(()))
        })?;
        for statement in [
            "CREATE TABLE IF NOT EXISTS draft (id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL, asset_ids TEXT NOT NULL, revision INTEGER NOT NULL)",
            "INSERT OR IGNORE INTO draft(id,text,asset_ids,revision) VALUES(1,'','[]',0)",
            "CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, mime TEXT NOT NULL, name TEXT NOT NULL, filename TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('mixed','article')), title TEXT NOT NULL, text TEXT NOT NULL, html TEXT NOT NULL, note TEXT NOT NULL, source_url TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('capturing','incomplete','ready')), missing TEXT NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT NOT NULL, revision INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS clip_assets (clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE, asset_id TEXT NOT NULL REFERENCES assets(id), position INTEGER NOT NULL, PRIMARY KEY(clip_id,asset_id))",
            "CREATE TABLE IF NOT EXISTS captures (id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, update_id TEXT NOT NULL, title TEXT NOT NULL, source_url TEXT NOT NULL, scope TEXT NOT NULL, html TEXT NOT NULL, text TEXT NOT NULL, missing TEXT NOT NULL, asset_ids TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS asset_cleanup (asset_id TEXT PRIMARY KEY, filename TEXT NOT NULL, requested_at TEXT NOT NULL)",
        ] {
            retry_busy("Could not initialize collection storage", || {
                connection.execute_batch(statement)
            })?;
        }
        Ok(Self {
            connection,
            assets_dir: storage.assets_access_path(),
            assets_display_dir: storage.assets_display_path(),
            _storage: storage,
            cleanup_warnings: Vec::new(),
        })
    }

    pub fn dispatch(&mut self, request: &Value, helper_bin: Option<&Path>) -> Value {
        self.cleanup_warnings.clear();
        if request.get("op").and_then(Value::as_str) == Some("asset.import") {
            let Some(helper_path) = helper_bin else {
                return json!({"ok": false, "error": "asset.import requires --helper-bin"});
            };
            return crate::assets::import(
                &mut self.connection,
                &self.assets_dir,
                &self.assets_display_dir,
                helper_path,
                request,
            );
        }
        if request.get("op").and_then(Value::as_str) == Some("asset.read") {
            return crate::assets::read(&self.connection, &self.assets_dir, request);
        }
        if request.get("op").and_then(Value::as_str) == Some("asset.reconcile") {
            return crate::assets::reconcile(&mut self.connection, &self.assets_dir, request);
        }
        if request.get("op").and_then(Value::as_str) == Some("capture.finish") {
            let Some(helper_path) = helper_bin else {
                return json!({"ok": false, "error": "capture.finish requires --helper-bin"});
            };
            return crate::captures::finish(
                &mut self.connection,
                &self.assets_dir,
                &self.assets_display_dir,
                helper_path,
                request,
            );
        }
        match self.dispatch_result(request, helper_bin) {
            Ok(result) => {
                crate::assets::success_envelope(result, std::mem::take(&mut self.cleanup_warnings))
            }
            Err(error) => json!({"ok": false, "error": error}),
        }
    }

    fn record_cleanup_warnings(&mut self, warnings: Vec<String>) {
        self.cleanup_warnings.extend(warnings);
    }

    pub(crate) fn take_cleanup_warnings(&mut self) -> Vec<String> {
        std::mem::take(&mut self.cleanup_warnings)
    }

    fn dispatch_result(
        &mut self,
        request: &Value,
        helper_bin: Option<&Path>,
    ) -> Result<Value, String> {
        let op = string_value(request, "op", "");
        if op.is_empty() {
            return Err("Missing operation".to_owned());
        }
        match op.as_str() {
            "clip.list" => self.list(request),
            "clip.get" => self.get(request),
            "clip.edit" => self.clip_edit(request),
            "clip.trash" => self.clip_lifecycle(request, true),
            "clip.restore" => self.clip_lifecycle(request, false),
            "note.save" => self.note_save(request),
            "draft.get" => self.draft(),
            "draft.save" => self.draft_save(request),
            "draft.commit" => self.draft_commit(request),
            "capture.check" => self.capture_check(request),
            "capture.pending" => self.capture_pending(),
            "capture.begin" => self.capture_begin(request, helper_bin),
            "capture.abort" => self.capture_abort(request),
            "capture.mixed" => self.capture_mixed(request),
            "trash.empty" => self.trash_empty(request),
            _ => Err(format!("Unknown operation: {op}")),
        }
    }

    fn draft(&self) -> Result<Value, String> {
        let (draft_text, encoded_ids, revision): (String, String, i64) = self
            .connection
            .query_row(
                "SELECT text,asset_ids,revision FROM draft WHERE id=1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .map_err(|error| match error {
                rusqlite::Error::QueryReturnedNoRows => "Draft storage is unavailable".to_owned(),
                other => storage_error("Could not read the draft", other),
            })?;
        let asset_ids = serde_json::from_str::<Value>(&encoded_ids)
            .ok()
            .and_then(|value| value.as_array().cloned())
            .unwrap_or_default();
        let mut assets = Vec::new();
        for value in &asset_ids {
            let Some(id) = value.as_str() else {
                continue;
            };
            let result = self.connection.query_row(
                "SELECT mime,name,filename FROM assets WHERE id=?",
                params![id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            );
            match result {
                Ok((mime, name, filename)) => {
                    let path = self.assets_display_dir.join(filename);
                    assets.push(json!({
                        "id": id,
                        "url": format!("file://{}", percent_encode_path(&path.to_string_lossy())),
                        "mime": mime,
                        "name": name,
                    }));
                }
                Err(rusqlite::Error::QueryReturnedNoRows) => {}
                Err(error) => return Err(storage_error("Could not read draft assets", error)),
            }
        }
        Ok(json!({
            "text": draft_text,
            "assetIds": asset_ids,
            "assets": assets,
            "revision": revision,
        }))
    }

    fn draft_save(&mut self, request: &Value) -> Result<Value, String> {
        let text = request
            .get("text")
            .and_then(Value::as_str)
            .ok_or_else(|| "text must be a string".to_owned())?;
        let expected_revision = required_revision(request)?;
        let asset_ids = string_list(request, "assetIds")?;
        self.require_assets(&asset_ids)?;
        let old = self.draft()?;
        let old_ids = string_list(&old, "assetIds")?;
        let removed = old_ids
            .into_iter()
            .filter(|id| !asset_ids.contains(id))
            .collect::<Vec<_>>();
        let encoded_ids = serde_json::to_string(&asset_ids)
            .map_err(|error| format!("Could not encode Draft assets: {error}"))?;
        self.begin_transaction()?;
        let operation = (|| {
            let changed = self
                .connection
                .execute(
                    "UPDATE draft SET text=?,asset_ids=?,revision=revision+1 WHERE id=1 AND revision=?",
                    params![text, encoded_ids, expected_revision],
                )
                .map_err(|error| storage_error("Could not save the draft", error))?;
            if changed != 1 {
                return Err("Draft changed; reload it before saving".to_owned());
            }
            self.require_assets(&asset_ids)?;
            let files = crate::assets::plan_orphan_cleanup(&self.connection, &removed)?;
            let result = self.draft()?;
            Ok((files, result))
        })();
        let (files, result) = match operation {
            Ok(value) => value,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        let cleanup =
            crate::assets::remove_orphan_files(&mut self.connection, &self.assets_dir, files);
        self.record_cleanup_warnings(cleanup.warnings);
        Ok(result)
    }

    fn draft_commit(&self, request: &Value) -> Result<Value, String> {
        let expected_revision = required_revision(request)?;
        let draft = self.draft()?;
        if draft.get("revision").and_then(Value::as_i64) != Some(expected_revision) {
            return Err("Draft changed; reload it before saving".to_owned());
        }
        let asset_ids = string_list(&draft, "assetIds")?;
        self.require_assets(&asset_ids)?;
        let text = draft.get("text").and_then(Value::as_str).unwrap_or("");
        self.begin_transaction()?;
        let operation = (|| {
            let id = self.insert_mixed(text, &asset_ids, "", "", "")?;
            let changed = self
                .connection
                .execute(
                    "UPDATE draft SET text='',asset_ids='[]',revision=revision+1 WHERE id=1 AND revision=?",
                    params![expected_revision],
                )
                .map_err(|error| storage_error("Could not clear the saved draft", error))?;
            if changed != 1 {
                return Err("Draft changed while it was being saved".to_owned());
            }
            self.clip_by_id(&id, true)
        })();
        let result = match operation {
            Ok(result) => result,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        Ok(result)
    }

    fn capture_mixed(&self, request: &Value) -> Result<Value, String> {
        let source_url = string_value(request, "sourceUrl", "");
        if !source_url.is_empty() {
            validate_source_url(&source_url)?;
        }
        let scope = string_value(request, "scope", "").trim().to_owned();
        if !scope.is_empty() {
            if source_url.is_empty() {
                return Err("scope requires a sourceUrl".to_owned());
            }
            if !valid_capture_scope(&scope) {
                return Err("scope is invalid".to_owned());
            }
        }
        let asset_ids = string_list(request, "assetIds")?;
        self.require_assets(&asset_ids)?;
        let text = string_value(request, "text", "");
        let title = string_value(request, "title", "");
        self.begin_transaction()?;
        let result = match self
            .insert_mixed(&text, &asset_ids, &title, &source_url, &scope)
            .and_then(|id| self.clip_by_id(&id, true))
        {
            Ok(result) => result,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        Ok(result)
    }

    fn insert_mixed(
        &self,
        text: &str,
        asset_ids: &[String],
        title: &str,
        source_url: &str,
        scope: &str,
    ) -> Result<String, String> {
        self.require_assets(asset_ids)?;
        if text.trim().is_empty() && asset_ids.is_empty() {
            return Err("A clip cannot be empty".to_owned());
        }
        let id = new_id(&self.connection)?;
        let title = if title.is_empty() {
            title_from_text(text, !asset_ids.is_empty())
        } else {
            left_chars(title, 200)
        };
        let created_at = now(&self.connection)?;
        self.connection
            .execute(
                "INSERT INTO clips(id,kind,title,text,html,note,source_url,scope,status,missing,created_at,deleted_at,revision) \
                 VALUES(?,'mixed',?,?, '', '',?,?,'ready','[]',?,'',1)",
                params![id, title, text, source_url, scope, created_at],
            )
            .map_err(|error| storage_error("Could not save the mixed clip", error))?;
        self.link_assets(&id, asset_ids)?;
        Ok(id)
    }

    fn require_assets(&self, ids: &[String]) -> Result<(), String> {
        let mut statement = self
            .connection
            .prepare(concat!(
                "SELECT 1 FROM assets WHERE id=? ",
                "AND NOT EXISTS(SELECT 1 FROM asset_cleanup WHERE asset_id=assets.id)"
            ))
            .map_err(|error| storage_error("Could not validate an asset", error))?;
        for id in ids {
            match statement.query_row(params![id], |_| Ok(())) {
                Ok(()) => {}
                Err(rusqlite::Error::QueryReturnedNoRows) => {
                    return Err(format!("Unknown asset: {id}"));
                }
                Err(error) => return Err(storage_error("Could not validate an asset", error)),
            }
        }
        Ok(())
    }

    fn link_assets(&self, clip_id: &str, ids: &[String]) -> Result<(), String> {
        let mut statement = self
            .connection
            .prepare("INSERT INTO clip_assets(clip_id,asset_id,position) VALUES(?,?,?)")
            .map_err(|error| storage_error("Could not attach an asset", error))?;
        for (position, id) in ids.iter().enumerate() {
            statement
                .execute(params![clip_id, id, position as i64])
                .map_err(|error| storage_error("Could not attach an asset", error))?;
        }
        Ok(())
    }

    pub(crate) fn discard_unreferenced_asset(&mut self, id: &str) -> Result<(), String> {
        self.begin_transaction()?;
        let files = match crate::assets::plan_orphan_cleanup(&self.connection, &[id.to_owned()]) {
            Ok(files) => files,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        let cleanup =
            crate::assets::remove_orphan_files(&mut self.connection, &self.assets_dir, files);
        self.record_cleanup_warnings(cleanup.warnings);
        Ok(())
    }

    fn clip_edit(&mut self, request: &Value) -> Result<Value, String> {
        let id = string_value(request, "id", "");
        let old = self.clip_by_id(&id, true)?;
        if old.get("kind").and_then(Value::as_str) != Some("mixed") {
            return Err("Captured article bodies cannot be edited".to_owned());
        }
        let text = request
            .get("text")
            .and_then(Value::as_str)
            .ok_or_else(|| "text must be a string".to_owned())?;
        let asset_ids = string_list(request, "assetIds")?;
        self.require_assets(&asset_ids)?;
        if text.trim().is_empty() && asset_ids.is_empty() {
            return Err("A clip cannot be empty".to_owned());
        }
        let replaced = old
            .get("assets")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|asset| asset.get("id").and_then(Value::as_str).map(str::to_owned))
            .collect::<Vec<_>>();
        let expected_revision = optional_revision(request, -1);
        self.begin_transaction()?;
        let operation = (|| {
            let changed = self
                .connection
                .execute(
                    "UPDATE clips SET text=?,title=?,revision=revision+1 WHERE id=? AND revision=?",
                    params![
                        text,
                        title_from_text(text, !asset_ids.is_empty()),
                        id,
                        expected_revision
                    ],
                )
                .map_err(|error| storage_error("Could not edit the clip", error))?;
            if changed != 1 {
                return Err("Clip changed; reload it before saving".to_owned());
            }
            self.connection
                .execute("DELETE FROM clip_assets WHERE clip_id=?", params![id])
                .map_err(|error| storage_error("Could not replace clip assets", error))?;
            self.require_assets(&asset_ids)?;
            self.link_assets(&id, &asset_ids)?;
            let files = crate::assets::plan_orphan_cleanup(&self.connection, &replaced)?;
            let result = self.clip_by_id(&id, true)?;
            Ok((files, result))
        })();
        let (files, result) = match operation {
            Ok(value) => value,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        let cleanup =
            crate::assets::remove_orphan_files(&mut self.connection, &self.assets_dir, files);
        self.record_cleanup_warnings(cleanup.warnings);
        Ok(result)
    }

    fn note_save(&self, request: &Value) -> Result<Value, String> {
        let id = string_value(request, "id", "");
        let old = self.clip_by_id(&id, true)?;
        if old.get("kind").and_then(Value::as_str) != Some("article") {
            return Err("Only article clips have separate notes".to_owned());
        }
        let note = request
            .get("note")
            .and_then(Value::as_str)
            .ok_or_else(|| "note must be a string".to_owned())?;
        let expected_revision = optional_revision(request, -1);
        self.begin_transaction()?;
        let operation = (|| {
            let changed = self
                .connection
                .execute(
                    "UPDATE clips SET note=?,revision=revision+1 WHERE id=? AND revision=?",
                    params![note, id, expected_revision],
                )
                .map_err(|error| storage_error("Could not save the article note", error))?;
            if changed != 1 {
                return Err("Clip changed; reload it before saving".to_owned());
            }
            self.clip_by_id(&id, true)
        })();
        let result = match operation {
            Ok(result) => result,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        Ok(result)
    }

    fn clip_lifecycle(&self, request: &Value, trash: bool) -> Result<Value, String> {
        let id = string_value(request, "id", "");
        self.clip_by_id(&id, true)?;
        self.begin_transaction()?;
        let operation = (|| {
            let changed = if trash {
                let deleted_at = now(&self.connection)?;
                self.connection.execute(
                    "UPDATE clips SET deleted_at=?,revision=revision+1 WHERE id=? AND deleted_at=''",
                    params![deleted_at, id],
                )
            } else {
                self.connection.execute(
                    "UPDATE clips SET deleted_at='',revision=revision+1 WHERE id=? AND deleted_at<>''",
                    params![id],
                )
            }
            .map_err(|error| storage_error("Could not change clip lifecycle", error))?;
            if changed != 1 {
                return Err(if trash {
                    "Clip is already in Trash".to_owned()
                } else {
                    "Clip is not in Trash".to_owned()
                });
            }
            self.clip_by_id(&id, true)
        })();
        let result = match operation {
            Ok(result) => result,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        Ok(result)
    }

    fn trash_empty(&mut self, request: &Value) -> Result<Value, String> {
        if request.get("confirm").and_then(Value::as_bool) != Some(true) {
            return Err("Emptying Trash requires confirm: true".to_owned());
        }
        self.begin_transaction()?;
        let operation = (|| {
            let mut candidates = Vec::new();
            {
                let mut statement = self
                    .connection
                    .prepare(
                        "SELECT DISTINCT ca.asset_id FROM clip_assets ca \
                         JOIN clips c ON c.id=ca.clip_id WHERE c.deleted_at<>''",
                    )
                    .map_err(|error| storage_error("Could not inspect Trash assets", error))?;
                let mut rows = statement
                    .query([])
                    .map_err(|error| storage_error("Could not inspect Trash assets", error))?;
                while let Some(row) = rows
                    .next()
                    .map_err(|error| storage_error("Could not inspect Trash assets", error))?
                {
                    candidates.push(
                        row.get::<_, String>(0).map_err(|error| {
                            storage_error("Could not inspect Trash assets", error)
                        })?,
                    );
                }
            }
            {
                let mut statement = self
                    .connection
                    .prepare(
                        "SELECT cp.asset_ids FROM captures cp \
                         JOIN clips c ON c.id=cp.clip_id WHERE c.deleted_at<>''",
                    )
                    .map_err(|error| {
                        storage_error("Could not inspect Trash capture assets", error)
                    })?;
                let mut rows = statement.query([]).map_err(|error| {
                    storage_error("Could not inspect Trash capture assets", error)
                })?;
                while let Some(row) = rows.next().map_err(|error| {
                    storage_error("Could not inspect Trash capture assets", error)
                })? {
                    let encoded: String = row.get(0).map_err(|error| {
                        storage_error("Could not inspect Trash capture assets", error)
                    })?;
                    candidates.extend(encoded_string_ids(&encoded));
                }
            }
            self.connection
                .execute(
                    "DELETE FROM captures WHERE clip_id IN (SELECT id FROM clips WHERE deleted_at<>'')",
                    [],
                )
                .map_err(|error| storage_error("Could not remove Trash capture progress", error))?;
            let removed = self
                .connection
                .execute("DELETE FROM clips WHERE deleted_at<>''", [])
                .map_err(|error| storage_error("Could not empty Trash", error))?;
            let files = crate::assets::plan_orphan_cleanup(&self.connection, &candidates)?;
            Ok((removed, files))
        })();
        let (removed, files) = match operation {
            Ok(value) => value,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        let cleanup =
            crate::assets::remove_orphan_files(&mut self.connection, &self.assets_dir, files);
        self.record_cleanup_warnings(cleanup.warnings);
        Ok(json!({"removed": removed}))
    }

    fn list(&self, request: &Value) -> Result<Value, String> {
        let filter = string_value(request, "filter", "all");
        if !matches!(filter.as_str(), "all" | "articles" | "notes" | "images") {
            return Err("Unknown clip filter".to_owned());
        }
        let trash = bool_value(request, "trash", false);
        let query_text = string_value(request, "query", "");
        let offset = int_value(request, "offset", 0).clamp(0, 1_000_000);
        let limit = int_value(request, "limit", 100).clamp(1, 500);

        let mut where_parts = vec![if trash {
            "deleted_at<>''"
        } else {
            "deleted_at=''"
        }];
        match filter.as_str() {
            "articles" => where_parts.push("kind='article'"),
            "notes" => where_parts.push("kind='mixed' AND trim(text)<>''"),
            "images" => where_parts.push(
                "kind='mixed' AND trim(text)='' AND EXISTS (SELECT 1 FROM clip_assets WHERE clip_id=clips.id)",
            ),
            _ => {}
        }

        let mut values = Vec::new();
        if !query_text.is_empty() {
            where_parts.push("(title LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' OR html LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\')");
            let escaped = query_text
                .replace('\\', "\\\\")
                .replace('%', "\\%")
                .replace('_', "\\_");
            let pattern = format!("%{escaped}%");
            values.extend((0..4).map(|_| SqlValue::Text(pattern.clone())));
        }

        let where_clause = where_parts.join(" AND ");
        let count_sql = format!("SELECT COUNT(*) FROM clips WHERE {where_clause}");
        let total: i64 = self
            .connection
            .query_row(&count_sql, params_from_iter(values.iter()), |row| {
                row.get(0)
            })
            .map_err(|error| format!("Could not count clips: {error}"))?;

        values.push(SqlValue::Integer(limit));
        values.push(SqlValue::Integer(offset));
        let list_sql = format!(
            "SELECT * FROM clips WHERE {where_clause} ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?"
        );
        let mut statement = self
            .connection
            .prepare(&list_sql)
            .map_err(|error| format!("Could not prepare the clip list: {error}"))?;
        let mut rows = statement
            .query(params_from_iter(values.iter()))
            .map_err(|error| format!("Could not list clips: {error}"))?;
        let mut items = Vec::new();
        while let Some(row) = rows
            .next()
            .map_err(|error| format!("Could not list clips: {error}"))?
        {
            items.push(self.clip_from_row(row, false)?);
        }
        Ok(json!({"items": items, "total": total}))
    }

    fn get(&self, request: &Value) -> Result<Value, String> {
        let id = string_value(request, "id", "");
        self.clip_by_id(&id, true)
    }

    fn capture_check(&self, request: &Value) -> Result<Value, String> {
        let source_url = string_value(request, "sourceUrl", "");
        let scope = string_value(request, "scope", "");
        validate_source_url(&source_url)?;
        if scope.trim().is_empty() {
            return Err("scope is required".to_owned());
        }

        let mut statement = self
            .connection
            .prepare(
                "SELECT * FROM clips WHERE kind='article' AND source_url=? AND scope=? \
                 ORDER BY (deleted_at='') DESC,created_at DESC LIMIT 1",
            )
            .map_err(|error| format!("Could not prepare the capture check: {error}"))?;
        let mut rows = statement
            .query(params![source_url, scope])
            .map_err(|error| format!("Could not check for an existing capture: {error}"))?;
        let existing = match rows
            .next()
            .map_err(|error| format!("Could not check for an existing capture: {error}"))?
        {
            Some(row) => self.clip_from_row(row, false)?,
            None => Value::Null,
        };
        Ok(json!({"existing": existing}))
    }

    fn capture_begin(
        &mut self,
        request: &Value,
        helper_bin: Option<&Path>,
    ) -> Result<Value, String> {
        let title = string_value(request, "title", "").trim().to_owned();
        let source_url = string_value(request, "sourceUrl", "");
        let scope = string_value(request, "scope", "").trim().to_owned();
        validate_source_url(&source_url)?;
        if title.is_empty() {
            return Err("title is required".to_owned());
        }
        if scope.is_empty() {
            return Err("scope is required".to_owned());
        }
        let missing = request
            .get("missing")
            .and_then(Value::as_array)
            .ok_or_else(|| "missing must be an array".to_owned())?;
        let helper_bin =
            helper_bin.ok_or_else(|| "capture.begin requires --helper-bin".to_owned())?;
        let html = string_value(request, "html", "");
        let sanitized = CaptureSanitizer::new(helper_bin).sanitize(&html)?;
        let text = string_value(request, "text", "");
        let update_id = string_value(request, "updateId", "");
        let capture_id = new_id(&self.connection)?;
        let mut clip_id = update_id.clone();

        let title = left_chars(&title, 200);
        let missing = serde_json::to_string(missing)
            .map_err(|error| format!("Could not encode capture progress: {error}"))?;
        let mut superseded_asset_ids = Vec::new();
        self.begin_transaction()?;
        let operation = (|| {
            let mut existing_capture = None;
            if update_id.is_empty() {
                let mut statement = self
                    .connection
                    .prepare(
                        "SELECT * FROM clips WHERE kind='article' AND source_url=? AND scope=? \
                         AND deleted_at='' ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    )
                    .map_err(|error| {
                        storage_error("Could not check for a concurrent capture", error)
                    })?;
                let mut rows = statement
                    .query(params![source_url, scope])
                    .map_err(|error| {
                        storage_error("Could not check for a concurrent capture", error)
                    })?;
                if let Some(row) = rows.next().map_err(|error| {
                    storage_error("Could not check for a concurrent capture", error)
                })? {
                    existing_capture = Some(self.clip_from_row(row, false)?);
                }
                drop(rows);
                drop(statement);
                if existing_capture.is_none() {
                    clip_id = new_id(&self.connection)?;
                    let created_at = now(&self.connection)?;
                    self.connection
                        .execute(
                            "INSERT INTO clips(id,kind,title,text,html,note,source_url,scope,status,missing,created_at,deleted_at,revision) \
                             VALUES(?,'article',?,?,?,?,?,?,'capturing',?,?, '',1)",
                            params![
                                clip_id,
                                title,
                                text,
                                sanitized,
                                "",
                                source_url,
                                scope,
                                missing,
                                created_at,
                            ],
                        )
                        .map_err(|error| storage_error("Could not begin the capture", error))?;
                }
            } else {
                let mut statement = self
                    .connection
                    .prepare("SELECT asset_ids FROM captures WHERE clip_id=?")
                    .map_err(|error| {
                        storage_error("Could not inspect an earlier capture attempt", error)
                    })?;
                let rows = statement
                    .query_map(params![clip_id], |row| row.get::<_, String>(0))
                    .map_err(|error| {
                        storage_error("Could not inspect an earlier capture attempt", error)
                    })?;
                for encoded in rows {
                    let encoded = encoded.map_err(|error| {
                        storage_error("Could not inspect an earlier capture attempt", error)
                    })?;
                    superseded_asset_ids.extend(encoded_string_ids(&encoded));
                }
                drop(statement);
                self.connection
                    .execute("DELETE FROM captures WHERE clip_id=?", params![clip_id])
                    .map_err(|error| {
                        storage_error("Could not supersede an earlier capture attempt", error)
                    })?;
                let active_article = self
                    .connection
                    .query_row(
                        "SELECT EXISTS(SELECT 1 FROM clips WHERE id=? AND kind='article' AND deleted_at='')",
                        params![clip_id],
                        |row| row.get::<_, bool>(0),
                    )
                    .map_err(|error| {
                        storage_error("Could not validate the update target", error)
                    })?;
                if !active_article {
                    return Err("Update target must be an active article clip".to_owned());
                }
            }
            if existing_capture.is_none() {
                let created_at = now(&self.connection)?;
                self.connection
                    .execute(
                        "INSERT INTO captures(id,clip_id,update_id,title,source_url,scope,html,text,missing,asset_ids,status,reason,created_at) \
                         VALUES(?,?,?,?,?,?,?,?,?,'[]','pending','',?)",
                        params![
                            capture_id,
                            clip_id,
                            update_id,
                            title,
                            source_url,
                            scope,
                            sanitized,
                            text,
                            missing,
                            created_at,
                        ],
                    )
                    .map_err(|error| {
                        storage_error("Could not persist capture progress", error)
                    })?;
            }
            let files =
                crate::assets::plan_orphan_cleanup(&self.connection, &superseded_asset_ids)?;
            Ok((existing_capture, files))
        })();
        let (existing_capture, files) = match operation {
            Ok(value) => value,
            Err(error) => {
                self.rollback_transaction();
                return Err(error);
            }
        };
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        let cleanup =
            crate::assets::remove_orphan_files(&mut self.connection, &self.assets_dir, files);
        self.record_cleanup_warnings(cleanup.warnings);
        match existing_capture {
            Some(existing) => Ok(json!({"existing": existing})),
            None => Ok(json!({"captureId": capture_id, "clipId": clip_id})),
        }
    }

    fn capture_abort(&self, request: &Value) -> Result<Value, String> {
        let capture_id = string_value(request, "captureId", "");
        let reason = string_value(request, "reason", "").trim().to_owned();
        if reason.is_empty() {
            return Err("reason is required".to_owned());
        }
        let reason = left_chars(&reason, 1000);
        self.begin_transaction()?;
        let operation = (|| {
            let (clip_id, update_id, stored_missing): (String, String, String) = self
                .connection
                .query_row(
                    "SELECT clip_id,update_id,missing FROM captures WHERE id=?",
                    params![capture_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .map_err(|error| match error {
                    rusqlite::Error::QueryReturnedNoRows => "Capture not found".to_owned(),
                    other => storage_error("Could not read capture progress", other),
                })?;
            let mut missing = serde_json::from_str::<Value>(&stored_missing)
                .ok()
                .and_then(|value| value.as_array().cloned())
                .unwrap_or_default();
            let changed = self
                .connection
                .execute(
                    "UPDATE captures SET status='aborted',reason=? WHERE id=?",
                    params![reason, capture_id],
                )
                .map_err(|error| {
                    storage_error("Could not retain failed capture progress", error)
                })?;
            if changed != 1 {
                return Err("Capture not found".to_owned());
            }
            if update_id.is_empty() {
                missing.push(json!({"url": "", "reason": reason}));
                let encoded = serde_json::to_string(&missing)
                    .map_err(|error| format!("Could not encode capture progress: {error}"))?;
                let changed = self
                    .connection
                    .execute(
                        "UPDATE clips SET status='incomplete',missing=?,revision=revision+1 WHERE id=?",
                        params![encoded, clip_id],
                    )
                    .map_err(|error| {
                        storage_error("Could not mark the incomplete capture", error)
                    })?;
                if changed != 1 {
                    return Err("Capture target no longer exists".to_owned());
                }
            }
            Ok(())
        })();
        if let Err(error) = operation {
            self.rollback_transaction();
            return Err(error);
        }
        if let Err(error) = self.commit_transaction() {
            self.rollback_transaction();
            return Err(error);
        }
        Ok(json!({
            "captureId": capture_id,
            "status": "incomplete",
            "reason": reason,
        }))
    }

    fn begin_transaction(&self) -> Result<(), String> {
        self.connection
            .execute_batch("BEGIN IMMEDIATE")
            .map_err(|error| storage_error("Could not start a storage transaction", error))
    }

    fn commit_transaction(&self) -> Result<(), String> {
        self.connection
            .execute_batch("COMMIT")
            .map_err(|error| storage_error("Could not commit storage changes", error))
    }

    fn rollback_transaction(&self) {
        let _ = self.connection.execute_batch("ROLLBACK");
    }

    fn capture_pending(&self) -> Result<Value, String> {
        let mut statement = self
            .connection
            .prepare("SELECT * FROM captures ORDER BY created_at")
            .map_err(|error| format!("Could not prepare the pending capture list: {error}"))?;
        let mut rows = statement
            .query([])
            .map_err(|error| format!("Could not list pending captures: {error}"))?;
        let mut items = Vec::new();
        while let Some(row) = rows
            .next()
            .map_err(|error| format!("Could not list pending captures: {error}"))?
        {
            let stored_status = text(row, "status")?;
            items.push(json!({
                "captureId": text(row, "id")?,
                "clipId": text(row, "clip_id")?,
                "updateId": text(row, "update_id")?,
                "title": text(row, "title")?,
                "sourceUrl": text(row, "source_url")?,
                "scope": text(row, "scope")?,
                "status": if stored_status == "pending" { "capturing" } else { "incomplete" },
                "missing": normalized_missing(&text(row, "missing")?),
                "reason": text(row, "reason")?,
                "createdAt": text(row, "created_at")?,
            }));
        }
        Ok(json!({"items": items}))
    }

    fn clip_by_id(&self, id: &str, include_html: bool) -> Result<Value, String> {
        clip_result(&self.connection, &self.assets_display_dir, id, include_html)
    }

    fn clip_from_row(&self, row: &Row<'_>, include_html: bool) -> Result<Value, String> {
        clip_result_from_row(
            &self.connection,
            &self.assets_display_dir,
            row,
            include_html,
        )
    }
}

pub(crate) fn clip_result(
    connection: &Connection,
    assets_dir: &Path,
    id: &str,
    include_html: bool,
) -> Result<Value, String> {
    let mut statement = connection
        .prepare("SELECT * FROM clips WHERE id=?")
        .map_err(|error| format!("Could not prepare the clip lookup: {error}"))?;
    let mut rows = statement
        .query(params![id])
        .map_err(|error| format!("Could not load the clip: {error}"))?;
    match rows
        .next()
        .map_err(|error| format!("Could not load the clip: {error}"))?
    {
        Some(row) => clip_result_from_row(connection, assets_dir, row, include_html),
        None => Err("Clip not found".to_owned()),
    }
}

fn clip_result_from_row(
    connection: &Connection,
    assets_dir: &Path,
    row: &Row<'_>,
    include_html: bool,
) -> Result<Value, String> {
    let id = text(row, "id")?;
    let mut object = Map::new();
    for (json_name, column) in [
        ("id", "id"),
        ("kind", "kind"),
        ("title", "title"),
        ("text", "text"),
        ("note", "note"),
        ("sourceUrl", "source_url"),
        ("scope", "scope"),
        ("status", "status"),
        ("createdAt", "created_at"),
        ("deletedAt", "deleted_at"),
    ] {
        object.insert(json_name.to_owned(), Value::String(text(row, column)?));
    }
    object.insert(
        "assets".to_owned(),
        Value::Array(clip_assets(connection, assets_dir, &id)?),
    );
    object.insert(
        "missing".to_owned(),
        Value::Array(normalized_missing(&text(row, "missing")?)),
    );
    let revision: i64 = row
        .get("revision")
        .map_err(|error| format!("Could not read clip revision: {error}"))?;
    object.insert("revision".to_owned(), Value::Number(revision.into()));
    if include_html {
        object.insert("html".to_owned(), Value::String(text(row, "html")?));
    }
    if let Some(pending) = pending_capture_result(connection, &id)? {
        object.insert("pendingCapture".to_owned(), pending);
    }
    Ok(Value::Object(object))
}

fn clip_assets(
    connection: &Connection,
    assets_dir: &Path,
    clip_id: &str,
) -> Result<Vec<Value>, String> {
    let mut statement = connection
        .prepare(
            "SELECT a.id,a.mime,a.name,a.filename FROM assets a \
             JOIN clip_assets ca ON ca.asset_id=a.id WHERE ca.clip_id=? ORDER BY ca.position",
        )
        .map_err(|error| format!("Could not prepare the clip assets: {error}"))?;
    let mut rows = statement
        .query(params![clip_id])
        .map_err(|error| format!("Could not load clip assets: {error}"))?;
    let mut assets = Vec::new();
    while let Some(row) = rows
        .next()
        .map_err(|error| format!("Could not load clip assets: {error}"))?
    {
        let filename = text(row, "filename")?;
        let path = assets_dir.join(filename);
        assets.push(json!({
            "id": text(row, "id")?,
            "url": format!("file://{}", percent_encode_path(&path.to_string_lossy())),
            "mime": text(row, "mime")?,
            "name": text(row, "name")?,
        }));
    }
    Ok(assets)
}

fn pending_capture_result(connection: &Connection, clip_id: &str) -> Result<Option<Value>, String> {
    let mut statement = connection
        .prepare(
            "SELECT * FROM captures WHERE clip_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        )
        .map_err(|error| format!("Could not prepare the pending capture: {error}"))?;
    let mut rows = statement
        .query(params![clip_id])
        .map_err(|error| format!("Could not load the pending capture: {error}"))?;
    let Some(row) = rows
        .next()
        .map_err(|error| format!("Could not load the pending capture: {error}"))?
    else {
        return Ok(None);
    };
    let stored_status = text(row, "status")?;
    let update_id = text(row, "update_id")?;
    Ok(Some(json!({
        "captureId": text(row, "id")?,
        "status": if stored_status == "pending" { "pending" } else { "incomplete" },
        "reason": text(row, "reason")?,
        "missing": normalized_missing(&text(row, "missing")?),
        "isUpdate": !update_id.is_empty(),
        "retryable": true,
        "sourceUrl": text(row, "source_url")?,
        "scope": text(row, "scope")?,
        "createdAt": text(row, "created_at")?,
    })))
}

fn text(row: &Row<'_>, name: &str) -> Result<String, String> {
    row.get::<_, Option<String>>(name)
        .map(|value| value.unwrap_or_default())
        .map_err(|error| format!("Could not read {name}: {error}"))
}

fn string_value(request: &Value, name: &str, default: &str) -> String {
    request
        .get(name)
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_owned()
}

fn bool_value(request: &Value, name: &str, default: bool) -> bool {
    request
        .get(name)
        .and_then(Value::as_bool)
        .unwrap_or(default)
}

fn int_value(request: &Value, name: &str, default: i64) -> i64 {
    let Some(number) = request.get(name).and_then(Value::as_f64) else {
        return default;
    };
    if number.fract() == 0.0 && number >= i32::MIN as f64 && number <= i32::MAX as f64 {
        number as i64
    } else {
        default
    }
}

fn required_revision(request: &Value) -> Result<i64, String> {
    request
        .get("expectedRevision")
        .and_then(Value::as_f64)
        .map(|value| value as i64)
        .ok_or_else(|| "expectedRevision is required".to_owned())
}

fn optional_revision(request: &Value, default: i64) -> i64 {
    request
        .get("expectedRevision")
        .and_then(Value::as_f64)
        .map(|value| value as i64)
        .unwrap_or(default)
}

fn string_list(request: &Value, name: &str) -> Result<Vec<String>, String> {
    let values = request
        .get(name)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{name} must be an array"))?;
    let mut result = Vec::new();
    for value in values {
        let Some(value) = value.as_str().filter(|value| !value.is_empty()) else {
            return Err(format!("{name} contains an invalid ID"));
        };
        if !result.iter().any(|existing| existing == value) {
            result.push(value.to_owned());
        }
    }
    Ok(result)
}

fn valid_capture_scope(scope: &str) -> bool {
    let bytes = scope.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes[0].is_ascii_lowercase()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

fn title_from_text(text: &str, has_images: bool) -> String {
    for line in text.split('\n') {
        let mut clean = line.trim();
        while let Some(rest) = clean.strip_prefix('#') {
            clean = rest;
        }
        clean = clean.trim();
        if !clean.is_empty() {
            return left_chars(clean, 100);
        }
    }
    if has_images {
        "Image clip".to_owned()
    } else {
        "Untitled clip".to_owned()
    }
}

fn encoded_string_ids(encoded: &str) -> Vec<String> {
    serde_json::from_str::<Value>(encoded)
        .ok()
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|value| value.as_str().map(str::to_owned))
        .collect()
}

pub(crate) fn normalized_missing(encoded: &str) -> Vec<Value> {
    let Ok(Value::Array(items)) = serde_json::from_str(encoded) else {
        return Vec::new();
    };
    items
        .into_iter()
        .map(|item| {
            let Value::Object(mut object) = item else {
                return item;
            };
            let reason = object.get("reason").and_then(Value::as_str).unwrap_or("");
            let category = object.get("category").and_then(Value::as_str).unwrap_or("");
            let category = if category.is_empty() {
                if reason == "Permission is required to retry this public image origin." {
                    "permission"
                } else if reason.to_lowercase().contains("not a supported image type")
                    || matches!(
                        reason,
                        "Asset image dimensions are unsupported"
                            | "Asset image format is unsupported"
                            | "Declared MIME type does not match image data"
                    )
                {
                    "unsupported"
                } else {
                    "transient"
                }
            } else {
                category
            }
            .to_owned();
            object.insert("category".to_owned(), Value::String(category.clone()));
            if !object.get("retryable").is_some_and(Value::is_boolean) {
                object.insert(
                    "retryable".to_owned(),
                    Value::Bool(category != "unsupported"),
                );
            }
            Value::Object(object)
        })
        .collect()
}

fn validate_source_url(url: &str) -> Result<(), String> {
    let Some((scheme, remainder)) = url.split_once("://") else {
        return Err("sourceUrl must be an absolute HTTP(S) URL".to_owned());
    };
    if !(scheme.eq_ignore_ascii_case("http") || scheme.eq_ignore_ascii_case("https"))
        || url.chars().any(|character| {
            character.is_control()
                || character.is_whitespace()
                || matches!(
                    character,
                    '"' | '<' | '>' | '\\' | '^' | '`' | '{' | '|' | '}'
                )
        })
        || !has_valid_percent_encoding(url.as_bytes())
    {
        return Err("sourceUrl must be an absolute HTTP(S) URL".to_owned());
    }
    let authority = remainder.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.matches('@').count() > 1 {
        return Err("sourceUrl must be an absolute HTTP(S) URL".to_owned());
    }
    let host_port = authority.rsplit('@').next().unwrap_or_default();
    let valid_host = if let Some(bracketed) = host_port.strip_prefix('[') {
        let Some((host, suffix)) = bracketed.split_once(']') else {
            return Err("sourceUrl must be an absolute HTTP(S) URL".to_owned());
        };
        host.parse::<std::net::Ipv6Addr>().is_ok() && valid_port_suffix(suffix)
    } else {
        let mut parts = host_port.splitn(2, ':');
        let host = parts.next().unwrap_or_default();
        let suffix = parts.next().map(|port| format!(":{port}"));
        !host.is_empty()
            && !matches!(host, "." | "..")
            && !host.contains(['[', ']'])
            && suffix.as_deref().is_none_or(valid_port_suffix)
    };
    if !valid_host {
        return Err("sourceUrl must be an absolute HTTP(S) URL".to_owned());
    }
    Ok(())
}

fn has_valid_percent_encoding(bytes: &[u8]) -> bool {
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            if index + 2 >= bytes.len()
                || !bytes[index + 1].is_ascii_hexdigit()
                || !bytes[index + 2].is_ascii_hexdigit()
            {
                return false;
            }
            index += 3;
        } else {
            index += 1;
        }
    }
    true
}

fn valid_port_suffix(suffix: &str) -> bool {
    suffix.is_empty()
        || suffix
            .strip_prefix(':')
            .is_some_and(|port| port.is_empty() || port.parse::<u16>().is_ok())
}

pub(crate) fn storage_error(context: &str, error: rusqlite::Error) -> String {
    if matches!(
        error.sqlite_error_code(),
        Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
    ) {
        format!("{context}: Collection storage stayed busy for 3 seconds; retry the operation")
    } else {
        format!("{context}: {error}")
    }
}

fn retry_busy<T>(
    context: &str,
    mut operation: impl FnMut() -> rusqlite::Result<T>,
) -> Result<T, String> {
    let started = Instant::now();
    let mut delay = Duration::from_millis(10);
    loop {
        match operation() {
            Ok(value) => return Ok(value),
            Err(error)
                if matches!(
                    error.sqlite_error_code(),
                    Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
                ) =>
            {
                let remaining = STORAGE_BUSY_TIMEOUT.saturating_sub(started.elapsed());
                if remaining.is_zero() {
                    return Err(storage_error(context, error));
                }
                thread::sleep(delay.min(remaining));
                delay = (delay * 2).min(Duration::from_millis(100));
            }
            Err(error) => return Err(storage_error(context, error)),
        }
    }
}

pub(crate) fn new_id(connection: &Connection) -> Result<String, String> {
    let mut bytes: Vec<u8> = connection
        .query_row("SELECT randomblob(16)", [], |row| row.get(0))
        .map_err(|error| storage_error("Could not generate an identifier", error))?;
    if bytes.len() != 16 {
        return Err("Could not generate an identifier".to_owned());
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex = bytes
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    ))
}

pub(crate) fn now(connection: &Connection) -> Result<String, String> {
    connection
        .query_row("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')", [], |row| {
            row.get(0)
        })
        .map_err(|error| storage_error("Could not read the current time", error))
}

fn left_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

pub(crate) fn percent_encode_path(path: &str) -> String {
    let mut encoded = String::new();
    for byte in path.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(*byte, b'/' | b'-' | b'_' | b'.' | b'~') {
            encoded.push(char::from(*byte));
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}
