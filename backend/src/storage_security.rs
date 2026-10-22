use std::ffi::OsStr;
use std::fs::{self, DirBuilder, File, OpenOptions};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::io::AsRawFd;
use std::path::{Component, Path, PathBuf};

const O_DIRECTORY: i32 = 0o200000;
const O_NOFOLLOW: i32 = 0o400000;
const SQLITE_SIDECAR_RETRIES: u8 = 16;

const DATABASE: &str = "collection.sqlite3";
const SIDECARS: [&str; 3] = [
    "collection.sqlite3-wal",
    "collection.sqlite3-shm",
    "collection.sqlite3-journal",
];

pub struct SecureStorage {
    _root: File,
    assets: File,
    database: File,
    _inspection_shm: Option<File>,
    display_root: PathBuf,
}
impl SecureStorage {
    pub fn open_existing(path: &Path) -> Result<Option<Self>, String> {
        Self::open(path, false)
    }

    pub fn open_or_create(path: &Path) -> Result<Self, String> {
        Self::open(path, true)?.ok_or_else(|| storage_error("database creation failed"))
    }

    pub fn inspect(path: &Path) -> Result<Option<Self>, String> {
        let display_root = absolute_path(path)?;
        reject_broad_root(&display_root)?;
        let Some(root) = open_existing_directory_chain(&display_root)? else {
            return Ok(None);
        };
        validate_collection_contents(&root, &display_root)?;
        verify_directory(&root, &display_root)?;
        let root_access = descriptor_path(&root);
        let Some(assets) = validate_assets_directory(&root_access, &display_root.join("assets"))?
        else {
            return Ok(None);
        };
        let Some(database) = validate_file(&root_access, DATABASE, true)? else {
            return Ok(None);
        };
        lock_file_for_inspection(&database, "database")?;
        let inspection_shm = validate_file(&root_access, "collection.sqlite3-shm", true)?;
        if let Some(shm) = inspection_shm.as_ref() {
            lock_file_for_inspection(shm, "WAL shared-memory file")?;
        }
        let wal = validate_file(&root_access, "collection.sqlite3-wal", false)?;
        if let Some(wal) = wal {
            let metadata = wal.metadata().map_err(|error| {
                storage_io_error("could not inspect collection.sqlite3-wal", error)
            })?;
            if metadata.len() != 0 {
                return Err(storage_error(
                    "read-only inspection requires a checkpointed WAL; close other SQLite connections, checkpoint, and retry",
                ));
            }
        }
        let journal = validate_file(&root_access, "collection.sqlite3-journal", false)?;
        if let Some(journal) = journal {
            let metadata = journal.metadata().map_err(|error| {
                storage_io_error("could not inspect collection.sqlite3-journal", error)
            })?;
            if metadata.len() != 0 {
                return Err(storage_error(
                    "read-only inspection requires an empty rollback journal; close other SQLite connections, recover or remove the journal safely, and retry",
                ));
            }
        }
        Ok(Some(Self {
            _root: root,
            assets,
            database,
            _inspection_shm: inspection_shm,
            display_root,
        }))
    }

    pub fn database_path(&self) -> PathBuf {
        descriptor_path(&self.database)
    }

    pub fn assets_access_path(&self) -> PathBuf {
        descriptor_path(&self.assets)
    }

    pub fn assets_display_path(&self) -> PathBuf {
        self.display_root.join("assets")
    }

    fn open(path: &Path, create_database: bool) -> Result<Option<Self>, String> {
        let display_root = absolute_path(path)?;
        reject_broad_root(&display_root)?;
        let root = open_directory_chain(&display_root)?;
        validate_collection_contents(&root, &display_root)?;
        protect_directory(&root, &display_root)?;

        let root_access = descriptor_path(&root);
        let assets = secure_assets_directory(&root_access, &display_root.join("assets"))?;
        let database = match secure_file(&root_access, DATABASE, create_database, true, false)? {
            Some(file) => file,
            None => return Ok(None),
        };
        for name in SIDECARS {
            // SQLite may unlink a WAL sidecar between pathname and descriptor
            // checks. Retry that lifecycle race without retrying unsafe files.
            let _ = secure_file(&root_access, name, false, true, true)?;
        }
        Ok(Some(Self {
            _root: root,
            assets,
            database,
            _inspection_shm: None,
            display_root,
        }))
    }
}

