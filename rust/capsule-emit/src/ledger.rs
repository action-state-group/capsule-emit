//! Durable local ledger: `<ledger_dir>/capsules.jsonl` (one JSON capsule per
//! line) + `<ledger_dir>/signed-statements/<capsule_id>.cose` (the raw
//! COSE_Sign1 bytes).
//!
//! Three properties this module guarantees:
//!
//! 1. **capsule_id links** -- `append()` refuses to write a capsule whose
//!    `chain.parent_capsule_id` doesn't match the ledger's current head.
//! 2. **restart preserves a valid chain head** -- `open()` replays the whole
//!    `capsules.jsonl` on startup and recovers `chain_head` from it; no
//!    separate head-pointer file to fall out of sync.
//! 3. **a partial write doesn't create a silently-accepted chain** -- a torn
//!    write (crash mid-`write()`, so the final line has no trailing `\n`) is
//!    detected and the incomplete line is truncated away during recovery,
//!    never indexed, never trusted as the head. A *terminated* line that
//!    fails to parse, or whose stored `capsule_id` doesn't match its
//!    recomputed digest, or whose chain linkage doesn't match its
//!    predecessor, is a hard error instead of a silent drop: that is real
//!    corruption, not an artifact of a torn write.
//!
//! **Padding records** ([`crate::padding`], Evidence Layer -00 §12.1) are
//! ledger lines too -- the checkpoint MMR folds every line -- but they sit
//! OUTSIDE the chain: no `chain` block, and the chain head never advances
//! over one, so no record ever links to a padding record. They are appended
//! only through [`Ledger::append_padding`] / [`Ledger::pad_to_bucket`], are
//! not indexed for lookup, and are excluded from `known_capsule_ids`.
//!
//! **Storage.** [`Ledger::open`] keeps the flat layout above. [`Ledger::open_stores`]
//! keeps the same ledger in two standard layers instead: a CLL entry log
//! ([`cll::log::EntryStore`]: jsonl, memory, sqlite or mysql) holding each
//! line's 32-byte `capsule_id` in order, and an artifact store
//! ([`capsule_emit_artifact::Store`]) holding each line as a record: the line
//! without its inline `signature`/`key_id`, the producer envelope, and the
//! signed statement as the unbound attachment
//! [`STATEMENT`](crate::legacy_ledger::STATEMENT) (the mapping
//! [`crate::legacy_ledger`] uses). Every method works on both. On the store
//! layers the guarantees above hold as follows:
//!
//! - Chain linkage on append, and linkage, `capsule_id` recompute, padding
//!   shape and statement on open: `Ledger` checks them, as on the flat layout.
//!   Before writing it also runs the checks open would (the `capsule_id`
//!   recompute, the envelope's signer named by `key_id`, the statement), since
//!   the store checks an attachment's checksum but not its COSE meaning.
//! - A torn tail: the backends. CLL's `JsonlStore::init` truncates an
//!   incomplete last line and the artifact JSONL store never indexes one;
//!   SQLite and MySQL write each operation in one transaction. The caller
//!   provisions the stores before `open_stores`, so it logs any repair there:
//!   [`RecoveryReport::truncated_torn_write_at`] is always `None` here.
//! - A crash between the two writes: the record is `put` first and the log
//!   entry appended second, so the entry is the commit point. A crash leaves a
//!   record no entry names, and the retry's identical `put` is accepted.
//! - Real corruption: an entry without a record ([`LedgerError::MissingRecord`]),
//!   a record the store refuses ([`LedgerError::Store`]), a broken chain or a
//!   bad statement fails `open_stores`.
//!
//! The store admits only records with a producer envelope from a key it
//! trusts, and padding records. Never purge a ledger record: purge erases the
//! signed statement, and `open_stores` then refuses the log. One `Ledger`
//! writes a log: CLL `append` does not compare parents, so two writers could
//! each extend the same head. [`LedgerLine::offset`] is the log `seq` here,
//! where the flat layout has a byte offset.
//!
//! **Indexes.** A [`RecordIndexer`] derives keys from every chained record,
//! so a caller can ask "does this ledger already hold a record of X?" (a
//! dedup gate) without rescanning. The default indexer covers the two
//! registered cross-party citation purposes.

use crate::jcs::{compute_capsule_id, decode_record};
use crate::padding::{check_padding_shape, is_padding};
use capsule_emit_artifact::{Artifact, ArtifactError, Record, RetentionState, Store};
use cll::log::{AppendInput, AppendOutcome, EntryStore, ErrorCode, Timestamp, MAX_SCAN_LIMIT};
use serde_json::Value;
use sha2::Digest;
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

