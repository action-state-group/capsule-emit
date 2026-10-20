//! capsule-emit's legacy ledger (`capsules.jsonl` + `signed-statements/`) as
//! an artifact [`Store`] and a CLL [`EntryStore`](cll::log::EntryStore), with
//! the capability limits in [`CAPABILITIES`]. The mapping is specified in
//! `docs/legacy-ledger.md`; legacy ledgers keep their format and are exempt
//! from cross-language storage interop.
//!
//! **Entry log.** Every line is a leaf, padding included, at its existing
//! position. `Entry.value` is the line's `capsule_id`; `appended_at` is the
//! line's `timestamp`, else the previous entry's (the Unix epoch for the first
//! line). `scan_entries` keeps every line; `get_entry` returns the first
//! occurrence of a digest. `append` lacks the `digest-append` capability: it
//! returns a held digest's entry as `idempotent` and is `unsupported`
//! otherwise.
//!
//! **Artifact store.** A capsule is written by [`Store::put`], which appends
//! its line and therefore its log entry. `get` returns the line without the
//! inline `signature`/`key_id` as compact JSON, the envelope (empty for a
//! capsule sealed before producer envelopes), and one attachment,
//! [`STATEMENT`]: the line's `signed-statements/<capsule_id>.cose`, a
//! COSE_Sign1 over the whole line, `present` with no binding. `put` must supply
//! it, because the ledger checks it on every reload, and refuses any other
//! original as `unsupported`. `purge` is `unsupported`. A padding record
//! (`record_type: "padding"`) is a record too: `put` appends it as a padding
//! line, outside the chain, and `get` returns it like a capsule.
//! [`LegacyLedger::append_padding`] and [`LegacyLedger::pad_to_bucket`] write
//! padding lines directly.
//!
//! The ledger has one writer: this handle. [`Store::init`] opens (creating if
//! needed) the ledger directory, which also runs the ledger's torn-write
//! recovery; the constructor writes nothing.

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use capsule_emit::ledger::{check_statement_matches, Ledger, LedgerError};
use cll::log::{
    AppendInput, AppendOutcome, AppendResult, Entry, EntrySource, EntryStore, Error as LogError,
    ErrorCode, Timestamp, ENTRY_BYTES, MAX_PORTABLE_INTEGER, MAX_SCAN_LIMIT,
};
use serde_json::{Map, Value};

use crate::error::{ArtifactError, Result};
use crate::record::{
    check_config, decode_strict_json, is_padding, prepare, sha256_hex, valid_id, verify, Artifact,
    Record, RetentionState,
};
use crate::store::Store;

/// The name of the required, unbound `signed-statements/<capsule_id>.cose`
/// attachment.
pub const STATEMENT: &str = "signed-statement";

/// What the legacy backend offers, by capability name.
pub const CAPABILITIES: &[(&str, bool)] = &[
    ("digest-append", false),
    ("originals", false),
    ("purge", false),
    ("transactions", false),
    ("multiple-writers", false),
];

const ENVELOPE_MEMBERS: [&str; 2] = ["signature", "key_id"];

struct State {
    ledger: Option<Ledger>,
    closed: bool,
    /// One entry per ledger line, derived incrementally.
    entries: Vec<Entry>,
    /// First occurrence of each value, as an index into `entries`.
    first: HashMap<Vec<u8>, usize>,
}

/// capsule-emit's ledger directory behind the artifact [`Store`] and the CLL
/// [`EntryStore`](cll::log::EntryStore). Safe to share across threads.
pub struct LegacyLedger {
    dir: PathBuf,
    namespace: String,
    trusted: Vec<[u8; 32]>,
    state: Mutex<State>,
}

fn corrupt(error: LedgerError) -> ArtifactError {
    match error {
        LedgerError::Io(e) => ArtifactError::storage(e),
        other => ArtifactError::Corrupt(other.to_string()),
    }
}

fn log_error(error: ArtifactError) -> LogError {
    match error {
        ArtifactError::Corrupt(detail) => LogError::new(ErrorCode::Corrupt, detail),
        other => LogError::new(ErrorCode::Storage, other.to_string()),
    }
}

fn invalid(message: &str) -> LogError {
    LogError::new(ErrorCode::Invalid, message)
}

