//! JSONL store: one file per namespace, one record per line, byte-compatible
//! with the Go, TypeScript and Python JSONL stores in either direction. The
//! namespace is not stored per line. `put` appends one line; `purge` rewrites
//! the file through a temporary file and an atomic rename. Single writer: no
//! multi-process locking.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use crate::error::{ArtifactError, Result};
use crate::index::{normalize_paths, IndexStore, MemoryIndex, Page, Query};
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
    state: Mutex<State>,
}

/// What a [`JsonlStore`] keeps in memory, under one lock.
#[derive(Debug, Default)]
struct State {
    /// `capsule_id` to its line.
    spans: HashMap<String, Span>,
    /// Capsule IDs by first appearance in the file, which is admission
    /// order; purge rewrites lines in place and keeps it.
    order: Vec<String>,
    /// The index extension's index. Its declaration lives only in this store:
    /// a JSONL file stores records and nothing else.
    lookup: MemoryIndex,
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

/// One line from a streaming scan: a complete line (without its `\n`) with
/// its offset, or a torn last line (no `\n`), which is never indexed.
enum Scanned {
    Line {
        offset: u64,
        bytes: Vec<u8>,
        id: Option<String>,
    },
    Torn(Vec<u8>),
}

/// Streams `reader` line by line, holding one line in memory at a time.
fn scan_lines(
    reader: impl Read,
    mut each: impl FnMut(Scanned) -> std::io::Result<()>,
) -> std::io::Result<()> {
    let mut reader = BufReader::new(reader);
    let mut offset = 0u64;
    let mut buf = Vec::new();
    loop {
        buf.clear();
        let n = reader.read_until(b'\n', &mut buf)?;
        if n == 0 {
            return Ok(());
        }
        if buf.last() != Some(&b'\n') {
            return each(Scanned::Torn(std::mem::take(&mut buf)));
        }
        buf.pop();
        let id = if buf.is_empty() { None } else { peek_id(&buf) };
        each(Scanned::Line {
            offset,
            bytes: std::mem::take(&mut buf),
            id,
        })?;
        offset += n as u64;
    }
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
        let mut state = State::default();
        match File::open(&path) {
            Ok(file) => scan_lines(file, |line| {
                if let Scanned::Line {
                    offset,
                    bytes,
                    id: Some(id),
                } = line
                {
                    if !state.spans.contains_key(&id) {
                        state.order.push(id.clone());
                    }
                    state.spans.insert(id, (offset, bytes.len()));
                }
                Ok(())
            })
            .map_err(ArtifactError::storage)?,
            Err(e) if e.kind() == ErrorKind::NotFound => {}
            Err(e) => return Err(ArtifactError::storage(e)),
        }
        Ok(JsonlStore {
            path,
            namespace: namespace.to_string(),
            trusted,
            state: Mutex::new(state),
        })
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Creates an empty file if none exists. The caller holds the state lock.
    fn create(&self) -> Result<()> {
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

    /// Adds every readable record to a freshly declared index, in file order.
    /// A line that no longer decodes as a record is left out; `get` reports it.
    fn index_all(&self, state: &mut State) -> Result<()> {
        for id in &state.order {
            let capsule = match self.read_at(id, state.spans[id]) {
                Ok(record) => record.capsule,
                Err(ArtifactError::Corrupt(_) | ArtifactError::Invalid(_)) => continue,
                Err(e) => return Err(e),
            };
            match state.lookup.add(id, &capsule) {
                Ok(()) | Err(ArtifactError::Corrupt(_)) => {}
                Err(e) => return Err(e),
            }
        }
        Ok(())
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
        let source = File::open(&self.path).map_err(ArtifactError::storage)?;
        let permissions = source
            .metadata()
            .map_err(ArtifactError::storage)?
            .permissions();
        let dir = self
            .path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let (tmp_path, tmp) = tempfile_in(dir)?;
        let mut new_index = HashMap::new();
        let mut found = false;
        let result = (|| {
            fs::set_permissions(&tmp_path, permissions)?;
            let mut out = BufWriter::new(tmp);
            let mut position = 0u64;
            scan_lines(source, |line| match line {
                // Keep a torn last line byte for byte, still unindexed.
                Scanned::Torn(bytes) => out.write_all(&bytes),
                Scanned::Line {
                    bytes, id: line_id, ..
                } => {
                    let bytes = if line_id.as_deref() == Some(id) {
                        found = true;
                        replacement.to_vec()
                    } else {
                        bytes
                    };
                    if let Some(line_id) = line_id {
                        new_index.insert(line_id, (position, bytes.len()));
                    }
                    out.write_all(&bytes)?;
                    out.write_all(b"\n")?;
                    position += bytes.len() as u64 + 1;
                    Ok(())
                }
            })?;
            let file = out.into_inner().map_err(|e| e.into_error())?;
            file.sync_all()
        })();
        let outcome = match result {
            Err(e) => Err(ArtifactError::storage(e)),
            Ok(()) if !found => Err(ArtifactError::Corrupt(
                "target line vanished during rewrite".into(),
            )),
            Ok(()) => fs::rename(&tmp_path, &self.path).map_err(ArtifactError::storage),
        };
        if outcome.is_err() {
            let _ = fs::remove_file(&tmp_path);
            return outcome;
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
        let _state = self.state();
        self.create()
    }

    fn put(&self, record: &Record) -> Result<()> {
        let prepared = prepare(record)?;
        verify(&prepared, &self.trusted)?;
        let checksum = storage_checksum(&prepared);
        let mut state = self.state();
        if let Some(&span) = state.spans.get(&prepared.capsule_id) {
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
        let state = &mut *state;
        state
            .spans
            .insert(prepared.capsule_id.clone(), (offset, record_line.len()));
        state.order.push(prepared.capsule_id.clone());
        state.lookup.add(&prepared.capsule_id, &prepared.capsule)
    }

    fn get(&self, id: &str) -> Result<Record> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let record = {
            let state = self.state();
            let span = *state.spans.get(id).ok_or(ArtifactError::NotFound)?;
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
        let mut state = self.state();
        let span = *state.spans.get(id).ok_or(ArtifactError::NotFound)?;
        // A line that no longer decodes as a record cannot be rewritten: that
        // is corrupt here even when get would call its state invalid.
        let stored = self.read_at(id, span).map_err(|e| match e {
            ArtifactError::Invalid(why) => ArtifactError::Corrupt(why),
            other => other,
        })?;
        let replacement = encode_record(&purged(&stored));
        self.rewrite(&mut state.spans, id, replacement.as_bytes())
    }

    fn as_index_store(&self) -> Option<&dyn IndexStore> {
        Some(self)
    }
}

/// The declaration and the index live in this store only: a store opened
/// again over the same file has none until it declares again.
impl IndexStore for JsonlStore {
    /// Creates the file as `init` does, then declares paths for this store
    /// and indexes the records in the file, in file order.
    fn init_index(&self, paths: &[&str]) -> Result<()> {
        let mut state = self.state();
        self.create()?;
        if state.lookup.declare(paths)? {
            self.index_all(&mut state)?;
        }
        Ok(())
    }

    /// Replaces the declared paths and rebuilds the index from the file.
    fn reindex(&self, paths: &[&str]) -> Result<()> {
        let normalized = normalize_paths(paths)?;
        let mut state = self.state();
        self.create()?;
        state.lookup.reset(normalized);
        self.index_all(&mut state)
    }

    fn find(&self, query: &Query) -> Result<Page> {
        self.state().lookup.find(query)
    }
}