#[derive(Debug, thiserror::Error)]
pub enum LedgerError {
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
    /// A ledger line or statement payload is not JSON a record can be
    /// (see [`crate::jcs::decode_record`]).
    #[error("record JSON error: {0}")]
    Decode(#[from] crate::jcs::DecodeError),
    #[error("canonicalization error recomputing capsule_id: {0}")]
    Jcs(#[from] crate::jcs::JcsError),
    #[error(
        "ledger line {line} corrupt: stored capsule_id {stored:?} does not match recomputed digest {recomputed:?} -- refusing to trust this entry"
    )]
    CapsuleIdMismatch {
        line: usize,
        stored: String,
        recomputed: String,
    },
    #[error("ledger line {line} corrupt: missing or non-string capsule_id field")]
    MissingCapsuleId { line: usize },
    #[error("ledger line {line} references chain parent {parent:?} but the ledger head at that point was {head:?} -- chain is broken, not appending silently")]
    ChainBroken {
        line: usize,
        parent: Option<String>,
        head: Option<String>,
    },
    #[error(
        "ledger line {line}: signed statement file missing for capsule_id {capsule_id} ({path})"
    )]
    MissingStatement {
        line: usize,
        capsule_id: String,
        path: String,
    },
    #[error(
        "ledger line {line} corrupt: signed statement for capsule_id {capsule_id} does not check \
         out on reload ({detail}) -- refusing to trust this entry"
    )]
    StatementInvalid {
        line: usize,
        capsule_id: String,
        detail: String,
    },
    #[error("refusing to append: capsule's chain.parent_capsule_id {parent:?} does not match ledger head {head:?}")]
    AppendChainMismatch {
        parent: Option<String>,
        head: Option<String>,
    },
    #[error("capsule has no capsule_id field")]
    NoCapsuleId,
    #[error("ledger line {line}: malformed padding record: {detail}")]
    MalformedPadding { line: usize, detail: String },
    #[error("refusing to append: {0}")]
    PaddingMisrouted(&'static str),
    /// A log entry whose record the artifact store does not hold.
    #[error("ledger line {line}: the log names capsule_id {capsule_id} but the artifact store has no record of it")]
    MissingRecord { line: usize, capsule_id: String },
    /// The artifact store refused a read or a write.
    #[error("artifact store: {0}")]
    Store(#[from] ArtifactError),
    /// The CLL entry log failed.
    #[error("entry log: {0}")]
    Log(#[from] cll::log::Error),
    /// The log already holds this `capsule_id`, so appending it would not
    /// add a line.
    #[error("refusing to append: capsule_id {capsule_id} is already in the log")]
    AlreadyInLog { capsule_id: String },
    /// A store-backed ledger keeps a line's producer envelope as the record's
    /// envelope, so the line must carry a valid one whose signer `key_id` names.
    #[error("capsule_id {capsule_id}: producer envelope: {detail}")]
    Envelope { capsule_id: String, detail: String },
}

/// A recovered/looked-up ledger entry: the sealed capsule plus its raw
/// COSE_Sign1 signed-statement bytes ("receipt").
pub struct LedgerEntry {
    pub capsule: Value,
    pub signed_statement: Vec<u8>,
}

/// One line of `capsules.jsonl`, in file order: every line is a leaf of the
/// checkpoint MMR, padding included. `seq` is its 1-based position.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LedgerLine {
    pub capsule_id: String,
    /// Where the ledger reads the line back ([`Ledger::read_line`]): the byte
    /// offset of the line's start on the flat layout, its log `seq` on
    /// store-backed ledgers ([`Ledger::open_stores`]).
    pub offset: u64,
    pub padding: bool,
    /// The line's top-level `timestamp`, when it has a string one.
    pub timestamp: Option<String>,
}

impl LedgerLine {
    fn new(capsule_id: &str, offset: u64, record: &Value) -> Self {
        Self {
            capsule_id: capsule_id.to_string(),
            offset,
            padding: is_padding(record),
            timestamp: record
                .get("timestamp")
                .and_then(Value::as_str)
                .map(str::to_string),
        }
    }
}

/// What `open()` found while replaying the ledger — surfaced so a caller can
/// log/report a recovered torn write rather than have it happen invisibly.
#[derive(Debug, Default)]
pub struct RecoveryReport {
    pub valid_entries: usize,
    /// `Some(byte offset)` when a torn trailing write was found and the file
    /// was truncated back to this offset during recovery.
    pub truncated_torn_write_at: Option<u64>,
}

#[derive(Debug)]
pub struct Ledger {
    storage: Storage,
    /// capsule_id -> where its line is read back ([`LedgerLine::offset`]).
    index: HashMap<String, u64>,
    chain_head: Option<String>,
    /// Every line in `capsules.jsonl`, padding included -- the leaf count the
    /// checkpoint MMR over this file will reach once it has folded it all.
    entries: u64,
    /// The keys [`Self::indexer`] derived from every chained record -- rebuilt
    /// on `open`, maintained on `append`. Callers use them as dedup gates
    /// ("does this ledger already hold a record of X?").
    index_keys: HashSet<String>,
    indexer: RecordIndexer,
    /// Every line, padding included, in file order.
    lines: Vec<LedgerLine>,
}

/// The name of the unbound artifact that holds a line's signed statement in
/// an artifact store (store-backed ledgers and
/// [`crate::legacy_ledger::LegacyLedger`]).
pub const STATEMENT: &str = "signed-statement";

/// A line's signed statement as the [`STATEMENT`] artifact: present, unbound.
pub(crate) fn statement_artifact(statement: Vec<u8>) -> Artifact {
    Artifact {
        name: STATEMENT.to_string(),
        binding: String::new(),
        content_sha256: hex::encode(sha2::Sha256::digest(&statement)),
        content: Some(statement),
        state: RetentionState::Present,
    }
}

/// Where a ledger keeps its lines.
#[derive(Debug)]
enum Storage {
    /// `capsules.jsonl` and `signed-statements/`.
    Flat {
        capsules_path: PathBuf,
        statements_dir: PathBuf,
        append_handle: File,
    },
    /// A CLL entry log and an artifact store ([`Ledger::open_stores`]).
    Stores(Stores),
}

/// The two standard layers a store-backed ledger writes through.
struct Stores {
    log: Arc<dyn EntryStore + Send + Sync>,
    artifacts: Arc<dyn Store + Send + Sync>,
}

impl std::fmt::Debug for Stores {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Stores")
            .field("namespace", &self.artifacts.namespace())
            .finish_non_exhaustive()
    }
}

impl Stores {
    /// The record for `capsule_id` as a ledger line and its signed statement,
    /// `None` when the statement is missing or purged. `line` numbers errors.
    fn read(&self, line: usize, capsule_id: &str) -> Result<(Value, Option<Vec<u8>>), LedgerError> {
        let record = match self.artifacts.get(capsule_id) {
            Err(ArtifactError::NotFound) => {
                return Err(LedgerError::MissingRecord {
                    line,
                    capsule_id: capsule_id.to_string(),
                })
            }
            other => other?,
        };
        let mut parsed = decode_record(&record.capsule)?;
        let envelope =
            agent_action_capsule::envelope::verify(capsule_id, &record.producer_envelope);
        let object = parsed
            .as_object_mut()
            .ok_or(LedgerError::MissingCapsuleId { line })?;
        object.insert(
            "signature".into(),
            Value::String(hex::encode(&record.producer_envelope)),
        );
        object.insert(
            "key_id".into(),
            Value::String(hex::encode(&envelope.public_key)),
        );
        let statement = record
            .artifacts
            .into_iter()
            .find(|a| a.name == STATEMENT && a.binding.is_empty())
            .and_then(|a| a.content);
        Ok((parsed, statement))
    }

    /// The `capsule_id` at log position `seq`.
    fn capsule_id_at(&self, seq: u64) -> Result<String, LedgerError> {
        let entry = self
            .log
            .scan_entries(seq.saturating_sub(1), 1)?
            .into_iter()
            .next()
            .filter(|e| seq > 0 && e.seq == seq)
            .ok_or_else(|| cll::log::Error::new(ErrorCode::NotFound, "no log entry at this seq"))?;
        Ok(hex::encode(entry.value))
    }

    /// Writes one line: its record, then its log entry (the commit point).
    /// Returns the entry's `seq`.
    fn write(&self, capsule_id: &str, line: &Value, statement: &[u8]) -> Result<u64, LedgerError> {
        self.artifacts
            .put(&to_record(capsule_id, line, statement)?)?;
        let value = hex::decode(capsule_id).map_err(|_| LedgerError::NoCapsuleId)?;
        let appended = self.log.append(&AppendInput {
            value,
            appended_at: Timestamp::now(),
        })?;
        if appended.outcome != AppendOutcome::Inserted {
            return Err(LedgerError::AlreadyInLog {
                capsule_id: capsule_id.to_string(),
            });
        }
        Ok(appended.entry.seq)
    }
}