/// The millisecond instant of an RFC 3339 time (no offset means UTC).
fn parse_time(text: &str) -> Option<Timestamp> {
    use chrono::{DateTime, NaiveDateTime};
    let normalized = text.trim().replacen(['t', ' '], "T", 1).replace('z', "Z");
    let millis = match DateTime::parse_from_rfc3339(&normalized) {
        Ok(time) => time.timestamp_millis(),
        Err(_) => NaiveDateTime::parse_from_str(&normalized, "%Y-%m-%dT%H:%M:%S%.f")
            .ok()?
            .and_utc()
            .timestamp_millis(),
    };
    Some(Timestamp::from_unix_millis(millis))
}

impl LegacyLedger {
    /// A store over the ledger directory `dir`. Validates the configuration
    /// and writes nothing; call [`Store::init`] before use.
    pub fn new<K: AsRef<[u8]>>(
        dir: impl AsRef<Path>,
        namespace: &str,
        trusted: impl IntoIterator<Item = K>,
    ) -> Result<Self> {
        Ok(Self {
            trusted: check_config(namespace, trusted)?,
            dir: dir.as_ref().to_path_buf(),
            namespace: namespace.to_string(),
            state: Mutex::new(State {
                ledger: None,
                closed: false,
                entries: Vec::new(),
                first: HashMap::new(),
            }),
        })
    }

    /// Opens the ledger on first use and indexes lines not yet indexed.
    /// The caller holds the state lock.
    fn open_locked(&self, state: &mut State) -> Result<()> {
        if state.ledger.is_none() {
            if !self.dir.join("capsules.jsonl").exists() {
                return Err(ArtifactError::storage(std::io::Error::other(
                    "the ledger is not initialized",
                )));
            }
            let (ledger, _) = Ledger::open(&self.dir).map_err(corrupt)?;
            state.ledger = Some(ledger);
        }
        sync(state)
    }