fn absolute_path(path: &Path) -> Result<PathBuf, String> {
    let joined = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map_err(|error| storage_io_error("could not resolve the collection path", error))?
            .join(path)
    };
    let mut result = PathBuf::from("/");
    for component in joined.components() {
        match component {
            Component::RootDir | Component::CurDir => {}
            Component::Normal(value) => result.push(value),
            Component::ParentDir => {
                result.pop();
            }
            Component::Prefix(_) => {
                return Err(storage_error("the collection path is not supported"));
            }
        }
    }
    Ok(result)
}

fn reject_broad_root(path: &Path) -> Result<(), String> {
    if path == Path::new("/") {
        return Err(storage_error(
            "/ is too broad to use as a collection directory",
        ));
    }
    if let Some(home) = std::env::var_os("HOME") {
        if absolute_path(Path::new(&home)).ok().as_deref() == Some(path) {
            return Err(storage_error(&format!(
                "{} is too broad to use as a collection directory",
                path.display()
            )));
        }
    }
    Ok(())
}

fn open_directory_chain(path: &Path) -> Result<File, String> {
    let mut directory = open_directory(Path::new("/"), "/")?;
    let mut display = PathBuf::from("/");
    for component in path.components() {
        let Component::Normal(name) = component else {
            continue;
        };
        let parent_metadata = directory.metadata().map_err(|error| {
            storage_io_error(&format!("could not inspect {}", display.display()), error)
        })?;
        display.push(name);
        let child_path = descriptor_path(&directory).join(name);
        let before = match fs::symlink_metadata(&child_path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                if parent_metadata.mode() & 0o022 != 0 && parent_metadata.mode() & 0o1000 == 0 {
                    return Err(storage_error(&format!(
                        "{} can be replaced through a writable ancestor",
                        display.display()
                    )));
                }
                let mut builder = DirBuilder::new();
                builder.mode(0o700);
                match builder.create(&child_path) {
                    Ok(()) => fs::symlink_metadata(&child_path).map_err(|error| {
                        storage_io_error(&format!("could not inspect {}", display.display()), error)
                    })?,
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                        fs::symlink_metadata(&child_path).map_err(|error| {
                            storage_io_error(
                                &format!("could not inspect {}", display.display()),
                                error,
                            )
                        })?
                    }
                    Err(error) => {
                        return Err(storage_io_error(
                            &format!("could not create {}", display.display()),
                            error,
                        ));
                    }
                }
            }
            Err(error) => {
                return Err(storage_io_error(
                    &format!("could not inspect {}", display.display()),
                    error,
                ));
            }
        };
        if before.file_type().is_symlink() {
            return Err(storage_error(&format!(
                "{} is a symbolic link",
                display.display()
            )));
        }
        if !before.is_dir() {
            return Err(storage_error(&format!(
                "{} is not a directory",
                display.display()
            )));
        }
        validate_replaceable_ancestor(&parent_metadata, &before, &display)?;
        let opened = open_directory(&child_path, &display.to_string_lossy())?;
        let after = opened.metadata().map_err(|error| {
            storage_io_error(&format!("could not verify {}", display.display()), error)
        })?;
        if !same_file(&before, &after) {
            return Err(storage_error(&format!(
                "{} changed while it was being opened",
                display.display()
            )));
        }
        directory = opened;
    }
    ensure_owned(
        &directory.metadata().map_err(|error| {
            storage_io_error(&format!("could not inspect {}", path.display()), error)
        })?,
        path,
    )?;
    Ok(directory)
}

