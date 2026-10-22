mod assets;
mod captures;
mod desktop;
mod helper;
mod storage_security;
mod store;

use serde_json::{json, Value};
use std::env;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};
use store::Store;

const MAX_REQUEST_BYTES: usize = 24 * 1024 * 1024;
const MAX_NATIVE_RESPONSE_BYTES: usize = 1024 * 1024;

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Request,
    Native,
    Open,
}

struct Options {
    mode: Mode,
    data_dir: PathBuf,
    helper_bin: Option<PathBuf>,
    reader_bin: PathBuf,
    backend_bin: PathBuf,
    ui_dir: PathBuf,
    open_id: Option<String>,
    lifetime_lock: Option<File>,
    expected_origin: Option<String>,
    actual_origin: Option<String>,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            if !error.is_empty() {
                eprintln!("{error}");
            }
            ExitCode::from(2)
        }
    }
}

fn run() -> Result<(), String> {
    let options = parse_options()?;
    if options.mode == Mode::Native {
        validate_native_origin(
            options.expected_origin.as_deref(),
            options.actual_origin.as_deref(),
        )?;
    }
    match options.mode {
        Mode::Request => run_request(&options),
        Mode::Native => run_native(&options),
        Mode::Open => {
            let error = reader_command(&options, options.open_id.as_deref().unwrap_or("")).exec();
            Err(format!("Could not open the detail window: {error}"))
        }
    }
}

fn parse_options() -> Result<Options, String> {
    let mut mode = None;
    let mut data_dir = None;
    let mut expected_origin = None;
    let mut helper_bin = None;
    let mut reader_bin = None;
    let mut ui_dir = None;
    let mut open_id = None;
    let mut positional = Vec::new();
    let mut args = env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--request" => set_mode(&mut mode, Mode::Request)?,
            "--native" => set_mode(&mut mode, Mode::Native)?,
            "--open" => {
                set_mode(&mut mode, Mode::Open)?;
                open_id = Some(args.next().ok_or("--open requires a value")?);
            }
            "--reader-bin" => {
                reader_bin = Some(PathBuf::from(
                    args.next().ok_or("--reader-bin requires a value")?,
                ))
            }
            "--ui-dir" => {
                ui_dir = Some(PathBuf::from(
                    args.next().ok_or("--ui-dir requires a value")?,
                ))
            }
            "--data-dir" => {
                data_dir = Some(PathBuf::from(
                    args.next().ok_or("--data-dir requires a value")?,
                ));
            }
            "--allowed-origin" => {
                expected_origin = Some(args.next().ok_or("--allowed-origin requires a value")?);
            }
            "--helper-bin" => {
                helper_bin = Some(PathBuf::from(
                    args.next().ok_or("--helper-bin requires a value")?,
                ));
            }
            "--help" => {
                println!(
                    "Usage: omarchy-clipper-backend (--request | --native | --open ID) [--data-dir PATH] \
                     [--helper-bin PATH] [--reader-bin PATH] [--ui-dir PATH] \
                     [--allowed-origin ORIGIN [ACTUAL_ORIGIN]]"
                );
                std::process::exit(0);
            }
            "--version" => {
                println!("omarchy-clipper-backend 0.1.0");
                std::process::exit(0);
            }
            value if value.starts_with('-') => return Err(format!("Unknown option: {value}")),
            value => positional.push(value.to_owned()),
        }
    }
    if positional.len() > 1 {
        return Err("Unexpected positional arguments".to_owned());
    }
    Ok(Options {
        mode: mode.ok_or("Either --request, --native or --open is required")?,
        data_dir: match data_dir {
            Some(path) => path,
            None => default_data_dir()?,
        },
        helper_bin: Some(match helper_bin {
            Some(path) => path,
            None => default_helper_bin()?,
        }),
        expected_origin,
        reader_bin: reader_bin.unwrap_or(default_install_path(
            "libexec/omarchy-clipper/clipper-reader",
        )?),
        backend_bin: env::current_exe()
            .map_err(|error| format!("Could not locate the backend executable: {error}"))?,
        ui_dir: ui_dir.unwrap_or(default_install_path("share/omarchy-clipper/ui")?),
        open_id,
        lifetime_lock: installation_lock()?,
        actual_origin: positional.into_iter().next(),
    })
}

fn default_data_dir() -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("XDG_DATA_HOME").filter(|value| !value.is_empty()) {
        return Ok(PathBuf::from(path).join("omarchy-clipper"));
    }
    env::var_os("HOME")
        .filter(|value| !value.is_empty())
        .map(|path| PathBuf::from(path).join(".local/share/omarchy-clipper"))
        .ok_or_else(|| "Could not determine the collection data directory".to_owned())
}