    /// Runs `f` on the open ledger with its entries current.
    fn with<T>(&self, f: impl FnOnce(&mut State) -> Result<T>) -> Result<T> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.closed {
            return Err(ArtifactError::storage(std::io::Error::other(
                "the store is closed",
            )));
        }
        self.open_locked(&mut state)?;
        f(&mut state)
    }

    /// A CLL operation, in the contract's order under one lock: `closed`,
    /// then the arguments (`validate`), then durable state. An uninitialized
    /// ledger has no entries.
    fn with_log<T>(
        &self,
        validate: impl FnOnce() -> cll::log::Result<()>,
        f: impl FnOnce(&State) -> cll::log::Result<T>,
    ) -> cll::log::Result<T> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.closed {
            return Err(LogError::new(ErrorCode::Closed, "backend is closed"));
        }
        validate()?;
        if state.ledger.is_some() || self.dir.join("capsules.jsonl").exists() {
            self.open_locked(&mut state).map_err(log_error)?;
        }
        f(&state)
    }

    /// The ledger directory.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Appends one padding record and its signed statement
    /// ([`Ledger::append_padding`]).
    pub fn append_padding(&self, record: &Value, statement: &[u8]) -> Result<()> {
        // Check what the ledger's reload check will, before writing, so a
        // rejected record leaves the ledger reopenable.
        let capsule_id = record
            .get("capsule_id")
            .and_then(Value::as_str)
            .filter(|id| valid_id(id))
            .ok_or_else(|| ArtifactError::Invalid("capsule id".into()))?;
        let computed = capsule_emit::jcs::compute_capsule_id(record)
            .map_err(|e| ArtifactError::Invalid(format!("capsule id: {e}")))?;
        if computed != capsule_id {
            return Err(ArtifactError::Invalid("capsule id mismatch".into()));
        }
        check_statement_matches(record, capsule_id, statement)
            .map_err(|e| ArtifactError::Invalid(format!("signed statement: {e}")))?;
        self.with(|state| {
            if state
                .first
                .contains_key(&hex::decode(capsule_id).expect("valid_id checked"))
            {
                return Err(ArtifactError::Conflict);
            }
            ledger(state)
                .append_padding(record, statement)
                .map_err(|e| match e {
                    LedgerError::Io(e) => ArtifactError::storage(e),
                    other => ArtifactError::Invalid(other.to_string()),
                })?;
            sync(state)
        })
    }

    /// Pads the ledger to a multiple of `bucket` lines ([`Ledger::pad_to_bucket`])
    /// and returns the line count.
    pub fn pad_to_bucket(
        &self,
        bucket: u64,
        signing_key: &ed25519_dalek::SigningKey,
        issuer: &str,
    ) -> Result<u64> {
        self.with(|state| {
            ledger(state)
                .pad_to_bucket(bucket, signing_key, issuer)
                .map_err(corrupt)
        })
    }

    /// Closes the store; every later operation fails.
    pub fn close(&self) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.closed = true;
        state.ledger = None;
    }

    fn raw_line(&self, offset: u64) -> Result<Vec<u8>> {
        let mut file =
            File::open(self.dir.join("capsules.jsonl")).map_err(ArtifactError::storage)?;
        file.seek(SeekFrom::Start(offset))
            .map_err(ArtifactError::storage)?;
        let mut line = Vec::new();
        BufReader::new(file)
            .read_until(b'\n', &mut line)
            .map_err(ArtifactError::storage)?;
        while line.last() == Some(&b'\n') {
            line.pop();
        }
        Ok(line)
    }

    /// The verified record a capsule line holds.
    fn record(&self, ledger: &Ledger, capsule_id: &str, offset: u64) -> Result<Record> {
        let mut line = decode_strict_json(&self.raw_line(offset)?)
            .map_err(|e| ArtifactError::Invalid(format!("capsule verification: {e}")))?;
        let statement = ledger.read_statement(capsule_id).map_err(corrupt)?;
        check_statement_matches(&line, capsule_id, &statement)
            .map_err(|e| ArtifactError::Invalid(format!("signed statement: {e}")))?;
        let object = line
            .as_object_mut()
            .ok_or_else(|| ArtifactError::Invalid("capsule is not an object".into()))?;
        let claimed = ENVELOPE_MEMBERS
            .iter()
            .filter(|m| object.contains_key(**m))
            .count();
        let signature = object.remove("signature");
        let key_id = object.remove("key_id");
        let envelope = match claimed {
            0 => Vec::new(),
            2 => {
                let envelope = signature
                    .as_ref()
                    .and_then(Value::as_str)
                    .and_then(|s| hex::decode(s).ok())
                    .filter(|e| !e.is_empty())
                    .ok_or_else(|| ArtifactError::Invalid("envelope: signature".into()))?;
                key_id
                    .as_ref()
                    .and_then(Value::as_str)
                    .ok_or_else(|| ArtifactError::Invalid("envelope: key_id".into()))?;
                envelope
            }
            _ => {
                return Err(ArtifactError::Invalid(
                    "envelope: only one of signature and key_id is present".into(),
                ))
            }
        };
        let capsule = serde_json::to_vec(&line).map_err(ArtifactError::storage)?;
        let record = Record {
            capsule_id: capsule_id.to_string(),
            capsule,
            producer_envelope: envelope,
            artifacts: Vec::new(),
        };
        let signer = self.check(&record)?;
        if let Some(signer) = signer {
            let key_id = key_id.as_ref().and_then(Value::as_str).unwrap_or_default();
            if !key_id.eq_ignore_ascii_case(&hex::encode(signer)) {
                return Err(ArtifactError::Invalid(
                    "envelope: key_id is not the envelope's signer".into(),
                ));
            }
        }
        Ok(Record {
            artifacts: vec![statement_artifact(statement)],
            ..record
        })
    }

    /// The legacy check of a record without originals. Returns the envelope
    /// signer, or `None` for a capsule sealed before producer envelopes.
    fn check(&self, record: &Record) -> Result<Option<[u8; 32]>> {
        if record.producer_envelope.is_empty() {
            let capsule = decode_strict_json(&record.capsule)
                .map_err(|e| ArtifactError::Invalid(format!("capsule verification: {e}")))?;
            let class1 = agent_action_capsule::verify::verify(&capsule, None);
            if !class1.ok || class1.capsule_id.as_deref() != Some(record.capsule_id.as_str()) {
                return Err(ArtifactError::Invalid("capsule verification".into()));
            }
            return Ok(None);
        }
        verify(record, &self.trusted)?;
        let envelope =
            agent_action_capsule::envelope::verify(&record.capsule_id, &record.producer_envelope);
        let signer = <[u8; 32]>::try_from(&envelope.public_key[..])
            .map_err(|_| ArtifactError::Invalid("envelope: public key".into()))?;
        Ok(Some(signer))
    }
}