fn open_existing_directory_chain(path: &Path) -> Result<Option<File>, String> {
    let mut directory = open_directory(Path::new("/"), "/")?;
    let mut display = PathBuf::from("/");
    for component in path.components() {
        let Component::Normal(name) = component else {
            continue;
        };
        let parent_metadata = directory.metadata().map_err(|error| {
            storage_io_error(&format!("could not inspect {}", display.display()), error)
        })?;
        display.push(name);
        let child_path = descriptor_path(&directory).join(name);
        let before = match fs::symlink_metadata(&child_path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(storage_io_error(
                    &format!("could not inspect {}", display.display()),
                    error,
                ));
            }
        };
        if before.file_type().is_symlink() {
            return Err(storage_error(&format!(
                "{} is a symbolic link",
                display.display()
            )));
        }
        if !before.is_dir() {
            return Err(storage_error(&format!(
                "{} is not a directory",
                display.display()
            )));
        }
        validate_replaceable_ancestor(&parent_metadata, &before, &display)?;
        let opened = open_directory(&child_path, &display.to_string_lossy())?;
        let after = opened.metadata().map_err(|error| {
            storage_io_error(&format!("could not verify {}", display.display()), error)
        })?;
        if !same_file(&before, &after) {
            return Err(storage_error(&format!(
                "{} changed while it was being opened",
                display.display()
            )));
        }
        directory = opened;
    }
    ensure_owned(
        &directory.metadata().map_err(|error| {
            storage_io_error(&format!("could not inspect {}", path.display()), error)
        })?,
        path,
    )?;
    Ok(Some(directory))
}

fn validate_replaceable_ancestor(
    parent: &fs::Metadata,
    child: &fs::Metadata,
    display: &Path,
) -> Result<(), String> {
    if parent.mode() & 0o022 == 0 {
        return Ok(());
    }
    if parent.mode() & 0o1000 != 0 && child.uid() == effective_uid() {
        return Ok(());
    }
    Err(storage_error(&format!(
        "{} can be replaced through a writable ancestor",
        display.display()
    )))
}

fn validate_collection_contents(root: &File, display_root: &Path) -> Result<(), String> {
    let mut install_state = false;
    let mut ui = false;
    let mut extension = false;
    let root_access = descriptor_path(root);
    let entries = fs::read_dir(&root_access).map_err(|error| {
        storage_io_error(
            &format!("could not inspect {}", display_root.display()),
            error,
        )
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| {
            storage_io_error(
                &format!("could not inspect {}", display_root.display()),
                error,
            )
        })?;
        let name = entry.file_name();
        let managed = matches!(
            name.to_str(),
            Some(
                "assets"
                    | DATABASE
                    | "collection.sqlite3-wal"
                    | "collection.sqlite3-shm"
                    | "collection.sqlite3-journal"
            )
        );
        let recognized = matches!(
            name.to_str(),
            Some(".install-state.json" | "ui" | "extension")
        );
        if !managed && !recognized {
            return Err(storage_error(&format!(
                "{} contains unrelated entry {}",
                display_root.display(),
                name.to_string_lossy()
            )));
        }
        if recognized {
            let metadata = fs::symlink_metadata(entry.path()).map_err(|error| {
                storage_io_error(
                    &format!(
                        "could not inspect {}/{}",
                        display_root.display(),
                        name.to_string_lossy()
                    ),
                    error,
                )
            })?;
            ensure_owned(&metadata, &display_root.join(&name))?;
            let valid = match name.to_str() {
                Some(".install-state.json") => {
                    metadata.is_file() && !metadata.file_type().is_symlink()
                }
                Some("ui" | "extension") => metadata.is_dir() && !metadata.file_type().is_symlink(),
                _ => false,
            };
            if !valid {
                return Err(storage_error(&format!(
                    "{}/{} is not a recognized Clipper install resource",
                    display_root.display(),
                    name.to_string_lossy()
                )));
            }
            install_state |= name == OsStr::new(".install-state.json");
            ui |= name == OsStr::new("ui");
            extension |= name == OsStr::new("extension");
        }
    }
    if (ui || extension) && !install_state {
        return Err(storage_error(&format!(
            "{} contains install resources without Clipper install state",
            display_root.display()
        )));
    }
    Ok(())
}