fn installation_lock() -> Result<Option<File>, String> {
    let executable = env::current_exe().map_err(|error| error.to_string())?;
    // Development artifacts have no installation lifecycle. Installed entry points
    // must share the installer's persistent lock inode for their entire lifetime.
    if executable.file_name().and_then(|name| name.to_str()) != Some("clipper")
        || executable
            .parent()
            .and_then(|path| path.file_name())
            .and_then(|name| name.to_str())
            != Some("bin")
    {
        return Ok(None);
    }
    let share_path = default_install_path("share")?;
    let share = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(&share_path)
        .map_err(|error| format!("Could not open the installation share directory: {error}"))?;
    let share_metadata = share
        .metadata()
        .map_err(|error| format!("Could not inspect the installation share directory: {error}"))?;
    if !share_metadata.is_dir()
        || share_metadata.uid() != unsafe { libc::geteuid() }
        || share_metadata.mode() & 0o022 != 0
    {
        return Err("The installation share directory is unsafe".to_owned());
    }

    let name = c".omarchy-clipper-backend.lock";
    // SAFETY: share is a live directory descriptor and name is a static,
    // NUL-terminated filename without path separators.
    let descriptor = unsafe {
        libc::openat(
            share.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
        )
    };
    if descriptor == -1 {
        return Err(format!(
            "Could not open the installation lifetime lock: {}",
            io::Error::last_os_error()
        ));
    }
    // SAFETY: openat returned a new owned descriptor.
    let file = unsafe { File::from_raw_fd(descriptor) };
    let metadata = file
        .metadata()
        .map_err(|error| format!("Could not inspect the installation lifetime lock: {error}"))?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.nlink() != 1
        || metadata.mode() & 0o777 != 0o600
    {
        return Err("The installation lifetime lock is unsafe".to_owned());
    }
    file.try_lock_shared()
        .map_err(|_| "The installation is being changed; retry after it completes".to_owned())?;
    Ok(Some(file))
}

fn default_helper_bin() -> Result<PathBuf, String> {
    default_install_path("libexec/omarchy-clipper/clipper-helper")
}

fn default_install_path(relative: &str) -> Result<PathBuf, String> {
    let executable = env::current_exe()
        .map_err(|error| format!("Could not locate the backend executable: {error}"))?;
    let directory = executable
        .parent()
        .ok_or_else(|| "Could not locate the backend executable directory".to_owned())?;
    let prefix = directory
        .parent()
        .ok_or("Could not locate the installation prefix")?;
    Ok(prefix.join(relative))
}

fn reader_command(options: &Options, id: &str) -> Command {
    let mut command = Command::new(&options.reader_bin);
    command
        .arg("--data-dir")
        .arg(&options.data_dir)
        .arg("--open")
        .arg(id)
        .arg("--ui-dir")
        .arg(&options.ui_dir);
    command.arg("--backend-bin").arg(&options.backend_bin);
    if let Some(lock) = options.lifetime_lock.as_ref() {
        let descriptor = lock.as_raw_fd();
        // SAFETY: fcntl is async-signal-safe, the descriptor remains owned by
        // options, and this callback runs only in the Reader child immediately
        // before exec. Other child processes keep the default close-on-exec bit.
        unsafe {
            command.pre_exec(move || clear_close_on_exec(descriptor));
        }
    }
    command
}