fn ledger(state: &mut State) -> &mut Ledger {
    state.ledger.as_mut().expect("with() opened the ledger")
}

fn statement_artifact(statement: Vec<u8>) -> Artifact {
    Artifact {
        name: STATEMENT.to_string(),
        binding: String::new(),
        content_sha256: sha256_hex(&statement),
        content: Some(statement),
        state: RetentionState::Present,
    }
}

/// Derives entries for ledger lines not yet indexed.
fn sync(state: &mut State) -> Result<()> {
    let ledger = state.ledger.as_ref().expect("with() opened the ledger");
    let lines = ledger.lines();
    for line in &lines[state.entries.len()..] {
        let value = hex::decode(&line.capsule_id)
            .ok()
            .filter(|v| v.len() == ENTRY_BYTES)
            .ok_or_else(|| ArtifactError::Corrupt("capsule_id is not a 32-byte digest".into()))?;
        let previous = state
            .entries
            .last()
            .map_or(Timestamp::from_unix_millis(0), |e| e.appended_at);
        let appended_at = line
            .timestamp
            .as_deref()
            .and_then(parse_time)
            .filter(|t| t.is_portable())
            .unwrap_or(previous);
        let seq = state.entries.len() as u64 + 1;
        state
            .first
            .entry(value.clone())
            .or_insert(state.entries.len());
        state.entries.push(Entry {
            seq,
            value,
            appended_at,
        });
    }
    Ok(())
}

impl Store for LegacyLedger {
    fn namespace(&self) -> &str {
        &self.namespace
    }

    fn init(&self) -> Result<()> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.closed {
            return Err(ArtifactError::storage(std::io::Error::other(
                "the store is closed",
            )));
        }
        if state.ledger.is_none() {
            let (ledger, _) = Ledger::open(&self.dir).map_err(corrupt)?;
            state.ledger = Some(ledger);
        }
        Ok(())
    }

    fn put(&self, record: &Record) -> Result<()> {
        let prepared = prepare(record)?;
        if !valid_id(&prepared.capsule_id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let capsule = decode_strict_json(&prepared.capsule)
            .map_err(|e| ArtifactError::Invalid(format!("capsule verification: {e}")))?;
        let Value::Object(object) = &capsule else {
            return Err(ArtifactError::Invalid("capsule is not an object".into()));
        };
        if ENVELOPE_MEMBERS.iter().any(|m| object.contains_key(*m)) {
            return Err(ArtifactError::Invalid(
                "signature and key_id are not capsule members".into(),
            ));
        }
        if serde_json::to_vec(&capsule).map_err(ArtifactError::storage)? != prepared.capsule {
            return Err(ArtifactError::Invalid(
                "capsule bytes are not in the ledger's line form".into(),
            ));
        }
        let bare = Record {
            artifacts: Vec::new(),
            ..prepared.clone()
        };
        let signer = self.check(&bare)?;
        let mut statement = None;
        for a in &prepared.artifacts {
            let is_statement = a.name == STATEMENT
                && a.binding.is_empty()
                && a.state == RetentionState::Present
                && a.content.is_some();
            if statement.is_some() && a.name == STATEMENT {
                return Err(ArtifactError::Invalid(format!(
                    "duplicate artifact {STATEMENT}"
                )));
            }
            if !is_statement {
                return Err(ArtifactError::Unsupported(
                    "the legacy ledger keeps no originals besides its signed statement".into(),
                ));
            }
            statement = a.content.clone();
        }
        let statement = statement.ok_or_else(|| {
            ArtifactError::Invalid(format!("the {STATEMENT} attachment is required"))
        })?;
        let mut line: Map<String, Value> = object.clone();
        if let Some(signer) = signer {
            line.insert(
                "signature".into(),
                Value::String(hex::encode(&prepared.producer_envelope)),
            );
            line.insert("key_id".into(), Value::String(hex::encode(signer)));
        }
        let line = Value::Object(line);
        let padding = is_padding(&line);
        check_statement_matches(&line, &prepared.capsule_id, &statement)
            .map_err(|e| ArtifactError::Invalid(format!("signed statement: {e}")))?;
        let text = serde_json::to_vec(&line).map_err(ArtifactError::storage)?;
        self.with(|state| {
            let value = hex::decode(&prepared.capsule_id).expect("valid_id checked");
            if let Some(&index) = state.first.get(&value) {
                let existing = &ledger(state).lines()[index];
                let same = existing.padding == padding
                    && self.raw_line(existing.offset)? == text
                    && ledger(state)
                        .read_statement(&prepared.capsule_id)
                        .map_err(corrupt)?
                        == statement;
                return if same {
                    Ok(())
                } else {
                    Err(ArtifactError::Conflict)
                };
            }
            let appended = if padding {
                ledger(state).append_padding(&line, &statement)
            } else {
                ledger(state).append(&line, &statement)
            };
            appended.map_err(|e| match e {
                LedgerError::Io(e) => ArtifactError::storage(e),
                other => ArtifactError::Invalid(other.to_string()),
            })?;
            sync(state)
        })
    }

    fn get(&self, id: &str) -> Result<Record> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        if !self.dir.join("capsules.jsonl").exists() {
            return Err(ArtifactError::NotFound);
        }
        self.with(|state| {
            let value = hex::decode(id).expect("valid_id checked");
            let index = *state.first.get(&value).ok_or(ArtifactError::NotFound)?;
            let line = ledger(state).lines()[index].clone();
            self.record(ledger(state), id, line.offset)
        })
    }

    fn purge(&self, id: &str) -> Result<()> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        Err(ArtifactError::Unsupported(
            "the signed statement is required by the ledger and cannot be purged".into(),
        ))
    }
}

