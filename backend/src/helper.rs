use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const MAX_HELPER_RESPONSE_BYTES: usize = 24 * 1024 * 1024;
const MAX_HELPER_DIAGNOSTIC_BYTES: usize = 8 * 1024;
const HELPER_TIMEOUT: Duration = Duration::from_secs(3);

pub struct CaptureSanitizer {
    path: PathBuf,
}

impl CaptureSanitizer {
    pub fn new(path: &Path) -> Self {
        Self {
            path: path.to_owned(),
        }
    }

    pub fn sanitize(&self, html: &str) -> Result<String, String> {
        let response = request(
            &self.path,
            &json!({
                "op": "html.sanitize_capture",
                "html": html,
                "assetIds": [],
            }),
            "HTML sanitizer helper",
        )?;
        if response.get("ok").and_then(Value::as_bool) != Some(true) {
            return Err(response
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("HTML sanitizer helper failed")
                .to_owned());
        }
        response
            .pointer("/result/html")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| "HTML sanitizer helper returned an invalid response".to_owned())
    }
}

pub(crate) fn request(path: &Path, request: &Value, context: &str) -> Result<Value, String> {
    request_with_environment(path, request, context, &[])
}

pub(crate) fn request_with_environment(
    path: &Path,
    request: &Value,
    context: &str,
    environment_keys: &[&str],
) -> Result<Value, String> {
    request_with_options(path, request, context, environment_keys, HELPER_TIMEOUT)
}

pub(crate) fn request_with_options(
    path: &Path,
    request: &Value,
    context: &str,
    environment_keys: &[&str],
    timeout: Duration,
) -> Result<Value, String> {
    let input = serde_json::to_vec(request)
        .map_err(|error| format!("Could not encode {context} request: {error}"))?;
    let mut command = Command::new(path);
    command
        .arg("--request")
        .env_clear()
        .env("LANG", "C.UTF-8")
        .process_group(0)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for key in environment_keys {
        if let Some(value) = env::var_os(key) {
            command.env(key, value);
        }
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start {context}: {error}"))?;
    let process_group = child.id();
    let pipes = (child.stdin.take(), child.stdout.take(), child.stderr.take());
    let (Some(mut stdin), Some(stdout), Some(stderr)) = pipes else {
        let _ = stop_helper_tree(&mut child, process_group, false);
        return Err(format!("Could not open {context} pipes"));
    };
    let stdout_reader = thread::spawn(move || read_bounded(stdout, MAX_HELPER_RESPONSE_BYTES));
    let stderr_reader = thread::spawn(move || read_bounded(stderr, MAX_HELPER_DIAGNOSTIC_BYTES));
    let stdin_writer = thread::spawn(move || stdin.write_all(&input));

    let deadline = Instant::now() + timeout;
    let mut timed_out = false;
    let mut wait_error = None;
    let leader_exited = loop {
        match process_exited(process_group) {
            Ok(true) => break true,
            Ok(false) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            Ok(false) => {
                timed_out = true;
                break false;
            }
            Err(error) => {
                wait_error = Some(error);
                break false;
            }
        }
    };

    // The helper owns a fresh process group. Kill that group before joining
    // pipe readers so a forked descendant cannot retain a pipe indefinitely.
    // Observe Linux's zombie state without reaping the leader first, so its PID
    // cannot be reused as an unrelated process-group ID before cleanup.
    let status = stop_helper_tree(&mut child, process_group, leader_exited);
    let input_result = stdin_writer.join();
    let output_result = stdout_reader.join();
    let diagnostics_result = stderr_reader.join();

    if timed_out {
        return Err(format!("{context} timed out"));
    }
    if let Some(error) = wait_error {
        return Err(format!("Could not wait for {context}: {error}"));
    }
    input_result
        .map_err(|_| format!("{context} request writer failed"))?
        .map_err(|error| format!("Could not write {context} request: {error}"))?;
    let output = output_result
        .map_err(|_| format!("{context} response reader failed"))?
        .map_err(|error| format!("Could not read {context} response: {error}"))?;
    let diagnostics = diagnostics_result
        .map_err(|_| format!("{context} diagnostics reader failed"))?
        .map_err(|error| format!("Could not read {context} diagnostics: {error}"))?;
    if output.len() > MAX_HELPER_RESPONSE_BYTES {
        return Err(format!("{context} response is too large"));
    }
    if diagnostics.len() > MAX_HELPER_DIAGNOSTIC_BYTES {
        return Err(format!("{context} diagnostics are too large"));
    }
    let status = status.map_err(|error| format!("Could not wait for {context}: {error}"))?;
    if !status.success() {
        return Err(format!("{context} failed with {status}"));
    }

    let response: Value = serde_json::from_slice(&output)
        .map_err(|_| format!("{context} returned an invalid response"))?;
    if response.get("ok").and_then(Value::as_bool).is_none() {
        return Err(format!("{context} returned an invalid response"));
    }
    Ok(response)
}

fn process_exited(pid: u32) -> std::io::Result<bool> {
    let status = fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let delimiter = status
        .rfind(") ")
        .ok_or_else(|| std::io::Error::other("invalid process status"))?;
    Ok(status
        .as_bytes()
        .get(delimiter + 2)
        .is_some_and(|state| matches!(*state, b'Z' | b'X')))
}

fn stop_helper_tree(
    child: &mut Child,
    process_group: u32,
    leader_exited: bool,
) -> std::io::Result<std::process::ExitStatus> {
    let _ = Command::new("/usr/bin/kill")
        .args(["-KILL", "--", &format!("-{process_group}")])
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    if !leader_exited {
        let _ = child.kill();
    }
    child.wait()
}

fn read_bounded(reader: impl Read, limit: usize) -> std::io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    reader.take((limit + 1) as u64).read_to_end(&mut bytes)?;
    Ok(bytes)
}