fn clear_close_on_exec(descriptor: RawFd) -> io::Result<()> {
    // SAFETY: descriptor is a live installation lock descriptor while the
    // Reader command is being executed.
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    if flags == -1 {
        return Err(io::Error::last_os_error());
    }
    if flags & libc::FD_CLOEXEC != 0
        // SAFETY: F_SETFD only updates flags on the live descriptor.
        && unsafe { libc::fcntl(descriptor, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } == -1
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn set_mode(mode: &mut Option<Mode>, value: Mode) -> Result<(), String> {
    if mode.replace(value).is_some() {
        return Err("Choose exactly one of --request, --native or --open".to_owned());
    }
    Ok(())
}

fn validate_native_origin(expected: Option<&str>, actual: Option<&str>) -> Result<(), String> {
    if !matches!(
        (expected, actual),
        (Some(expected), Some(actual))
            if expected.starts_with("chrome-extension://") && actual == expected
    ) {
        return Err("Browser origin is not allowed".to_owned());
    }
    Ok(())
}

fn run_request(options: &Options) -> Result<(), String> {
    let mut bytes = Vec::new();
    io::stdin()
        .take((MAX_REQUEST_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("Could not read request: {error}"))?;
    let response = if bytes.len() > MAX_REQUEST_BYTES {
        json!({"ok": false, "error": "Request is too large"})
    } else {
        let request = object_or_empty(&bytes);
        dispatch(options, &request)?
    };
    println!(
        "{}",
        serde_json::to_string(&response)
            .map_err(|error| format!("Could not encode response: {error}"))?
    );
    Ok(())
}

fn run_native(options: &Options) -> Result<(), String> {
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    loop {
        let mut header = [0_u8; 4];
        match input.read_exact(&mut header) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(error) => return Err(format!("Could not read native message header: {error}")),
        }
        let length = u32::from_ne_bytes(header) as usize;
        if length == 0 || length > MAX_REQUEST_BYTES {
            return Err(String::new());
        }
        let mut bytes = vec![0_u8; length];
        input.read_exact(&mut bytes).map_err(|_| String::new())?;
        let request = object_or_empty(&bytes);
        let op = request.get("op").and_then(Value::as_str).unwrap_or("");
        let response = if matches!(
            op,
            "capture.check"
                | "capture.pending"
                | "capture.begin"
                | "capture.abort"
                | "capture.finish"
                | "capture.mixed"
                | "capture.region"
                | "asset.import"
                | "clip.open"
                | "clip.restore"
                | "settings.get"
        ) {
            native_metadata(dispatch(options, &request)?)
        } else {
            json!({"ok": false, "error": "Operation is not available to browser clients"})
        };
        let mut encoded = serde_json::to_vec(&response)
            .map_err(|error| format!("Could not encode native response: {error}"))?;
        if encoded.len() > MAX_NATIVE_RESPONSE_BYTES {
            encoded =
                serde_json::to_vec(&json!({"ok": false, "error": "Native response is too large"}))
                    .map_err(|error| format!("Could not encode native response: {error}"))?;
        }
        output
            .write_all(&(encoded.len() as u32).to_ne_bytes())
            .and_then(|_| output.write_all(&encoded))
            .and_then(|_| output.flush())
            .map_err(|error| format!("Could not write native response: {error}"))?;
    }
}

fn dispatch(options: &Options, request: &Value) -> Result<Value, String> {
    let op = request.get("op").and_then(Value::as_str).unwrap_or("");
    if op.is_empty() {
        return Ok(json!({"ok":false,"error":"Missing operation"}));
    }
    if op == "settings.get" {
        return Ok(settings_get());
    }
    if !store::supports(op)
        && !matches!(
            op,
            "theme.get"
                | "link.open"
                | "file.import"
                | "clipboard.import"
                | "clipboard.copy"
                | "capture.region"
                | "clip.open"
        )
    {
        return Ok(json!({"ok":false,"error":format!("Unknown operation: {op}")}));
    }
    if request.get("op").and_then(Value::as_str) == Some("clip.open") {
        let mut store = Store::open_read(&options.data_dir)?;
        let found = store.dispatch(
            &json!({"op":"clip.get", "id": request.get("id")}),
            options.helper_bin.as_deref(),
        );
        if found.get("ok").and_then(Value::as_bool) != Some(true) {
            return Ok(found);
        }
        let id = request.get("id").and_then(Value::as_str).unwrap_or("");
        return Ok(
            match reader_command(options, id)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .process_group(0)
                .spawn()
            {
                Ok(mut child) => {
                    std::thread::spawn(move || {
                        let _ = child.wait();
                    });
                    json!({"ok":true,"result":true})
                }
                Err(_) => json!({"ok":false,"error":"Could not open the detail window"}),
            },
        );
    }
    if op == "asset.reconcile" && request.get("confirm").and_then(Value::as_bool) != Some(true) {
        let Some(mut store) = Store::open_inspect(&options.data_dir)? else {
            return Ok(json!({
                "ok": true,
                "result": {
                    "mode": "inspect",
                    "items": [],
                    "hasMore": false,
                    "nextAfter": Value::Null,
                    "readOnly": true,
                }
            }));
        };
        return Ok(store.dispatch(request, options.helper_bin.as_deref()));
    }
    let mut store = if Store::requires_write(request)
        || matches!(
            request.get("op").and_then(Value::as_str),
            Some("file.import" | "clipboard.import" | "capture.region")
        ) {
        Store::open_write(&options.data_dir)?
    } else {
        Store::open_read(&options.data_dir)?
    };
    if let Some(helper) = options.helper_bin.as_deref() {
        if let Some(response) =
            desktop::dispatch(request, &mut store, helper, options.mode == Mode::Native)
        {
            return Ok(response);
        }
    }
    Ok(store.dispatch(request, options.helper_bin.as_deref()))
}

fn settings_get() -> Value {
    match read_language_settings() {
        Ok(language) => json!({
            "ok": true,
            "result": {
                "resolvedLanguage": resolved_language(&language),
                "language": language,
            }
        }),
        Err(error) => json!({"ok": false, "error": error}),
    }
}

fn read_language_settings() -> Result<String, String> {
    let config_dir = if let Some(path) = env::var_os("XDG_CONFIG_HOME").filter(|v| !v.is_empty()) {
        PathBuf::from(path)
    } else {
        PathBuf::from(
            env::var_os("HOME")
                .filter(|v| !v.is_empty())
                .ok_or("Could not read Clipper settings; check configuration permissions.")?,
        )
        .join(".config")
    };
    let retained_path = config_dir.join("omarchy-clipper/settings.json");
    if retained_path.exists() {
        let bytes = std::fs::read(&retained_path).map_err(|_| {
            "Could not read Clipper settings; check configuration permissions.".to_owned()
        })?;
        let document: Value = serde_json::from_slice(&bytes)
            .map_err(|_| "Invalid Clipper settings; repair the saved configuration.".to_owned())?;
        return retained_language(&document)
            .ok_or_else(|| "Invalid Clipper settings; repair the saved configuration.".to_owned());
    }

    let legacy_path = config_dir.join("omarchy/shell.json");
    let Ok(bytes) = std::fs::read(legacy_path) else {
        return Ok("system".to_owned());
    };
    let Ok(document) = serde_json::from_slice::<Value>(&bytes) else {
        return Ok("system".to_owned());
    };
    Ok(configured_language(&document))
}

fn retained_language(document: &Value) -> Option<String> {
    let root = document.as_object()?;
    if root.get("version").and_then(Value::as_f64) != Some(1.0) {
        return None;
    }
    let settings = root.get("settings")?.as_object()?;
    const KNOWN_KEYS: &[&str] = &[
        "language",
        "previewEnabled",
        "previewSide",
        "dataDir",
        "executable",
    ];
    if settings
        .keys()
        .any(|key| !KNOWN_KEYS.contains(&key.as_str()))
    {
        return None;
    }
    let language = match settings.get("language") {
        None => "system",
        Some(value) => value.as_str()?,
    };
    if !matches!(language, "system" | "zh" | "en") {
        return None;
    }
    if settings
        .get("previewEnabled")
        .is_some_and(|value| !value.is_boolean())
    {
        return None;
    }
    let preview_side = match settings.get("previewSide") {
        None => "right",
        Some(value) => value.as_str()?,
    };
    if !matches!(preview_side, "left" | "right") {
        return None;
    }
    let data_dir = match settings.get("dataDir") {
        None => "",
        Some(value) => value.as_str()?,
    };
    if data_dir.contains('\0') || (!data_dir.is_empty() && !data_dir.starts_with('/')) {
        return None;
    }
    let executable = match settings.get("executable") {
        None => "clipper",
        Some(value) => value.as_str()?,
    };
    if executable.contains('\0') || executable.trim().is_empty() {
        return None;
    }
    Some(language.to_owned())
}

fn configured_language(document: &Value) -> String {
    const SECTIONS: &[&str] = &[
        "left",
        "centerBegin",
        "center",
        "centerEnd",
        "right",
        "tray",
    ];
    let Some(layout) = document
        .get("bar")
        .and_then(|value| value.get("layout"))
        .and_then(Value::as_object)
    else {
        return "system".to_owned();
    };
    for section in SECTIONS {
        let Some(entries) = layout.get(*section).and_then(Value::as_array) else {
            continue;
        };
        for entry in entries {
            if entry.get("id").and_then(Value::as_str) != Some("lcryolite.clipper") {
                continue;
            }
            if let Some(language @ ("system" | "zh" | "en")) =
                entry.get("language").and_then(Value::as_str)
            {
                return language.to_owned();
            }
        }
    }
    "system".to_owned()
}

fn resolved_language(language: &str) -> &'static str {
    if language == "zh" {
        return "zh";
    }
    if language == "en" {
        return "en";
    }
    let locale = ["LC_ALL", "LC_MESSAGES", "LANG"]
        .iter()
        .find_map(|name| env::var(name).ok().filter(|value| !value.is_empty()))
        .unwrap_or_default()
        .to_lowercase();
    if locale == "zh" || locale.starts_with("zh_") || locale.starts_with("zh-") {
        "zh"
    } else {
        "en"
    }
}

fn object_or_empty(bytes: &[u8]) -> Value {
    match serde_json::from_slice(bytes) {
        Ok(Value::Object(object)) => Value::Object(object),
        _ => json!({}),
    }
}

fn native_metadata(value: Value) -> Value {
    match value {
        Value::Array(items) => Value::Array(items.into_iter().map(native_metadata).collect()),
        Value::Object(object) => {
            let mut filtered = object
                .into_iter()
                .map(|(key, value)| (key, native_metadata(value)))
                .collect::<serde_json::Map<_, _>>();
            if filtered.contains_key("kind") && filtered.contains_key("id") {
                filtered.remove("html");
                filtered.remove("text");
                filtered.remove("note");
            }
            if filtered.contains_key("mime") && filtered.contains_key("id") {
                filtered.remove("url");
            }
            Value::Object(filtered)
        }
        scalar => scalar,
    }
}