/// A ledger line as an artifact record: the line without `signature` and
/// `key_id`, the producer envelope those carry, and the signed statement.
/// Refuses a line without a valid envelope, or whose `key_id` is not exactly
/// the envelope's signer (lowercase hex), so a record always reads back as the
/// line it was written from.
fn to_record(capsule_id: &str, line: &Value, statement: &[u8]) -> Result<Record, LedgerError> {
    let envelope_error = |detail: &str| LedgerError::Envelope {
        capsule_id: capsule_id.to_string(),
        detail: detail.to_string(),
    };
    let mut object = line
        .as_object()
        .cloned()
        .ok_or_else(|| envelope_error("the line is not a JSON object"))?;
    let signature = object.remove("signature");
    let key_id = object.remove("key_id");
    let (Some(Value::String(signature)), Some(Value::String(key_id))) = (signature, key_id) else {
        return Err(envelope_error(
            "a store-backed ledger needs the line's producer envelope (signature and key_id)",
        ));
    };
    let envelope = hex::decode(&signature).map_err(|_| envelope_error("signature is not hex"))?;
    let verified = agent_action_capsule::envelope::verify(capsule_id, &envelope);
    if !verified.ok {
        return Err(envelope_error(
            "the envelope does not verify over the capsule_id",
        ));
    }
    if key_id != hex::encode(&verified.public_key) {
        return Err(envelope_error("key_id is not the envelope's signer"));
    }
    // Lowercase hex, as written back on every read.
    if signature != hex::encode(&envelope) {
        return Err(envelope_error("signature is not lowercase hex"));
    }
    Ok(Record {
        capsule_id: capsule_id.to_string(),
        capsule: serde_json::to_vec(&Value::Object(object))?,
        producer_envelope: envelope,
        artifacts: vec![statement_artifact(statement.to_vec())],
    })
}

/// What a replay builds, line by line: the same checks and index for the flat
/// layout and the store layers.
struct Replay {
    index: HashMap<String, u64>,
    chain_head: Option<String>,
    report: RecoveryReport,
    index_keys: HashSet<String>,
    lines: Vec<LedgerLine>,
    indexer: RecordIndexer,
}

impl Replay {
    fn new(indexer: RecordIndexer) -> Self {
        Self {
            index: HashMap::new(),
            chain_head: None,
            report: RecoveryReport::default(),
            index_keys: HashSet::new(),
            lines: Vec::new(),
            indexer,
        }
    }

    /// Checks line `line_no` and indexes it at `locator`: its `capsule_id`
    /// recomputes, padding has its shape, a chained record extends the head,
    /// and its signed statement (read by `statement` after those checks)
    /// matches it.
    fn admit(
        &mut self,
        line_no: usize,
        locator: u64,
        parsed: Value,
        statement: impl FnOnce(&str) -> Result<Vec<u8>, LedgerError>,
    ) -> Result<(), LedgerError> {
        let stored_id = parsed
            .get("capsule_id")
            .and_then(Value::as_str)
            .ok_or(LedgerError::MissingCapsuleId { line: line_no })?
            .to_string();

        let recomputed = compute_capsule_id(&parsed)?;
        if recomputed != stored_id {
            return Err(LedgerError::CapsuleIdMismatch {
                line: line_no,
                stored: stored_id,
                recomputed,
            });
        }

        let padding = is_padding(&parsed);
        if padding {
            check_padding_shape(&parsed).map_err(|detail| LedgerError::MalformedPadding {
                line: line_no,
                detail,
            })?;
        }
        let parent = parsed
            .get("chain")
            .and_then(|c| c.get("parent_capsule_id"))
            .and_then(Value::as_str)
            .map(str::to_string);
        if !padding && parent != self.chain_head {
            return Err(LedgerError::ChainBroken {
                line: line_no,
                parent,
                head: self.chain_head.clone(),
            });
        }

        // Existence is not enough: a garbage/swapped statement must not
        // reload clean (see `Ledger::open`).
        let statement_bytes = statement(&stored_id)?;
        check_statement_matches(&parsed, &stored_id, &statement_bytes).map_err(|detail| {
            LedgerError::StatementInvalid {
                line: line_no,
                capsule_id: stored_id.clone(),
                detail,
            }
        })?;

        self.report.valid_entries += 1;
        self.lines
            .push(LedgerLine::new(&stored_id, locator, &parsed));
        if padding {
            // Outside the chain: never the head, never indexed.
            return Ok(());
        }
        self.index_keys.extend((self.indexer)(&parsed));
        self.index.insert(stored_id.clone(), locator);
        self.chain_head = Some(stored_id);
        Ok(())
    }

    fn finish(self, storage: Storage) -> (Ledger, RecoveryReport) {
        (
            Ledger {
                storage,
                index: self.index,
                chain_head: self.chain_head,
                entries: self.report.valid_entries as u64,
                index_keys: self.index_keys,
                indexer: self.indexer,
                lines: self.lines,
            },
            self.report,
        )
    }
}

/// The error for a missing or purged statement attachment.
fn statement_missing(line: usize, capsule_id: &str) -> LedgerError {
    LedgerError::StatementInvalid {
        line,
        capsule_id: capsule_id.to_string(),
        detail: format!("the record's {STATEMENT} attachment is missing or purged"),
    }
}

fn statement_path(statements_dir: &Path, capsule_id: &str) -> PathBuf {
    statements_dir.join(format!("{capsule_id}.cose"))
}

/// Derives index keys from one chained record. A caller that needs to ask
/// "does this ledger already hold a record of X?" supplies one to
/// [`Ledger::open_with_indexer`] and asks [`Ledger::has_index_key`].
pub type RecordIndexer = fn(&Value) -> Vec<String>;

/// Index key for a record citing another party's record of the same exchange.
pub fn counterparty_half_key(foreign_capsule_id: &str) -> String {
    format!(
        "{}:{foreign_capsule_id}",
        crate::capsule::CITATION_PURPOSE_COUNTERPARTY_HALF
    )
}

/// Index key for a record citing another party's inclusion proof for its
/// record `foreign_capsule_id`.
pub fn counterparty_inclusion_key(foreign_capsule_id: &str) -> String {
    format!(
        "{}:{foreign_capsule_id}",
        crate::capsule::CITATION_PURPOSE_COUNTERPARTY_INCLUSION
    )
}

