//! JSONL store: one file per namespace, one record per line, byte-compatible
//! with the Go, TypeScript and Python JSONL stores in either direction. The
//! namespace is not stored per line. `put` appends one line; `purge` rewrites
//! the file through a temporary file and an atomic rename. Single writer: no
//! multi-process locking.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use crate::error::{ArtifactError, Result};
use crate::record::{
    check_config, decode_record, encode_record, has_purged, prepare, purged, storage_checksum,
    valid_id, verify, Record,
};
use crate::store::Store;

/// (offset, length) of a line, excluding its `\n`.
type Span = (u64, usize);

/// A file-backed store. Opening reads an existing file once to index
/// `capsule_id` to line offsets (last occurrence wins); it does not create the
/// file, which [`Store::init`] does.
#[derive(Debug)]
pub struct JsonlStore {
    path: PathBuf,
    namespace: String,
    trusted: Vec<[u8; 32]>,
    index: Mutex<HashMap<String, Span>>,
}

struct Line {
    offset: usize,
    len: usize,
    id: Option<String>,
}

fn peek_id(line: &[u8]) -> Option<String> {
    #[derive(serde::Deserialize)]
    struct Peek {
        capsule_id: String,
    }
    serde_json::from_slice::<Peek>(line)
        .ok()
        .map(|p| p.capsule_id)
        .filter(|id| !id.is_empty())
}

/// Splits complete lines; a trailing partial line is not returned.
fn scan(data: &[u8]) -> Vec<Line> {
    let mut lines = Vec::new();
    let mut offset = 0;
    while let Some(end) = data[offset..].iter().position(|&b| b == b'\n') {
        let line = &data[offset..offset + end];
        let id = if line.is_empty() { None } else { peek_id(line) };
        lines.push(Line {
            offset,
            len: end,
            id,
        });
        offset += end + 1;
    }
    lines
}

fn index_of(lines: &[Line]) -> HashMap<String, Span> {
    lines
        .iter()
        .filter_map(|l| l.id.clone().map(|id| (id, (l.offset as u64, l.len))))
        .collect()
}

impl JsonlStore {
    /// Validates the configuration and indexes the file if it exists.
    pub fn new<K: AsRef<[u8]>>(
        path: impl AsRef<Path>,
        namespace: &str,
        trusted: impl IntoIterator<Item = K>,
    ) -> Result<Self> {
        let trusted = check_config(namespace, trusted)?;
        let path = path.as_ref().to_path_buf();
        let index = match fs::read(&path) {
            Ok(data) => index_of(&scan(&data)),
            Err(e) if e.kind() == ErrorKind::NotFound => HashMap::new(),
            Err(e) => return Err(ArtifactError::storage(e)),
        };
        Ok(JsonlStore {
            path,
            namespace: namespace.to_string(),
            trusted,
            index: Mutex::new(index),
        })
    }

    fn index(&self) -> std::sync::MutexGuard<'_, HashMap<String, Span>> {
        self.index
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn read_at(&self, id: &str, span: Span) -> Result<Record> {
        let mut file = File::open(&self.path).map_err(ArtifactError::storage)?;
        file.seek(SeekFrom::Start(span.0))
            .map_err(ArtifactError::storage)?;
        let mut buf = vec![0; span.1];
        file.read_exact(&mut buf)
            .map_err(|e| ArtifactError::Corrupt(format!("read failed: {e}")))?;
        let record = decode_record(&buf)?;
        if record.capsule_id != id {
            return Err(ArtifactError::Corrupt(
                "index points to a different capsule".into(),
            ));
        }
        Ok(record)
    }

    fn rewrite(
        &self,
        index: &mut HashMap<String, Span>,
        id: &str,
        replacement: &[u8],
    ) -> Result<()> {
        let data = fs::read(&self.path).map_err(ArtifactError::storage)?;
        let permissions = fs::metadata(&self.path)
            .map_err(ArtifactError::storage)?
            .permissions();
        let lines = scan(&data);
        if !lines.iter().any(|l| l.id.as_deref() == Some(id)) {
            return Err(ArtifactError::Corrupt(
                "target line vanished during rewrite".into(),
            ));
        }
        let mut out = Vec::with_capacity(data.len());
        let mut new_index = HashMap::new();
        for line in &lines {
            let bytes = if line.id.as_deref() == Some(id) {
                replacement
            } else {
                &data[line.offset..line.offset + line.len]
            };
            if let Some(line_id) = &line.id {
                new_index.insert(line_id.clone(), (out.len() as u64, bytes.len()));
            }
            out.extend_from_slice(bytes);
            out.push(b'\n');
        }
        // Keep a torn trailing line (no "\n") byte for byte, still unindexed.
        let tail = lines.last().map_or(0, |l| l.offset + l.len + 1);
        out.extend_from_slice(&data[tail..]);

        let dir = self
            .path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let mut tmp = tempfile_in(dir)?;
        let result = (|| {
            fs::set_permissions(&tmp.0, permissions)?;
            tmp.1.write_all(&out)?;
            tmp.1.sync_all()?;
            fs::rename(&tmp.0, &self.path)
        })();
        if let Err(e) = result {
            let _ = fs::remove_file(&tmp.0);
            return Err(ArtifactError::storage(e));
        }
        *index = new_index;
        Ok(())
    }
}