fn secure_assets_directory(root: &Path, display: &Path) -> Result<File, String> {
    let path = root.join("assets");
    match fs::symlink_metadata(&path) {
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let mut builder = DirBuilder::new();
            builder.mode(0o700);
            match builder.create(&path) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => {
                    return Err(storage_io_error(
                        "could not create the asset directory",
                        error,
                    ));
                }
            }
        }
        Err(error) => {
            return Err(storage_io_error(
                "could not inspect the asset directory",
                error,
            ));
        }
    }
    let before = fs::symlink_metadata(&path)
        .map_err(|error| storage_io_error("could not inspect the asset directory", error))?;
    if before.file_type().is_symlink() {
        return Err(storage_error(&format!(
            "{} is a symbolic link",
            display.display()
        )));
    }
    if !before.is_dir() {
        return Err(storage_error(&format!(
            "{} is not a directory",
            display.display()
        )));
    }
    ensure_owned(&before, display)?;
    let directory = open_directory(&path, &display.to_string_lossy())?;
    let after = directory.metadata().map_err(|error| {
        storage_io_error(&format!("could not verify {}", display.display()), error)
    })?;
    if !same_file(&before, &after) {
        return Err(storage_error(&format!(
            "{} changed while it was being opened",
            display.display()
        )));
    }
    secure_directory_tree(&directory, display)?;
    Ok(directory)
}

fn validate_assets_directory(root: &Path, display: &Path) -> Result<Option<File>, String> {
    let path = root.join("assets");
    let before = match fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(storage_io_error(
                "could not inspect the asset directory",
                error,
            ));
        }
    };
    if before.file_type().is_symlink() {
        return Err(storage_error(&format!(
            "{} is a symbolic link",
            display.display()
        )));
    }
    if !before.is_dir() {
        return Err(storage_error(&format!(
            "{} is not a directory",
            display.display()
        )));
    }
    ensure_owned(&before, display)?;
    let directory = open_directory(&path, &display.to_string_lossy())?;
    let after = directory.metadata().map_err(|error| {
        storage_io_error(&format!("could not verify {}", display.display()), error)
    })?;
    if !same_file(&before, &after) {
        return Err(storage_error(&format!(
            "{} changed while it was being opened",
            display.display()
        )));
    }
    validate_directory_tree(&directory, display)?;
    Ok(Some(directory))
}

fn secure_directory_tree(directory: &File, display: &Path) -> Result<(), String> {
    protect_directory(directory, display)?;
    let access = descriptor_path(directory);
    let entries = fs::read_dir(&access).map_err(|error| {
        storage_io_error(&format!("could not scan {}", display.display()), error)
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| {
            storage_io_error(&format!("could not scan {}", display.display()), error)
        })?;
        let name = entry.file_name();
        let child_display = display.join(&name);
        let path = access.join(&name);
        let before = fs::symlink_metadata(&path).map_err(|error| {
            storage_io_error(
                &format!("could not inspect {}", child_display.display()),
                error,
            )
        })?;
        ensure_owned(&before, &child_display)?;
        if before.file_type().is_symlink() {
            return Err(storage_error(&format!(
                "{} is a symbolic link",
                child_display.display()
            )));
        }
        if before.is_dir() {
            let child = open_directory(&path, &child_display.to_string_lossy())?;
            let after = child.metadata().map_err(|error| {
                storage_io_error(
                    &format!("could not verify {}", child_display.display()),
                    error,
                )
            })?;
            if !same_file(&before, &after) {
                return Err(storage_error(&format!(
                    "{} changed while it was being opened",
                    child_display.display()
                )));
            }
            secure_directory_tree(&child, &child_display)?;
        } else {
            if !before.is_file() {
                return Err(storage_error(&format!(
                    "{} is not a regular file or directory",
                    child_display.display()
                )));
            }
            if before.nlink() != 1 {
                return Err(storage_error(&format!(
                    "{} has an unsafe hard-link alias",
                    child_display.display()
                )));
            }
            if secure_file(&access, &name.to_string_lossy(), false, true, false)?.is_none() {
                return Err(storage_error(&format!(
                    "{} disappeared while it was being protected",
                    child_display.display()
                )));
            }
        }
    }
    Ok(())
}