/// The default indexer: keys for the two registered cross-party citation
/// purposes.
///
/// - A `counterparty_half` citation is identified by
///   `references[].citation_purpose` alone (never by a `chain.relation`
///   string); its key names the cited capsule.
/// - A `counterparty_inclusion` citation's `references[]` cite the proof and
///   the checkpoint; the record it is about is named in
///   `compute_attestation.counterparty_inclusion.half_capsule_id`, and the
///   key names that.
pub fn counterparty_citation_keys(capsule: &Value) -> Vec<String> {
    let mut keys = Vec::new();
    let Some(references) = capsule.get("references").and_then(Value::as_array) else {
        return keys;
    };
    let purpose = |reference: &Value| {
        reference
            .get("citation_purpose")
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    for reference in references {
        if purpose(reference).as_deref() == Some(crate::capsule::CITATION_PURPOSE_COUNTERPARTY_HALF)
        {
            if let Some(digest) = reference.get("digest").and_then(Value::as_str) {
                keys.push(counterparty_half_key(digest));
            }
        }
    }
    let is_inclusion_citation = references.iter().any(|r| {
        purpose(r).as_deref() == Some(crate::capsule::CITATION_PURPOSE_COUNTERPARTY_INCLUSION)
    });
    if is_inclusion_citation {
        if let Some(half) = capsule
            .pointer(
                "/model_attestation/compute_attestation/counterparty_inclusion/half_capsule_id",
            )
            .and_then(Value::as_str)
        {
            keys.push(counterparty_inclusion_key(half));
        }
    }
    keys
}

/// The reload check behind [`LedgerError::StatementInvalid`]: the `.cose`
/// beside a ledger line must be a parseable COSE_Sign1 whose payload names
/// this line's `capsule_id` -- and, when the capsule carries its inline
/// producer `key_id` (the hex Ed25519 public key `attach_producer_envelope`
/// writes), the COSE signature must actually verify against that key, through
/// the same `cose::verify_signed_statement` path `verify::verify_offline`
/// gates on. Returns the human-readable failure detail on mismatch.
pub fn check_statement_matches(
    capsule: &Value,
    capsule_id: &str,
    statement: &[u8],
) -> Result<(), String> {
    let payload = match producer_verifying_key(capsule) {
        Some(Ok(key)) => {
            crate::cose::verify_signed_statement(statement, &key)
                .map_err(|e| {
                    format!("COSE verification against the capsule's own key_id failed: {e}")
                })?
                .payload
        }
        Some(Err(detail)) => return Err(detail),
        // No inline key_id (e.g. a pre-envelope capsule): signature
        // verification is impossible without a key, but a garbage or
        // swapped statement file is still caught by parsing the COSE and
        // checking whose capsule its payload names.
        None => crate::cose::statement_payload(statement)
            .map_err(|e| format!("not a parseable COSE_Sign1: {e}"))?,
    };
    let payload_json =
        decode_record(&payload).map_err(|e| format!("COSE payload is not valid JSON: {e}"))?;
    match payload_json.get("capsule_id").and_then(Value::as_str) {
        Some(id) if id == capsule_id => Ok(()),
        other => Err(format!(
            "COSE payload names capsule_id {other:?}, expected {capsule_id:?}"
        )),
    }
}

/// The Ed25519 verifying key a capsule's inline `key_id` names, when it
/// carries one. `None` when the capsule has no `key_id`; `Some(Err(..))` when
/// it has one that is not a valid hex-encoded Ed25519 public key (a corrupt
/// claim -- surfaced, never skipped).
fn producer_verifying_key(capsule: &Value) -> Option<Result<ed25519_dalek::VerifyingKey, String>> {
    let key_hex = capsule.get("key_id").and_then(Value::as_str)?;
    Some(
        hex::decode(key_hex)
            .ok()
            .and_then(|bytes| <[u8; 32]>::try_from(bytes).ok())
            .and_then(|bytes| ed25519_dalek::VerifyingKey::from_bytes(&bytes).ok())
            .ok_or_else(|| format!("capsule key_id {key_hex:?} is not a valid Ed25519 public key")),
    )
}

impl Ledger {
    /// Open (creating if absent) a ledger rooted at `ledger_dir`, replaying
    /// `capsules.jsonl` to recover the chain head + receipt index.
    ///
    /// Reload verifies each entry's `.cose` signed statement by CONTENT, not
    /// just existence: the statement must parse as COSE_Sign1, its payload
    /// must name the line's `capsule_id`, and where the capsule carries its
    /// inline producer `key_id` the signature is verified against it (see
    /// [`check_statement_matches`]). A garbage or swapped statement file is a
    /// hard [`LedgerError::StatementInvalid`], same spirit as
    /// [`LedgerError::CapsuleIdMismatch`]. Startup cost: one read + COSE
    /// parse (+ one Ed25519 verify where a key_id is present) per entry per
    /// open -- linear in ledger length; a
    /// much larger ledger would want a verified index instead of dropping
    /// this check.
    pub fn open(ledger_dir: &Path) -> Result<(Self, RecoveryReport), LedgerError> {
        Self::open_with_indexer(ledger_dir, counterparty_citation_keys)
    }

    /// [`Self::open`] with a caller-supplied [`RecordIndexer`]. A deployment
    /// that indexes its own record kinds as well usually calls
    /// [`counterparty_citation_keys`] from its indexer and adds its own keys.
    pub fn open_with_indexer(
        ledger_dir: &Path,
        indexer: RecordIndexer,
    ) -> Result<(Self, RecoveryReport), LedgerError> {
        fs::create_dir_all(ledger_dir)?;
        let statements_dir = ledger_dir.join("signed-statements");
        fs::create_dir_all(&statements_dir)?;
        let capsules_path = ledger_dir.join("capsules.jsonl");
        if !capsules_path.exists() {
            File::create(&capsules_path)?;
        }

        let mut raw = Vec::new();
        File::open(&capsules_path)?.read_to_end(&mut raw)?;

        let mut offset: u64 = 0;
        let mut replay = Replay::new(indexer);

        // Split on '\n', keeping track of whether the buffer ends with one.
        // A missing trailing newline on the final chunk means a torn write:
        // truncate it away rather than trust or reject it as corruption.
        let ends_with_newline = raw.last() == Some(&b'\n');
        let text = String::from_utf8_lossy(&raw);
        let mut parts: Vec<&str> = text.split('\n').collect();
        if parts.last() == Some(&"") {
            parts.pop(); // trailing split artifact from a final '\n'
        }

        for (i, line) in parts.iter().enumerate() {
            let is_last = i == parts.len() - 1;
            let line_bytes_len = line.len() as u64 + 1; // + '\n'
            let line_no = i + 1;

            if line.is_empty() {
                offset += line_bytes_len;
                continue;
            }

            if is_last && !ends_with_newline {
                // Torn write: truncate the file back to the start of this
                // line so the on-disk ledger never carries a partial record.
                let f = OpenOptions::new().write(true).open(&capsules_path)?;
                f.set_len(offset)?;
                replay.report.truncated_torn_write_at = Some(offset);
                break;
            }

            let parsed = decode_record(line.as_bytes())?;
            replay.admit(line_no, offset, parsed, |stored_id| {
                let stmt_path = statement_path(&statements_dir, stored_id);
                if !stmt_path.exists() {
                    return Err(LedgerError::MissingStatement {
                        line: line_no,
                        capsule_id: stored_id.to_string(),
                        path: stmt_path.display().to_string(),
                    });
                }
                Ok(fs::read(&stmt_path)?)
            })?;
            offset += line_bytes_len;
        }

        let append_handle = OpenOptions::new().append(true).open(&capsules_path)?;
        Ok(replay.finish(Storage::Flat {
            capsules_path,
            statements_dir,
            append_handle,
        }))
    }

    /// Open a ledger kept in a CLL entry log and an artifact store (see the
    /// module doc's **Storage**), replaying the log to recover the chain head
    /// and index with the checks [`Self::open`] runs. Both stores must already
    /// be provisioned (`init`); an empty log is an empty ledger. The store's
    /// trusted keys must include every key that signed a line.
    pub fn open_stores(
        log: Arc<dyn EntryStore + Send + Sync>,
        artifacts: Arc<dyn Store + Send + Sync>,
    ) -> Result<(Self, RecoveryReport), LedgerError> {
        Self::open_stores_with_indexer(log, artifacts, counterparty_citation_keys)
    }

    /// [`Self::open_stores`] with a caller-supplied [`RecordIndexer`].
    pub fn open_stores_with_indexer(
        log: Arc<dyn EntryStore + Send + Sync>,
        artifacts: Arc<dyn Store + Send + Sync>,
        indexer: RecordIndexer,
    ) -> Result<(Self, RecoveryReport), LedgerError> {
        let stores = Stores { log, artifacts };
        let mut replay = Replay::new(indexer);
        let mut after = 0u64;
        loop {
            let page = stores.log.scan_entries(after, MAX_SCAN_LIMIT)?;
            if page.is_empty() {
                break;
            }
            for entry in page {
                after = entry.seq;
                let line_no = entry.seq as usize;
                let capsule_id = hex::encode(&entry.value);
                let (parsed, statement) = stores.read(line_no, &capsule_id)?;
                if parsed.get("capsule_id").and_then(Value::as_str) != Some(capsule_id.as_str()) {
                    return Err(LedgerError::CapsuleIdMismatch {
                        line: line_no,
                        stored: parsed
                            .get("capsule_id")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            .to_string(),
                        recomputed: capsule_id,
                    });
                }
                replay.admit(line_no, entry.seq, parsed, |stored_id| {
                    statement.ok_or_else(|| statement_missing(line_no, stored_id))
                })?;
            }
        }
        Ok(replay.finish(Storage::Stores(stores)))
    }

    /// Every line in the ledger, padding included -- see [`Self::pad_to_bucket`].
    pub fn entries(&self) -> u64 {
        self.entries
    }

    pub fn chain_head(&self) -> Option<&str> {
        self.chain_head.as_deref()
    }

    pub fn contains(&self, capsule_id: &str) -> bool {
        self.index.contains_key(capsule_id)
    }

    /// All `capsule_id`s this ledger has validated and indexed -- the "known
    /// capsule store" a caller passes to `verify::verify_offline` for
    /// chain-parent-membership checking.
    pub fn known_capsule_ids(&self) -> std::collections::HashSet<String> {
        self.index.keys().cloned().collect()
    }

    /// Every indexed `capsule_id` in chain order (first record first), read
    /// off the index's byte offsets -- `capsules.jsonl` is append-only, so
    /// offset order IS chain order. Position `i` here is record `i + 1`.
    pub fn capsule_ids_in_order(&self) -> Vec<String> {
        let mut by_offset: Vec<(&u64, &String)> =
            self.index.iter().map(|(id, off)| (off, id)).collect();
        by_offset.sort_unstable();
        by_offset.into_iter().map(|(_, id)| id.clone()).collect()
    }

    /// How many records this ledger holds.
    pub fn len(&self) -> usize {
        self.index.len()
    }

    pub fn is_empty(&self) -> bool {
        self.index.is_empty()
    }

    /// Whether this ledger's indexer produced `key` for any chained record.
    pub fn has_index_key(&self, key: &str) -> bool {
        self.index_keys.contains(key)
    }

    /// Whether this ledger already holds a record citing the other party's
    /// record `foreign_capsule_id` (a `counterparty_half` citation). A dedup
    /// gate: a record received again must not seal a second citation.
    /// Answers from the default keys, so a custom indexer must include
    /// [`counterparty_citation_keys`] for this to work.
    pub fn cites_counterparty_half(&self, foreign_capsule_id: &str) -> bool {
        self.has_index_key(&counterparty_half_key(foreign_capsule_id))
    }

    /// Whether this ledger already holds a `counterparty_inclusion` citation
    /// for the other party's record `foreign_capsule_id`. Same note as
    /// [`Self::cites_counterparty_half`].
    pub fn cites_counterparty_inclusion(&self, foreign_capsule_id: &str) -> bool {
        self.has_index_key(&counterparty_inclusion_key(foreign_capsule_id))
    }

    /// Append a sealed capsule + its signed statement. The statement file is
    /// written and fsync'd BEFORE the jsonl line, so a crash between the two
    /// leaves at worst an unindexed orphan `.cose` file -- never a jsonl
    /// entry pointing at a receipt that doesn't exist. Refuses to write if
    /// `capsule.chain.parent_capsule_id` doesn't match the current head.
    pub fn append(&mut self, capsule: &Value, signed_statement: &[u8]) -> Result<(), LedgerError> {
        if is_padding(capsule) {
            return Err(LedgerError::PaddingMisrouted(
                "a padding record goes through append_padding, never onto the chain",
            ));
        }
        let capsule_id = capsule
            .get("capsule_id")
            .and_then(Value::as_str)
            .ok_or(LedgerError::NoCapsuleId)?
            .to_string();

        let parent = capsule
            .get("chain")
            .and_then(|c| c.get("parent_capsule_id"))
            .and_then(Value::as_str)
            .map(str::to_string);
        if parent != self.chain_head {
            return Err(LedgerError::AppendChainMismatch {
                parent,
                head: self.chain_head.clone(),
            });
        }

        let offset = self.write_line(&capsule_id, capsule, signed_statement)?;

        self.index_keys.extend((self.indexer)(capsule));
        self.index.insert(capsule_id.clone(), offset);
        self.chain_head = Some(capsule_id);
        Ok(())
    }

    /// The statement-then-line write every append shares: the `.cose` is
    /// fsync'd BEFORE the jsonl line, so a crash between the two leaves at
    /// worst an orphan statement file. Returns the line's byte offset.
    fn write_line(
        &mut self,
        capsule_id: &str,
        record: &Value,
        signed_statement: &[u8],
    ) -> Result<u64, LedgerError> {
        let offset = match &mut self.storage {
            Storage::Flat {
                capsules_path,
                statements_dir,
                append_handle,
            } => {
                let stmt_path = statement_path(statements_dir, capsule_id);
                let mut stmt_file = File::create(&stmt_path)?;
                stmt_file.write_all(signed_statement)?;
                stmt_file.sync_all()?;

                let offset = fs::metadata(capsules_path)?.len();
                let mut line = serde_json::to_string(record)?;
                line.push('\n');
                append_handle.write_all(line.as_bytes())?;
                append_handle.sync_all()?;
                offset
            }
            Storage::Stores(stores) => {
                // Everything open_stores will check, before anything is
                // written: a refused line must leave both stores reopenable.
                let line_no = self.entries as usize + 1;
                let recomputed = compute_capsule_id(record)?;
                if recomputed != capsule_id {
                    return Err(LedgerError::CapsuleIdMismatch {
                        line: line_no,
                        stored: capsule_id.to_string(),
                        recomputed,
                    });
                }
                check_statement_matches(record, capsule_id, signed_statement).map_err(
                    |detail| LedgerError::StatementInvalid {
                        line: line_no,
                        capsule_id: capsule_id.to_string(),
                        detail,
                    },
                )?;
                stores.write(capsule_id, record, signed_statement)?
            }
        };
        self.entries += 1;
        self.lines.push(LedgerLine::new(capsule_id, offset, record));
        Ok(offset)
    }

    /// Append one padding record + its signed statement. The chain head does
    /// not move and the record is not indexed: a padding record is a leaf of
    /// the checkpoint MMR and nothing else (see the module doc).
    pub fn append_padding(
        &mut self,
        record: &Value,
        signed_statement: &[u8],
    ) -> Result<(), LedgerError> {
        if !is_padding(record) {
            return Err(LedgerError::PaddingMisrouted(
                "append_padding only takes a padding record",
            ));
        }
        check_padding_shape(record).map_err(|detail| LedgerError::MalformedPadding {
            line: self.entries as usize + 1,
            detail,
        })?;
        let capsule_id = record
            .get("capsule_id")
            .and_then(Value::as_str)
            .ok_or(LedgerError::NoCapsuleId)?
            .to_string();
        self.write_line(&capsule_id, record, signed_statement)?;
        Ok(())
    }

    /// Append padding records until the ledger's line count is a multiple of
    /// `bucket` (a no-op when it already is, so a retry or a restart never
    /// pads twice). Each record is sealed with a fresh store nonce, carries
    /// the producer envelope under `signing_key`, and gets a detached signed
    /// statement from the same key, like every other line. Returns the
    /// resulting line count -- the leaf count the next checkpoint is cut at.
    ///
    /// The caller holds this ledger's writer lock across the call, so no
    /// real record can land between two padding records.
    pub fn pad_to_bucket(
        &mut self,
        bucket: u64,
        signing_key: &ed25519_dalek::SigningKey,
        issuer: &str,
    ) -> Result<u64, LedgerError> {
        if bucket == 0 {
            return Ok(self.entries);
        }
        let missing = (bucket - self.entries % bucket) % bucket;
        for _ in 0..missing {
            let mut record = crate::padding::build_padding_record()?;
            crate::capsule::attach_producer_envelope(&mut record, signing_key)
                .expect("build_padding_record always sets a hex capsule_id");
            let capsule_id = record["capsule_id"]
                .as_str()
                .expect("build_padding_record always sets capsule_id")
                .to_string();
            let statement = crate::cose::build_signed_statement(
                &crate::cose::SignedStatementInput {
                    payload: &crate::capsule::payload_bytes(&record),
                    issuer,
                    subject: &capsule_id,
                    content_type: crate::padding::PADDING_CONTENT_TYPE,
                },
                signing_key,
            );
            self.append_padding(&record, &statement)?;
        }
        Ok(self.entries)
    }

    /// Every line in file order, padding included: line `i` is leaf `i + 1`.
    pub fn lines(&self) -> &[LedgerLine] {
        &self.lines
    }

    /// The JSON of the line at `offset` (from [`Self::lines`]).
    pub fn read_line(&self, offset: u64) -> Result<Value, LedgerError> {
        match &self.storage {
            Storage::Flat { capsules_path, .. } => {
                let mut f = File::open(capsules_path)?;
                f.seek(SeekFrom::Start(offset))?;
                let mut line = String::new();
                BufReader::new(f).read_line(&mut line)?;
                Ok(decode_record(line.trim_end().as_bytes())?)
            }
            Storage::Stores(stores) => {
                let capsule_id = stores.capsule_id_at(offset)?;
                Ok(stores.read(offset as usize, &capsule_id)?.0)
            }
        }
    }

    /// The signed statement stored for `capsule_id`, padding included.
    pub fn read_statement(&self, capsule_id: &str) -> Result<Vec<u8>, LedgerError> {
        match &self.storage {
            Storage::Flat { statements_dir, .. } => {
                Ok(fs::read(statement_path(statements_dir, capsule_id))?)
            }
            Storage::Stores(stores) => {
                let line = self.line_number(capsule_id);
                stores
                    .read(line, capsule_id)?
                    .1
                    .ok_or_else(|| statement_missing(line, capsule_id))
            }
        }
    }

    /// The 1-based line holding `capsule_id`, for error messages (0 when the
    /// ledger has no such line).
    fn line_number(&self, capsule_id: &str) -> usize {
        self.lines
            .iter()
            .position(|l| l.capsule_id == capsule_id)
            .map_or(0, |i| i + 1)
    }

    /// Receipt lookup: read back the sealed capsule + its COSE_Sign1
    /// signed-statement bytes by `capsule_id`, seeking directly to the
    /// indexed byte offset rather than scanning the file (on store-backed
    /// ledgers, reading its record by id).
    pub fn lookup(&self, capsule_id: &str) -> Result<Option<LedgerEntry>, LedgerError> {
        let Some(&offset) = self.index.get(capsule_id) else {
            return Ok(None);
        };
        match &self.storage {
            Storage::Flat {
                capsules_path,
                statements_dir,
                ..
            } => {
                let mut f = File::open(capsules_path)?;
                f.seek(SeekFrom::Start(offset))?;
                let mut reader = BufReader::new(f);
                let mut line = String::new();
                reader.read_line(&mut line)?;
                let capsule = decode_record(line.trim_end().as_bytes())?;

                let stmt_path = statement_path(statements_dir, capsule_id);
                let mut signed_statement = Vec::new();
                File::open(&stmt_path)?.read_to_end(&mut signed_statement)?;

                Ok(Some(LedgerEntry {
                    capsule,
                    signed_statement,
                }))
            }
            Storage::Stores(stores) => {
                let line = offset as usize;
                let (capsule, statement) = stores.read(line, capsule_id)?;
                Ok(Some(LedgerEntry {
                    capsule,
                    signed_statement: statement
                        .ok_or_else(|| statement_missing(line, capsule_id))?,
                }))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// A minimal, independently-computed capsule (not routed through
    /// `capsule::seal`, so these tests exercise the ledger's OWN
    /// recomputation/validation logic against a value it didn't build).
    fn sample_capsule(seed: &str, parent: Option<&str>) -> Value {
        let mut body = serde_json::Map::new();
        body.insert(
            "spec_version".into(),
            json!("draft-mih-scitt-agent-action-capsule-02"),
        );
        body.insert("format_version".into(), json!("4"));
        body.insert("canonicalization_id".into(), json!("jcs"));
        body.insert("action_id".into(), json!(format!("test/{seed}")));
        body.insert("seed".into(), json!(seed));
        if let Some(p) = parent {
            body.insert(
                "chain".into(),
                json!({"parent_capsule_id": p, "relation": "follows"}),
            );
        }
        let capsule_id = compute_capsule_id(&Value::Object(body.clone())).unwrap();
        body.insert("capsule_id".into(), json!(capsule_id));
        // capsule_id must sort logically before other fields for readability
        // only; JSON object field order doesn't matter to any check here.
        Value::Object(body)
    }

    /// A REAL COSE_Sign1 signed statement over the capsule's own JSON bytes
    /// -- reload now verifies statement CONTENT, so a fake byte-blob would
    /// (rightly) fail `Ledger::open`. Deterministic key + deterministic
    /// Ed25519 => reproducible bytes, so equality assertions still hold.
    fn statement_for(capsule: &Value) -> Vec<u8> {
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[42u8; 32]);
        crate::cose::build_signed_statement(
            &crate::cose::SignedStatementInput {
                payload: &serde_json::to_vec(capsule).unwrap(),
                issuer: "ledger-test",
                subject: capsule["capsule_id"].as_str().unwrap(),
                content_type: "application/vnd.agent-action-capsule+json",
            },
            &signing_key,
        )
    }

    #[test]
    fn append_then_reopen_recovers_chain_head() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, report) = Ledger::open(dir.path()).unwrap();
        assert_eq!(report.valid_entries, 0);
        assert!(ledger.chain_head().is_none());

        let c1 = sample_capsule("one", None);
        let id1 = c1["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&c1, &statement_for(&c1)).unwrap();
        assert_eq!(ledger.chain_head(), Some(id1.as_str()));

        let c2 = sample_capsule("two", Some(&id1));
        let id2 = c2["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&c2, &statement_for(&c2)).unwrap();
        assert_eq!(ledger.chain_head(), Some(id2.as_str()));
        drop(ledger);

        // Restart: a fresh Ledger::open() over the same directory must
        // recover the same head purely by replaying capsules.jsonl.
        let (ledger2, report2) = Ledger::open(dir.path()).unwrap();
        assert_eq!(report2.valid_entries, 2);
        assert_eq!(ledger2.chain_head(), Some(id2.as_str()));
        assert!(ledger2.contains(&id1));
        assert!(ledger2.contains(&id2));
    }

    #[test]
    fn lines_index_every_line_in_file_order_across_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();
        let c1 = sample_capsule("one", None);
        let id1 = c1["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&c1, &statement_for(&c1)).unwrap();
        let key = ed25519_dalek::SigningKey::from_bytes(&[42u8; 32]);
        ledger.pad_to_bucket(3, &key, "ledger-test").unwrap();
        let ids: Vec<String> = ledger
            .lines()
            .iter()
            .map(|l| l.capsule_id.clone())
            .collect();
        assert_eq!(ids.len(), 3);
        assert_eq!(ids[0], id1);
        assert_eq!(
            ledger.lines().iter().map(|l| l.padding).collect::<Vec<_>>(),
            vec![false, true, true]
        );
        for line in ledger.lines() {
            let read = ledger.read_line(line.offset).unwrap();
            assert_eq!(read["capsule_id"].as_str(), Some(line.capsule_id.as_str()));
            let statement = ledger.read_statement(&line.capsule_id).unwrap();
            check_statement_matches(&read, &line.capsule_id, &statement).unwrap();
        }
        let before = ledger.lines().to_vec();
        drop(ledger);
        let (reopened, _) = Ledger::open(dir.path()).unwrap();
        assert_eq!(reopened.lines(), &before[..]);
    }

    #[test]
    fn append_rejects_chain_head_mismatch() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();
        let c1 = sample_capsule("one", None);
        ledger.append(&c1, &statement_for(&c1)).unwrap();

        // A capsule chaining to the WRONG parent (or no parent, when one is
        // expected) must be refused, not silently appended.
        let bad = sample_capsule("bogus", Some(&"f".repeat(64)));
        let err = ledger.append(&bad, &statement_for(&bad)).unwrap_err();
        assert!(matches!(err, LedgerError::AppendChainMismatch { .. }));

        let bad_no_parent = sample_capsule("no-parent", None);
        let err2 = ledger
            .append(&bad_no_parent, &statement_for(&bad_no_parent))
            .unwrap_err();
        assert!(matches!(err2, LedgerError::AppendChainMismatch { .. }));
    }

    #[test]
    fn receipt_lookup_returns_capsule_and_statement() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();
        let c1 = sample_capsule("one", None);
        let id1 = c1["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&c1, &statement_for(&c1)).unwrap();

        let entry = ledger.lookup(&id1).unwrap().expect("entry present");
        assert_eq!(entry.capsule, c1);
        assert_eq!(entry.signed_statement, statement_for(&c1));

        assert!(ledger.lookup(&"0".repeat(64)).unwrap().is_none());
    }

    #[test]
    fn torn_write_is_truncated_not_silently_accepted() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();
        let c1 = sample_capsule("one", None);
        let id1 = c1["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&c1, &statement_for(&c1)).unwrap();
        drop(ledger);

        // Simulate a crash mid-write: append a syntactically-truncated
        // second line with NO trailing newline.
        let capsules_path = dir.path().join("capsules.jsonl");
        let mut f = OpenOptions::new()
            .append(true)
            .open(&capsules_path)
            .unwrap();
        f.write_all(br#"{"capsule_id":"deadbee"#).unwrap(); // no trailing \n
        f.flush().unwrap();
        drop(f);

        let (ledger2, report) = Ledger::open(dir.path()).unwrap();
        assert!(report.truncated_torn_write_at.is_some());
        assert_eq!(report.valid_entries, 1);
        // The chain head reverts to the last VALID entry -- the torn write
        // was never accepted, silently or otherwise.
        assert_eq!(ledger2.chain_head(), Some(id1.as_str()));

        // And the file itself was actually truncated on disk, so the next
        // append lands cleanly (not appended after garbage bytes).
        let contents = fs::read_to_string(&capsules_path).unwrap();
        assert!(!contents.contains("deadbee"));
    }

    /// A capsule carrying a top-level `references` array (a
    /// CITING record) whose id was
    /// computed WITH `references` in the preimage. Proves the ledger's own
    /// recomputation covers `references` (`compute_capsule_id` includes it),
    /// so a chain of citing records cold-reloads clean.
    fn citing_capsule(seed: &str, parent: Option<&str>, cited: &str) -> Value {
        let mut body = serde_json::Map::new();
        body.insert(
            "spec_version".into(),
            json!("draft-mih-scitt-agent-action-capsule-02"),
        );
        body.insert("format_version".into(), json!("4"));
        body.insert("canonicalization_id".into(), json!("jcs"));
        body.insert("action_id".into(), json!(format!("cite/{seed}")));
        body.insert("seed".into(), json!(seed));
        if let Some(p) = parent {
            body.insert(
                "chain".into(),
                json!({"parent_capsule_id": p, "relation": "follows"}),
            );
        }
        body.insert(
            "references".into(),
            json!([{"type": "capsule", "digest_alg": "SHA-256", "digest": cited, "citation_purpose": "counterparty_half"}]),
        );
        let capsule_id = compute_capsule_id(&Value::Object(body.clone())).unwrap();
        body.insert("capsule_id".into(), json!(capsule_id));
        Value::Object(body)
    }

    /// A ledger holding citing records
    /// (each with a top-level `references` array committed into its
    /// `capsule_id`) recovers its chain head cleanly on cold reopen -- no
    /// `Ledger::open` change is needed for the new record shape, since
    /// `compute_capsule_id` already commits `references` into the preimage.
    #[test]
    fn ledger_with_citing_records_cold_reloads_clean() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();

        // A normal local record, then a citing record chained onto it, then a
        // second citing record -- the real shape after a received-half seal.
        let local = sample_capsule("local", None);
        let local_id = local["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&local, &statement_for(&local)).unwrap();

        let cite1 = citing_capsule("cite1", Some(&local_id), &"a".repeat(64));
        let cite1_id = cite1["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&cite1, &statement_for(&cite1)).unwrap();

        let cite2 = citing_capsule("cite2", Some(&cite1_id), &"b".repeat(64));
        let cite2_id = cite2["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&cite2, &statement_for(&cite2)).unwrap();
        drop(ledger);

        let (ledger2, report) = Ledger::open(dir.path()).unwrap();
        assert_eq!(report.valid_entries, 3);
        assert_eq!(ledger2.chain_head(), Some(cite2_id.as_str()));
        assert!(ledger2.contains(&local_id));
        assert!(ledger2.contains(&cite1_id));
        assert!(ledger2.contains(&cite2_id));
    }

    #[test]
    fn tampered_terminated_line_is_a_hard_error_not_a_silent_drop() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();
        let c1 = sample_capsule("one", None);
        ledger.append(&c1, &statement_for(&c1)).unwrap();
        drop(ledger);

        // Tamper the capsule_id field in a fully-terminated (newline-ended)
        // line -- this is corruption, not a torn write, and must be a hard
        // error on reopen, never quietly dropped like a torn write is.
        let capsules_path = dir.path().join("capsules.jsonl");
        let contents = fs::read_to_string(&capsules_path).unwrap();
        let tampered = contents.replace(c1["capsule_id"].as_str().unwrap(), &"a".repeat(64));
        assert_ne!(contents, tampered);
        fs::write(&capsules_path, tampered).unwrap();

        let err = Ledger::open(dir.path()).unwrap_err();
        assert!(matches!(err, LedgerError::CapsuleIdMismatch { .. }));
    }

    /// Reload verifies statement CONTENT, not just existence: a garbage
    /// `.cose` file (not COSE_Sign1 at all) must be a hard error on reopen,
    /// never a clean reload.
    #[test]
    fn garbage_statement_file_is_a_hard_error_on_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();
        let c1 = sample_capsule("one", None);
        let id1 = c1["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&c1, &statement_for(&c1)).unwrap();
        drop(ledger);

        let stmt_path = dir
            .path()
            .join("signed-statements")
            .join(format!("{id1}.cose"));
        fs::write(&stmt_path, b"not a cose statement at all").unwrap();

        let err = Ledger::open(dir.path()).unwrap_err();
        assert!(
            matches!(err, LedgerError::StatementInvalid { .. }),
            "expected StatementInvalid, got: {err}"
        );
    }

    /// A statement file SWAPPED with another entry's (each one a perfectly
    /// valid COSE_Sign1 -- just over the wrong capsule) must be caught: the
    /// payload names a different capsule_id than the ledger line it sits
    /// beside.
    #[test]
    fn swapped_statement_files_are_a_hard_error_on_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();
        let c1 = sample_capsule("one", None);
        let id1 = c1["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&c1, &statement_for(&c1)).unwrap();
        let c2 = sample_capsule("two", Some(&id1));
        let id2 = c2["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&c2, &statement_for(&c2)).unwrap();
        drop(ledger);

        let statements_dir = dir.path().join("signed-statements");
        let p1 = statements_dir.join(format!("{id1}.cose"));
        let p2 = statements_dir.join(format!("{id2}.cose"));
        let b1 = fs::read(&p1).unwrap();
        let b2 = fs::read(&p2).unwrap();
        fs::write(&p1, &b2).unwrap();
        fs::write(&p2, &b1).unwrap();

        let err = Ledger::open(dir.path()).unwrap_err();
        assert!(
            matches!(err, LedgerError::StatementInvalid { .. }),
            "expected StatementInvalid, got: {err}"
        );
    }

    /// Where the capsule names its producer key inline (`key_id`), reload
    /// verifies the COSE signature against it -- a statement re-signed by a
    /// DIFFERENT key (payload intact, so the payload check alone would pass)
    /// must be a hard error.
    #[test]
    fn statement_signed_by_a_different_key_is_a_hard_error_on_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();

        let node_key = ed25519_dalek::SigningKey::from_bytes(&[7u8; 32]);
        let mut c1 = sample_capsule("one", None);
        crate::capsule::attach_producer_envelope(&mut c1, &node_key).unwrap();
        let id1 = c1["capsule_id"].as_str().unwrap().to_string();
        // Genuine statement, signed by the SAME key the capsule names.
        let good = crate::cose::build_signed_statement(
            &crate::cose::SignedStatementInput {
                payload: &serde_json::to_vec(&c1).unwrap(),
                issuer: "ledger-test",
                subject: &id1,
                content_type: "application/vnd.agent-action-capsule+json",
            },
            &node_key,
        );
        ledger.append(&c1, &good).unwrap();
        drop(ledger);
        // Reload of the genuine state is clean (signature verifies).
        {
            let (_ledger, report) = Ledger::open(dir.path()).unwrap();
            assert_eq!(report.valid_entries, 1);
        }

        // Swap in a statement over the same payload signed by an IMPOSTOR key.
        let impostor = ed25519_dalek::SigningKey::from_bytes(&[8u8; 32]);
        let forged = crate::cose::build_signed_statement(
            &crate::cose::SignedStatementInput {
                payload: &serde_json::to_vec(&c1).unwrap(),
                issuer: "ledger-test",
                subject: &id1,
                content_type: "application/vnd.agent-action-capsule+json",
            },
            &impostor,
        );
        let stmt_path = dir
            .path()
            .join("signed-statements")
            .join(format!("{id1}.cose"));
        fs::write(&stmt_path, &forged).unwrap();

        let err = Ledger::open(dir.path()).unwrap_err();
        assert!(
            matches!(err, LedgerError::StatementInvalid { .. }),
            "expected StatementInvalid, got: {err}"
        );
    }

    /// The counterparty-half citation set survives append AND reopen, and is
    /// keyed on `citation_purpose` alone -- a legacy `relation: "cites"`
    /// citing record counts exactly like a current `"follows"` one
    /// (the record kind is the citation purpose, never the
    /// relation string).
    #[test]
    fn cited_counterparty_halves_tracked_on_append_and_rebuilt_on_open() {
        let dir = tempfile::tempdir().unwrap();
        let (mut ledger, _) = Ledger::open(dir.path()).unwrap();

        let local = sample_capsule("local", None);
        let local_id = local["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&local, &statement_for(&local)).unwrap();
        assert!(!ledger.cites_counterparty_half(&"a".repeat(64)));

        // A current citing record (relation "follows").
        let cite1 = citing_capsule("cite1", Some(&local_id), &"a".repeat(64));
        let cite1_id = cite1["capsule_id"].as_str().unwrap().to_string();
        ledger.append(&cite1, &statement_for(&cite1)).unwrap();
        assert!(ledger.cites_counterparty_half(&"a".repeat(64)));

        // A LEGACY citing record (relation "cites") -- same kind, same set.
        let mut body = serde_json::Map::new();
        body.insert(
            "spec_version".into(),
            json!("draft-mih-scitt-agent-action-capsule-02"),
        );
        body.insert("format_version".into(), json!("4"));
        body.insert("canonicalization_id".into(), json!("jcs"));
        body.insert("action_id".into(), json!("cite/legacy"));
        body.insert(
            "chain".into(),
            json!({"parent_capsule_id": cite1_id, "relation": "cites"}),
        );
        body.insert(
            "references".into(),
            json!([{"type": "capsule", "digest_alg": "SHA-256", "digest": "b".repeat(64), "citation_purpose": "counterparty_half"}]),
        );
        let legacy_id = compute_capsule_id(&Value::Object(body.clone())).unwrap();
        body.insert("capsule_id".into(), json!(legacy_id));
        let legacy = Value::Object(body);
        ledger.append(&legacy, &statement_for(&legacy)).unwrap();
        assert!(ledger.cites_counterparty_half(&"b".repeat(64)));
        drop(ledger);

        // Rebuilt from disk on reopen -- both the follows and the legacy
        // cites records land in the set.
        let (reopened, _) = Ledger::open(dir.path()).unwrap();
        assert!(reopened.cites_counterparty_half(&"a".repeat(64)));
        assert!(reopened.cites_counterparty_half(&"b".repeat(64)));
        assert!(!reopened.cites_counterparty_half(&"c".repeat(64)));
    }
}