/// Creates a new, uniquely named file in `dir`.
fn tempfile_in(dir: &Path) -> Result<(PathBuf, File)> {
    for attempt in 0..100u32 {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.subsec_nanos())
            .unwrap_or_default();
        let path = dir.join(format!(
            ".jsonl-rewrite-{}-{nanos}-{attempt}",
            std::process::id()
        ));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        // Owner-only until the rewrite is complete; the source file's own
        // permissions are applied before any record is written.
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        match options.open(&path) {
            Ok(file) => return Ok((path, file)),
            Err(e) if e.kind() == ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(ArtifactError::storage(e)),
        }
    }
    Err(ArtifactError::storage(std::io::Error::new(
        ErrorKind::AlreadyExists,
        "no unique temporary file name",
    )))
}

impl Store for JsonlStore {
    fn namespace(&self) -> &str {
        &self.namespace
    }

    /// Creates an empty file if none exists; an existing file is untouched.
    fn init(&self) -> Result<()> {
        let _index = self.index();
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&self.path)
        {
            Ok(_) => Ok(()),
            Err(e) if e.kind() == ErrorKind::AlreadyExists => Ok(()),
            Err(e) => Err(ArtifactError::storage(e)),
        }
    }

    fn put(&self, record: &Record) -> Result<()> {
        let prepared = prepare(record)?;
        verify(&prepared, &self.trusted)?;
        let checksum = storage_checksum(&prepared);
        let mut index = self.index();
        if let Some(&span) = index.get(&prepared.capsule_id) {
            let existing = self.read_at(&prepared.capsule_id, span)?;
            verify(&existing, &self.trusted)?; // reads fail closed
            if storage_checksum(&existing) != checksum {
                return Err(ArtifactError::Conflict);
            }
            if has_purged(&existing) {
                return Err(ArtifactError::Purged);
            }
            return Ok(());
        }
        let record_line = encode_record(&prepared).into_bytes();
        // No create: an uninitialized store fails instead of creating its file.
        let mut file = OpenOptions::new()
            .read(true)
            .append(true)
            .open(&self.path)
            .map_err(ArtifactError::storage)?;
        let size = file.metadata().map_err(ArtifactError::storage)?.len();
        // Terminate a torn last line first, so the new record starts its own
        // line instead of merging into the damaged one.
        let mut line = Vec::with_capacity(record_line.len() + 2);
        if size > 0 {
            let mut last = [0u8; 1];
            file.seek(SeekFrom::Start(size - 1))
                .and_then(|_| file.read_exact(&mut last))
                .map_err(ArtifactError::storage)?;
            if last[0] != b'\n' {
                line.push(b'\n');
            }
        }
        let offset = size + line.len() as u64;
        line.extend_from_slice(&record_line);
        line.push(b'\n');
        file.write_all(&line).map_err(ArtifactError::storage)?;
        index.insert(prepared.capsule_id, (offset, record_line.len()));
        Ok(())
    }

    fn get(&self, id: &str) -> Result<Record> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let record = {
            let index = self.index();
            let span = *index.get(id).ok_or(ArtifactError::NotFound)?;
            self.read_at(id, span)?
        };
        verify(&record, &self.trusted)?;
        Ok(record)
    }

    /// Rewrites the record's line with present originals tombstoned. Requires
    /// only the record's existence, not its verification.
    fn purge(&self, id: &str) -> Result<()> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let mut index = self.index();
        let span = *index.get(id).ok_or(ArtifactError::NotFound)?;
        let replacement = encode_record(&purged(&self.read_at(id, span)?));
        self.rewrite(&mut index, id, replacement.as_bytes())
    }
}