fn validate_directory_tree(directory: &File, display: &Path) -> Result<(), String> {
    verify_directory(directory, display)?;
    validate_directory_tree_entries(directory, display)
}

fn validate_directory_tree_entries(directory: &File, display: &Path) -> Result<(), String> {
    let access = descriptor_path(directory);
    let entries = fs::read_dir(&access).map_err(|error| {
        storage_io_error(&format!("could not scan {}", display.display()), error)
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| {
            storage_io_error(&format!("could not scan {}", display.display()), error)
        })?;
        let name = entry.file_name();
        let child_display = display.join(&name);
        let path = access.join(&name);
        let before = fs::symlink_metadata(&path).map_err(|error| {
            storage_io_error(
                &format!("could not inspect {}", child_display.display()),
                error,
            )
        })?;
        ensure_owned(&before, &child_display)?;
        if before.file_type().is_symlink() {
            return Err(storage_error(&format!(
                "{} is a symbolic link",
                child_display.display()
            )));
        }
        if before.is_dir() {
            let child = open_directory(&path, &child_display.to_string_lossy())?;
            let opened = child.metadata().map_err(|error| {
                storage_io_error(
                    &format!("could not verify {}", child_display.display()),
                    error,
                )
            })?;
            if !same_file(&before, &opened) {
                return Err(storage_error(&format!(
                    "{} changed while it was being opened",
                    child_display.display()
                )));
            }
            validate_directory_tree_entries(&child, &child_display)?;
        } else {
            let Some(_) = validate_file(&access, &name.to_string_lossy(), false)? else {
                return Err(storage_error(&format!(
                    "{} disappeared while it was being inspected",
                    child_display.display()
                )));
            };
        }
    }
    Ok(())
}

fn protect_directory(directory: &File, display: &Path) -> Result<(), String> {
    let before = directory.metadata().map_err(|error| {
        storage_io_error(&format!("could not inspect {}", display.display()), error)
    })?;
    ensure_owned(&before, display)?;
    if !before.is_dir() {
        return Err(storage_error(&format!(
            "{} is not a directory",
            display.display()
        )));
    }
    directory
        .set_permissions(fs::Permissions::from_mode(0o700))
        .map_err(|error| {
            storage_io_error(&format!("could not protect {}", display.display()), error)
        })?;
    let after = directory.metadata().map_err(|error| {
        storage_io_error(&format!("could not verify {}", display.display()), error)
    })?;
    if after.mode() & 0o777 != 0o700 {
        return Err(storage_error(&format!(
            "{} permissions could not be restricted to 0700",
            display.display()
        )));
    }
    Ok(())
}

fn verify_directory(directory: &File, display: &Path) -> Result<(), String> {
    let metadata = directory.metadata().map_err(|error| {
        storage_io_error(&format!("could not inspect {}", display.display()), error)
    })?;
    ensure_owned(&metadata, display)?;
    if !metadata.is_dir() {
        return Err(storage_error(&format!(
            "{} is not a directory",
            display.display()
        )));
    }
    if metadata.mode() & 0o777 != 0o700 {
        return Err(storage_error(&format!(
            "{} permissions are not 0700",
            display.display()
        )));
    }
    Ok(())
}