impl EntrySource for LegacyLedger {
    fn scan_entries(&self, after_seq: u64, limit: usize) -> cll::log::Result<Vec<Entry>> {
        self.with_log(
            || {
                if after_seq > MAX_PORTABLE_INTEGER || limit == 0 || limit > MAX_SCAN_LIMIT {
                    return Err(invalid("scan arguments are outside the contract's limits"));
                }
                Ok(())
            },
            |state| {
                let start = usize::try_from(after_seq)
                    .unwrap_or(usize::MAX)
                    .min(state.entries.len());
                let end = start.saturating_add(limit).min(state.entries.len());
                Ok(state.entries[start..end].to_vec())
            },
        )
    }
}

impl EntryStore for LegacyLedger {
    fn append(&self, input: &AppendInput) -> cll::log::Result<AppendResult> {
        self.with_log(
            || {
                if input.value.len() != ENTRY_BYTES || !input.appended_at.is_portable() {
                    return Err(invalid(
                        "append input is not a 32-byte value and a portable time",
                    ));
                }
                Ok(())
            },
            |state| match state.first.get(&input.value) {
                Some(&index) => Ok(AppendResult {
                    entry: state.entries[index].clone(),
                    outcome: AppendOutcome::Idempotent,
                }),
                None => Err(LogError::new(
                    ErrorCode::Unsupported,
                    "the legacy ledger cannot append a bare digest; write the capsule with put",
                )),
            },
        )
    }

    fn get_entry(&self, value: &[u8]) -> cll::log::Result<Entry> {
        self.with_log(
            || {
                if value.len() != ENTRY_BYTES {
                    return Err(invalid("entry value is not 32 bytes"));
                }
                Ok(())
            },
            |state| {
                state
                    .first
                    .get(value)
                    .map(|&index| state.entries[index].clone())
                    .ok_or_else(|| LogError::new(ErrorCode::NotFound, "entry not found"))
            },
        )
    }
}

impl cll::log::conformance::Target for LegacyLedger {
    fn entry_store(&self) -> Option<&dyn EntryStore> {
        Some(self)
    }
    fn supports_close(&self) -> bool {
        true
    }
    fn close(&self) -> cll::log::Result<()> {
        LegacyLedger::close(self);
        Ok(())
    }
}