fn secure_file(
    parent: &Path,
    name: &str,
    create: bool,
    writable: bool,
    sqlite_sidecar: bool,
) -> Result<Option<File>, String> {
    secure_file_with_retries(
        parent,
        name,
        create,
        writable,
        sqlite_sidecar,
        SQLITE_SIDECAR_RETRIES,
    )
}

fn secure_file_with_retries(
    parent: &Path,
    name: &str,
    create: bool,
    writable: bool,
    sqlite_sidecar: bool,
    retries: u8,
) -> Result<Option<File>, String> {
    let path = parent.join(name);
    let before = match fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && !create => return Ok(None),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let mut options = OpenOptions::new();
            options
                .read(true)
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(O_NOFOLLOW);
            let file = match options.open(&path) {
                Ok(file) => file,
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    if retries == 0 {
                        return Err(storage_error(&format!(
                            "{name} changed repeatedly while it was being opened"
                        )));
                    }
                    return secure_file_with_retries(
                        parent,
                        name,
                        false,
                        writable,
                        sqlite_sidecar,
                        retries - 1,
                    );
                }
                Err(error) => {
                    return Err(storage_io_error(&format!("could not create {name}"), error));
                }
            };
            let _ = protect_file(&file, name, false)?;
            return Ok(Some(file));
        }
        Err(error) => {
            return Err(storage_io_error(
                &format!("could not inspect {name}"),
                error,
            ));
        }
    };
    if before.file_type().is_symlink() {
        return Err(storage_error(&format!("{name} is a symbolic link")));
    }
    if !before.is_file() {
        return Err(storage_error(&format!("{name} is not a regular file")));
    }
    ensure_owned(&before, Path::new(name))?;
    if before.nlink() != 1 {
        return Err(storage_error(&format!(
            "{name} has an unsafe hard-link alias"
        )));
    }
    let mut options = OpenOptions::new();
    options.read(true).write(writable).custom_flags(O_NOFOLLOW);
    let file = match options.open(&path) {
        Ok(file) => file,
        Err(error) if sqlite_sidecar && error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(None);
        }
        Err(error) => {
            return Err(storage_io_error(&format!("could not open {name}"), error));
        }
    };
    let opened = file
        .metadata()
        .map_err(|error| storage_io_error(&format!("could not verify {name}"), error))?;
    if !same_file(&before, &opened) {
        if sqlite_sidecar && retries > 0 {
            return secure_file_with_retries(
                parent,
                name,
                false,
                writable,
                sqlite_sidecar,
                retries - 1,
            );
        }
        return Err(storage_error(&format!(
            "{name} changed while it was being opened"
        )));
    }
    if !protect_file(&file, name, sqlite_sidecar)? {
        if retries == 0 {
            return Err(storage_error(&format!(
                "{name} changed repeatedly while it was being opened"
            )));
        }
        return secure_file_with_retries(
            parent,
            name,
            false,
            writable,
            sqlite_sidecar,
            retries - 1,
        );
    }
    Ok(Some(file))
}

fn validate_file(parent: &Path, name: &str, writable: bool) -> Result<Option<File>, String> {
    let path = parent.join(name);
    let before = match fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(storage_io_error(
                &format!("could not inspect {name}"),
                error,
            ));
        }
    };
    if before.file_type().is_symlink() {
        return Err(storage_error(&format!("{name} is a symbolic link")));
    }
    if !before.is_file() || before.nlink() != 1 {
        return Err(storage_error(&format!("{name} is not a safe regular file")));
    }
    ensure_owned(&before, Path::new(name))?;
    if before.mode() & 0o777 != 0o600 {
        return Err(storage_error(&format!("{name} permissions are not 0600")));
    }
    let mut options = OpenOptions::new();
    options.read(true).write(writable).custom_flags(O_NOFOLLOW);
    let file = options
        .open(&path)
        .map_err(|error| storage_io_error(&format!("could not open {name}"), error))?;
    let opened = file
        .metadata()
        .map_err(|error| storage_io_error(&format!("could not verify {name}"), error))?;
    if !same_file(&before, &opened) {
        return Err(storage_error(&format!(
            "{name} changed while it was being opened"
        )));
    }
    if !opened.is_file() || opened.nlink() != 1 {
        return Err(storage_error(&format!(
            "{name} changed to an unsafe file while it was being opened"
        )));
    }
    ensure_owned(&opened, Path::new(name))?;
    if opened.mode() & 0o777 != 0o600 {
        return Err(storage_error(&format!(
            "{name} permissions changed while it was being opened"
        )));
    }
    Ok(Some(file))
}

fn lock_file_for_inspection(file: &File, name: &str) -> Result<(), String> {
    let mut lock = libc::flock {
        l_type: libc::F_WRLCK as libc::c_short,
        l_whence: libc::SEEK_SET as libc::c_short,
        l_start: 0,
        l_len: 0,
        l_pid: 0,
    };
    // SAFETY: file is a live descriptor and lock points to a correctly
    // laid-out Linux struct flock for the duration of the call.
    if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_OFD_SETLK, &mut lock) } == 0 {
        return Ok(());
    }
    let error = std::io::Error::last_os_error();
    if matches!(error.raw_os_error(), Some(libc::EACCES | libc::EAGAIN)) {
        return Err(storage_error(
            &format!(
                "read-only inspection requires all other SQLite connections to close before retrying (active {name})"
            ),
        ));
    }
    Err(storage_io_error(
        &format!("could not lock the {name} for read-only inspection"),
        error,
    ))
}

fn protect_file(file: &File, name: &str, tolerate_unlinked: bool) -> Result<bool, String> {
    let before = file
        .metadata()
        .map_err(|error| storage_io_error(&format!("could not inspect {name}"), error))?;
    ensure_owned(&before, Path::new(name))?;
    if tolerate_unlinked && before.is_file() && before.nlink() == 0 {
        return Ok(false);
    }
    if !before.is_file() || before.nlink() != 1 {
        return Err(storage_error(&format!("{name} changed to an unsafe file")));
    }
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|error| storage_io_error(&format!("could not protect {name}"), error))?;
    let after = file
        .metadata()
        .map_err(|error| storage_io_error(&format!("could not verify {name}"), error))?;
    if tolerate_unlinked && after.is_file() && after.nlink() == 0 {
        return Ok(false);
    }
    if !after.is_file() || after.nlink() != 1 {
        return Err(storage_error(&format!("{name} changed to an unsafe file")));
    }
    if after.mode() & 0o777 != 0o600 {
        return Err(storage_error(&format!(
            "{name} permissions could not be restricted to 0600"
        )));
    }
    Ok(true)
}

fn open_directory(path: &Path, display: &str) -> Result<File, String> {
    let mut options = OpenOptions::new();
    options.read(true).custom_flags(O_DIRECTORY | O_NOFOLLOW);
    options
        .open(path)
        .map_err(|error| storage_io_error(&format!("could not open {display}"), error))
}

fn descriptor_path(file: &File) -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{}", file.as_raw_fd()))
}

fn ensure_owned(metadata: &fs::Metadata, path: &Path) -> Result<(), String> {
    if metadata.uid() != effective_uid() {
        return Err(storage_error(&format!(
            "{} is not owned by the current user",
            path.display()
        )));
    }
    Ok(())
}

fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.dev() == right.dev() && left.ino() == right.ino()
}

fn storage_error(message: &str) -> String {
    format!("Collection storage is unsafe: {message}")
}

fn storage_io_error(action: &str, error: std::io::Error) -> String {
    storage_error(&format!("{action}: {error}"))
}

fn effective_uid() -> u32 {
    unsafe extern "C" {
        fn geteuid() -> u32;
    }
    // SAFETY: geteuid takes no arguments and has no failure mode.
    unsafe { geteuid() }
}
