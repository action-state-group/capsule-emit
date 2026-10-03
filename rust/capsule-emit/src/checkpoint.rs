//! Checkpointing over this crate's ledger (`<ledger_dir>/capsules.jsonl`):
//! an append-only MMR over `capsule_id` leaves, a periodic signed checkpoint,
//! and opt-in witness registration.
//!
//! The substrate is the `evidencebook` crate's
//! [`evidencebook::substrate::CllSubstrate`], which embeds the
//! checkpointed-local-log (`cll`): the MMR node store, the monotonicity and
//! rewrite guards, the root, the signature, the COSE wire form,
//! `checkpoints.jsonl` and inclusion proofs. The on-disk shape is the one the
//! Python checkpointed-local-log reference reads and writes, so the two can
//! share a `checkpoints.jsonl`. This module keeps the policy around it: which
//! `capsules.jsonl` lines to fold, when to cut (cadence, push-time coverage,
//! padding), what time a checkpoint commits to, and witness registration.
//!
//! **Durable node store.** MMR node hashes persist to
//! `<ledger_dir>/mmr_nodes.dat`, so a restart re-hashes only the leaves
//! appended since the last clean stop, not the whole ledger, which matters
//! for a long-running process. The resulting MMR (roots, peaks, proofs) is
//! the same as an in-memory rebuild.
//!
//! **Policy:** the age clock runs from the FIRST unwitnessed entry (never
//! from the last checkpoint, and never while the log is caught up: only if
//! there is new activity); a checkpoint is flushed on shutdown; witness
//! registration is best-effort and retried, and an unreachable witness never
//! blocks local checkpointing. See `tick`, `reconnect`,
//! `checkpoint_on_shutdown` and `retry_pending_witnesses`.
//!
//! **Signing goes only through the substrate's [`CheckpointSigner`]**: this
//! module never calls `ed25519_dalek::Signer` directly, so the caller's
//! already-loaded key (`capsule_emit::keys::KeyPair::signing_key`, which
//! implements it) is reused as-is, never a second key.
//! `tests/evidencebook_parity.rs` pins the output byte for byte.

use crate::anchor::{dispatch_base_for, AnchorClient, AnchorError};
use evidencebook::substrate::{record_id_from_hex, CllSubstrate, SubstrateError, RECORD_ID_LEN};
// Re-exported under this module's names so a caller of this crate names
// them without depending on `evidencebook` directly.
pub use evidencebook::substrate::{
    verify_inclusion, Checkpoint as CheckpointRecord, InclusionEvidence as InclusionProof,
    OpenReport, Signer as CheckpointSigner, WitnessEntry as CheckpointWitness,
};
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::Instant;

/// Witness receipts obtained after their checkpoint was written, one per line,
/// next to `checkpoints.jsonl` (which is append-only and holds checkpoints
/// only). A push-time cut is written before any witness sees it; its receipt
/// arrives on a later tick and is recorded here. Same role as the Python
/// ledger's witness-backfill entries: a checkpoint's effective witnesses are
/// its own plus every backfill for it, one per witness URL, later lines
/// winning. Read them with [`read_witness_backfills`] and
/// [`effective_witnesses`].
pub const WITNESS_BACKFILL_FILE: &str = "witness-backfill.jsonl";

/// One witness receipt for the checkpoint at `mmr_size` with `root`.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct WitnessBackfill {
    pub mmr_size: u64,
    pub root: String,
    pub witness: CheckpointWitness,
}

/// Every backfill recorded next to `checkpoints_path`, in file order. A
/// missing file is no backfills; a line that does not parse (a torn final
/// write) is skipped.
pub fn read_witness_backfills(checkpoints_path: &Path) -> Vec<WitnessBackfill> {
    let path = checkpoints_path.with_file_name(WITNESS_BACKFILL_FILE);
    let Ok(file) = File::open(path) else {
        return Vec::new();
    };
    BufReader::new(file)
        .lines()
        .map_while(Result::ok)
        .filter_map(|line| serde_json::from_str(&line).ok())
        .collect()
}

/// `cp`'s own witnesses merged with every backfill for it (same `mmr_size`
/// and `root`), one entry per witness URL: a backfill replaces an entry for
/// the same URL, and a new URL is appended, in file order.
pub fn effective_witnesses(
    cp: &CheckpointRecord,
    backfills: &[WitnessBackfill],
) -> Vec<CheckpointWitness> {
    let mut witnesses = cp.witnesses.clone();
    for b in backfills
        .iter()
        .filter(|b| b.mmr_size == cp.mmr_size && b.root == cp.root)
    {
        match witnesses.iter_mut().find(|w| w.ts_url == b.witness.ts_url) {
            Some(existing) => *existing = b.witness.clone(),
            None => witnesses.push(b.witness.clone()),
        }
    }
    witnesses
}

fn append_witness_backfill(
    checkpoints_path: &Path,
    backfill: &WitnessBackfill,
) -> std::io::Result<()> {
    use std::io::Write;
    let mut line = serde_json::to_string(backfill).map_err(std::io::Error::other)?;
    line.push('\n');
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(checkpoints_path.with_file_name(WITNESS_BACKFILL_FILE))?;
    file.write_all(line.as_bytes())?;
    file.sync_data()
}

#[derive(Debug, thiserror::Error)]
pub enum CheckpointStateError {
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("node store error: {0}")]
    NodeStore(String),
    #[error("checkpoints.jsonl error: {0}")]
    Store(String),
    #[error("mmr error: {0}")]
    Mmr(String),
    #[error("checkpoint error: {0}")]
    Checkpoint(String),
    #[error("signer error: {0}")]
    Signer(String),
    #[error(
        "durable node store at {path} indexes {stored_leaves} leaves but capsules.jsonl only has \
         {ledger_leaves} -- the node store is AHEAD of the ledger it is meant to index; refusing \
         to trust it rather than checkpoint a possibly-fabricated MMR state"
    )]
    NodeStoreAheadOfLedger {
        path: String,
        stored_leaves: u64,
        ledger_leaves: u64,
    },
    #[error(
        "durable node store leaf {leaf_index} is {stored_leaf} but capsules.jsonl's capsule_id at \
         that index ({capsule_id}) leaf-hashes to {expected_leaf} -- the chain has been REWRITTEN \
         under the node store (e.g. a migration/backfill that edited capsules.jsonl without \
         rebuilding mmr_nodes.dat); refusing to checkpoint a commitment over superseded leaves"
    )]
    NodeStoreDivergedFromLedger {
        leaf_index: u64,
        capsule_id: String,
        stored_leaf: String,
        expected_leaf: String,
    },
    #[error("capsules.jsonl line {line}: not valid JSON: {source}")]
    MalformedLine {
        line: usize,
        source: serde_json::Error,
    },
    #[error("capsules.jsonl line {line}: missing or non-string capsule_id field")]
    MissingCapsuleId { line: usize },
    #[error(
        "capsules.jsonl line {line}: capsule_id {capsule_id:?} is not {RECORD_ID_LEN} bytes of hex"
    )]
    BadCapsuleId { line: usize, capsule_id: String },
    #[error("cannot checkpoint an empty MMR (no leaves appended yet)")]
    EmptyMmr,
    #[error(
        "MMR size {current_size} is not greater than the previous checkpoint's size {prev_size} \
         -- monotonicity violated"
    )]
    RollbackSize { current_size: u64, prev_size: u64 },
    #[error(
        "MMR root at the previous checkpoint's size ({prev_size}) is {actual_root} but that \
         checkpoint recorded root {recorded_root} -- the log has been mutated since"
    )]
    RollbackRoot {
        prev_size: u64,
        actual_root: String,
        recorded_root: String,
    },
    #[error("capsule_id {capsule_id} is not in capsules.jsonl -- nothing to cover")]
    CapsuleNotInLedger { capsule_id: String },
    #[error("padding to the checkpoint bucket failed: {0}")]
    Padding(String),
    #[error(
        "padded the ledger to {padded_to} lines but only {folded} are readable in \
         capsules.jsonl -- refusing to cut an unpadded checkpoint"
    )]
    PaddingNotFolded { padded_to: u64, folded: u64 },
}

/// The substrate's errors, under this module's long-standing variants and
/// messages. A leaf mismatch is not converted here: only the caller knows
/// which `capsule_id` it checked, so it builds
/// [`CheckpointStateError::NodeStoreDivergedFromLedger`] itself.
impl From<SubstrateError> for CheckpointStateError {
    fn from(e: SubstrateError) -> Self {
        match e {
            SubstrateError::NodeStore(m) => Self::NodeStore(m),
            SubstrateError::Store(m) => Self::Store(m),
            SubstrateError::Mmr(m) => Self::Mmr(m),
            SubstrateError::Checkpoint(m) => Self::Checkpoint(m),
            SubstrateError::Signer(m) => Self::Signer(m),
            SubstrateError::EmptyMmr => Self::EmptyMmr,
            SubstrateError::RollbackSize {
                current_size,
                prev_size,
            } => Self::RollbackSize {
                current_size,
                prev_size,
            },
            SubstrateError::RollbackRoot {
                prev_size,
                actual_root,
                recorded_root,
            } => Self::RollbackRoot {
                prev_size,
                actual_root,
                recorded_root,
            },
            other @ (SubstrateError::LeafMismatch { .. }
            | SubstrateError::CutBeyondLog { .. }
            | SubstrateError::StalePrepared { .. }) => Self::Checkpoint(other.to_string()),
        }
    }
}

/// Appends padding records to the ledger this state indexes (Evidence Layer
/// -00 §12.1; see `crate::padding`). `CheckpointState` never writes
/// `capsules.jsonl` itself -- the ledger has one writer -- so the caller
/// hands it this hook, implemented over that one writer.
pub trait PaddingSink: Send {
    /// Under the ledger's writer lock, append padding records until its line
    /// count is a multiple of `bucket`; return that count. Appending nothing
    /// when it already is, so a retry or a restart never pads twice.
    fn pad_to_bucket(&self, bucket: u64) -> Result<u64, String>;
}

/// What [`CheckpointState::checkpoint_covering`] hands the push-a-bundle
/// sender: the signed checkpoint covering one leaf, that leaf's index, and
/// its inclusion proof against the checkpoint's `mmr_size`.
#[derive(Debug, Clone)]
pub struct Coverage {
    pub checkpoint: CheckpointRecord,
    pub leaf_index: u64,
    pub proof: InclusionProof,
    /// `true` when this call cut a new checkpoint, `false` when an existing
    /// one already covered the leaf (the coalescing case).
    pub cut_new: bool,
}

/// The wire JSON of an inclusion proof -- the exact field set
/// `scitt_cose.cll.InclusionProof.from_dict` reads.
pub fn inclusion_proof_json(proof: &InclusionProof) -> serde_json::Value {
    proof.to_wire_json()
}

/// Cadence/witness policy: the fields of the Python reference's
/// `capsule_emit.checkpoint.CheckpointConfig` this module acts on (age +
/// entry-count cadence; opt-in witness URLs).
#[derive(Debug, Clone)]
pub struct CheckpointCadenceConfig {
    pub cadence_entries: u64,
    pub cadence_seconds: u64,
    /// Witness registration is opt-in, always: empty means
    /// self-checkpointed only, no network.
    pub witness_urls: Vec<String>,
    /// `checkpoint_pad_bucket`: every checkpoint's leaf count
    /// (`mmr_leaf_count`, never `mmr_size`) is padded up to a multiple of
    /// this before it is cut, so two checkpoints reveal the record count
    /// between them only to within the bucket (Evidence Layer -00 §12.1).
    /// 0 turns padding off. Applies only when a [`PaddingSink`] is attached
    /// ([`CheckpointState::set_padding_sink`]) -- without one this state has
    /// no way to write the ledger and cuts at whatever size it has.
    pub pad_bucket: u64,
}

/// `checkpoint_pad_bucket`'s default.
pub const DEFAULT_PAD_BUCKET: u64 = 32;

impl Default for CheckpointCadenceConfig {
    /// 300 seconds (tighter than the Python reference's 900s default) and
    /// the reference `CheckpointConfig`'s `cadence_entries=100`.
    fn default() -> Self {
        Self {
            cadence_entries: 100,
            cadence_seconds: 300,
            witness_urls: Vec::new(),
            pad_bucket: DEFAULT_PAD_BUCKET,
        }
    }
}

/// The clock leg's allowance for the tick that measures it. A backlog that
/// arrives between ticks is first seen (and its age clock started) by the
/// next tick, a little after that tick's scheduled instant; the tick one
/// interval later then measures an age just short of `cadence_seconds` and,
/// without this allowance, waits a whole further interval. 5% of the cadence,
/// at least one second.
fn tick_slack_seconds(cfg: &CheckpointCadenceConfig) -> f64 {
    (cfg.cadence_seconds as f64 * 0.05).max(1.0)
}

/// True once `entries_since_last` reaches `cfg.cadence_entries`, or
/// `seconds_since_last` reaches `cfg.cadence_seconds` -- whichever comes
/// first. The age leg only ever applies with at least one unwitnessed entry:
/// `entries_since_last == 0` is always `false`, regardless of age -- an idle
/// log stays silent, never a heartbeat. Byte-for-byte port of
/// `cll.checkpoint.emit.due_for_checkpoint`.
fn due_for_checkpoint(
    cfg: &CheckpointCadenceConfig,
    entries_since_last: u64,
    seconds_since_last: Option<f64>,
) -> bool {
    if entries_since_last == 0 {
        return false;
    }
    if entries_since_last >= cfg.cadence_entries {
        return true;
    }
    seconds_since_last.is_some_and(|s| s >= cfg.cadence_seconds as f64)
}

/// What `CheckpointState::load` found while opening the durable node store
/// and catching it up to `capsules.jsonl` -- surfaced so a caller can log a
/// recovered torn write or a from-scratch rebuild rather than have it
/// happen invisibly.
#[derive(Debug, Default)]
pub struct LoadReport {
    pub node_store: Option<OpenReport>,
    pub leaves_indexed_this_load: u64,
}

/// The checkpoint state over one `ledger_dir`: an MMR over its
/// `capsules.jsonl`, plus the periodic signed checkpoint and (optional)
/// witness registration. One instance per `ledger_dir`.
pub struct CheckpointState {
    capsules_path: PathBuf,
    substrate: CllSubstrate,
    leaf_count: u64,
    /// Byte offset just past the last `capsules.jsonl` line folded into the
    /// MMR: `sync` reads only from here on, never the whole file again.
    consumed_bytes: u64,
    /// Byte offset where that last folded line starts -- re-read on every
    /// `sync` to catch a chain rewritten in place under the live state.
    last_line_start: u64,
    /// `capsule_id` -> leaf index for every folded leaf (the last occurrence
    /// wins), so locating a pushed half is a lookup, not a file scan.
    leaf_index_by_id: HashMap<String, u64>,
    log_id: String,
    cfg: CheckpointCadenceConfig,
    entries_since_checkpoint: u64,
    /// Instant of the FIRST currently-unwitnessed entry, or `None` when the
    /// log is fully caught up. Not restored across a restart (a monotonic
    /// `Instant` cannot be persisted meaningfully) -- an on-disk backlog at
    /// load time is treated as pending-as-of-now.
    pending_since: Option<Instant>,
    /// `witness_urls` from the last checkpoint's own registration attempt
    /// that have not yet succeeded.
    pending_witness_urls: Vec<String>,
    /// The latest checkpoint was cut at push time (see
    /// [`CheckpointState::checkpoint_covering`]) and has not been offered to
    /// a witness yet. Witness registration stays on the clock: the next
    /// [`CheckpointState::tick`] registers the LATEST checkpoint of the
    /// window once, never one registration per turn.
    witness_deferred: bool,
    /// See [`PaddingSink`]; `None` cuts unpadded.
    padding: Option<Box<dyn PaddingSink>>,
}

/// Divergence guard: verify the
/// durable node store still commits to the SAME leaves `capsules.jsonl`
/// holds -- not just to the same COUNT of leaves. The count-only trust was
/// the hole the received-half backfill incident exposed: a migration rewrote
/// the chain (same length, different capsule_ids at the tail) under an
/// existing `mmr_nodes.dat`, the count-based sync saw "nothing new to fold",
/// and the next tick would have emitted a checkpoint whose root committed to
/// the PRE-migration leaves. Sibling of `NodeStoreAheadOfLedger`/the
/// Rollback guards: a hard, descriptive error, never a silent stale
/// commitment.
///
/// `full = true` checks every stored leaf against the chain (used once, at
/// `load` -- one 32-byte read + one leaf hash per entry, cheap for a local
/// ledger and the only chance to catch a mid-chain rewrite that preserves
/// the tail). `full = false` checks only the LAST stored leaf (used on
/// every `sync`, so the per-tick cost is one node read + one hash).
fn verify_stored_leaves_match_chain(
    substrate: &CllSubstrate,
    stored_leaf_count: u64,
    capsule_ids: &[String],
    full: bool,
) -> Result<(), CheckpointStateError> {
    if stored_leaf_count == 0 {
        return Ok(());
    }
    debug_assert!(stored_leaf_count as usize <= capsule_ids.len());
    let first = if full { 0 } else { stored_leaf_count - 1 };
    for leaf_index in first..stored_leaf_count {
        check_leaf(substrate, leaf_index, &capsule_ids[leaf_index as usize])?;
    }
    Ok(())
}

/// The substrate's leaf check, reported as the divergence error this module
/// has always raised for a chain rewritten under the node store.
fn check_leaf(
    substrate: &CllSubstrate,
    leaf_index: u64,
    capsule_id: &str,
) -> Result<(), CheckpointStateError> {
    // read_complete_lines already validated every id as RECORD_ID_LEN bytes
    // of hex; this cannot fail.
    let record_id =
        record_id_from_hex(capsule_id).expect("read_complete_lines validates capsule_id hex");
    match substrate.check_leaf(leaf_index, &record_id) {
        Err(SubstrateError::LeafMismatch {
            leaf_index,
            stored_leaf,
            expected_leaf,
        }) => Err(CheckpointStateError::NodeStoreDivergedFromLedger {
            leaf_index,
            capsule_id: capsule_id.to_string(),
            stored_leaf,
            expected_leaf,
        }),
        other => Ok(other?),
    }
}

impl CheckpointState {
    /// Load (or create) checkpoint state for `ledger_dir`: opens the durable
    /// node store at `<ledger_dir>/mmr_nodes.dat`, catches it up to
    /// `<ledger_dir>/capsules.jsonl` (hashing only leaves added since the
    /// store was last synced -- see the module doc), and resumes from
    /// `<ledger_dir>/checkpoints.jsonl`'s last line if present.
    pub fn load(
        ledger_dir: &Path,
        log_id: impl Into<String>,
        cfg: CheckpointCadenceConfig,
    ) -> Result<(Self, LoadReport), CheckpointStateError> {
        let capsules_path = ledger_dir.join("capsules.jsonl");
        let checkpoints_path = ledger_dir.join("checkpoints.jsonl");
        let node_store_path = ledger_dir.join("mmr_nodes.dat");

        let (mut substrate, open_report) = CllSubstrate::open(&node_store_path, &checkpoints_path)?;
        let stored_leaf_count = substrate.leaf_count()?;

        let lines = read_complete_lines(&capsules_path, 0, 0)?;
        let capsule_ids: Vec<String> = lines.iter().map(|l| l.capsule_id.clone()).collect();
        if stored_leaf_count > capsule_ids.len() as u64 {
            return Err(CheckpointStateError::NodeStoreAheadOfLedger {
                path: node_store_path.display().to_string(),
                stored_leaves: stored_leaf_count,
                ledger_leaves: capsule_ids.len() as u64,
            });
        }
        // Full prefix check, once per process start: every leaf the durable
        // store already holds must still be the chain's capsule_id at that
        // index (see verify_stored_leaves_match_chain's doc for the incident
        // this guards against).
        verify_stored_leaves_match_chain(&substrate, stored_leaf_count, &capsule_ids, true)?;

        let mut leaves_indexed_this_load = 0u64;
        for capsule_id in &capsule_ids[stored_leaf_count as usize..] {
            // read_complete_lines already validated every id as RECORD_ID_LEN
            // bytes of hex; this cannot fail.
            let record_id = record_id_from_hex(capsule_id)
                .expect("read_complete_lines validates capsule_id hex");
            substrate.append_leaf(&record_id)?;
            leaves_indexed_this_load += 1;
        }
        let leaf_count = capsule_ids.len() as u64;
        let leaf_index_by_id = capsule_ids
            .iter()
            .enumerate()
            .map(|(i, id)| (id.clone(), i as u64))
            .collect();
        let (consumed_bytes, last_line_start) = lines.last().map_or((0, 0), |l| (l.end, l.start));

        if let Some(cp) = substrate.last_checkpoint().cloned() {
            let merged = effective_witnesses(&cp, &read_witness_backfills(&checkpoints_path));
            if let Some(witnesses) = substrate.last_checkpoint_witnesses_mut() {
                *witnesses = merged;
            }
        }
        let last_checkpoint_leaf_count = match substrate.last_checkpoint() {
            Some(cp) => cp.leaf_count()?,
            None => 0,
        };
        let entries_since_checkpoint = leaf_count.saturating_sub(last_checkpoint_leaf_count);

        let state = Self {
            capsules_path,
            substrate,
            leaf_count,
            consumed_bytes,
            last_line_start,
            leaf_index_by_id,
            log_id: log_id.into(),
            cfg,
            entries_since_checkpoint,
            pending_since: (entries_since_checkpoint > 0).then(Instant::now),
            pending_witness_urls: Vec::new(),
            witness_deferred: false,
            padding: None,
        };
        let report = LoadReport {
            node_store: Some(open_report),
            leaves_indexed_this_load,
        };
        Ok((state, report))
    }

    pub fn leaf_count(&self) -> u64 {
        self.leaf_count
    }

    /// Attach the ledger's padding hook: from now on every checkpoint this
    /// state cuts is padded to `cfg.pad_bucket` first (see [`PaddingSink`]).
    pub fn set_padding_sink(&mut self, sink: Box<dyn PaddingSink>) {
        self.padding = Some(sink);
    }

    /// The leaf count the next checkpoint is cut at. With padding on: pad
    /// the ledger to the bucket through the sink, fold what it wrote, and
    /// return the padded count -- the checkpoint is cut at EXACTLY that
    /// count, so a real record the ledger's writer appends between the
    /// padding and the cut (it holds its own lock, not this one) waits for
    /// the next checkpoint instead of knocking this one off the boundary.
    /// With padding off: every leaf folded so far.
    fn cut_leaf_count(&mut self) -> Result<u64, CheckpointStateError> {
        let bucket = self.cfg.pad_bucket;
        let Some(sink) = self.padding.as_ref().filter(|_| bucket > 0) else {
            return Ok(self.leaf_count);
        };
        let padded_to = sink
            .pad_to_bucket(bucket)
            .map_err(CheckpointStateError::Padding)?;
        self.sync()?;
        if self.leaf_count < padded_to {
            return Err(CheckpointStateError::PaddingNotFolded {
                padded_to,
                folded: self.leaf_count,
            });
        }
        Ok(padded_to)
    }

    pub fn last_checkpoint(&self) -> Option<&CheckpointRecord> {
        self.substrate.last_checkpoint()
    }

    /// Fold any `capsules.jsonl` lines not yet indexed into the MMR.
    /// Idempotent -- safe to call with nothing new to add. Reads only the NEW tail
    /// (from `consumed_bytes`) and re-hashes only new leaves -- O(new lines),
    /// never a re-parse of the whole ledger, since the push path calls this
    /// on every push.
    ///
    /// Divergence guards, per call: a file shorter than what was folded is
    /// `NodeStoreAheadOfLedger` (soft on `tick`, as before), and the last
    /// folded line is re-read and must still name the last stored leaf --
    /// a chain rewritten in place is `NodeStoreDivergedFromLedger` (`load`
    /// does the full prefix check). Only newline-terminated lines are
    /// folded: a line still being written is picked up by a later call.
    fn sync(&mut self) -> Result<u64, CheckpointStateError> {
        let file_len = match std::fs::metadata(&self.capsules_path) {
            Ok(meta) => meta.len(),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => 0,
            Err(err) => return Err(err.into()),
        };
        if file_len < self.consumed_bytes {
            return Err(self.ahead_of_ledger());
        }
        if self.leaf_count > 0 {
            self.check_last_folded_line()?;
        }
        let lines = read_complete_lines(
            &self.capsules_path,
            self.consumed_bytes,
            self.leaf_count as usize,
        )?;
        let mut added = 0u64;
        for line in lines {
            // read_complete_lines already validated every id as RECORD_ID_LEN
            // bytes of hex; this cannot fail.
            let record_id = record_id_from_hex(&line.capsule_id)
                .expect("read_complete_lines validates capsule_id hex");
            self.substrate.append_leaf(&record_id)?;
            self.leaf_index_by_id
                .insert(line.capsule_id, self.leaf_count + added);
            self.consumed_bytes = line.end;
            self.last_line_start = line.start;
            added += 1;
        }
        self.leaf_count += added;
        self.note_pending(added);
        Ok(added)
    }

    fn ahead_of_ledger(&self) -> CheckpointStateError {
        CheckpointStateError::NodeStoreAheadOfLedger {
            path: self.capsules_path.display().to_string(),
            stored_leaves: self.leaf_count,
            ledger_leaves: self.leaf_count.saturating_sub(1),
        }
    }

    /// The per-sync divergence check: the line at `last_line_start` must
    /// still be a complete line naming the last stored leaf. A missing or
    /// torn line there reads as the ledger being short (soft on tick); a
    /// different capsule_id is a rewritten chain (hard).
    fn check_last_folded_line(&mut self) -> Result<(), CheckpointStateError> {
        let leaf_index = self.leaf_count - 1;
        let line = read_complete_lines_limit(
            &self.capsules_path,
            self.last_line_start,
            leaf_index as usize,
            1,
        )?
        .into_iter()
        .next()
        .ok_or_else(|| self.ahead_of_ledger())?;
        check_leaf(&self.substrate, leaf_index, &line.capsule_id)?;
        // Same leaf; if the line's bytes changed length, resume after it.
        self.consumed_bytes = line.end;
        Ok(())
    }

    /// Checkpoint at push: return a signed checkpoint covering `capsule_id`
    /// plus that leaf's inclusion proof, cutting a new checkpoint only when
    /// the latest one does not already cover it (a burst of pushes shares
    /// one checkpoint; the caller paces cuts). A cut here is LOCAL ONLY: it is signed and
    /// persisted to `checkpoints.jsonl` exactly like a clock checkpoint, but
    /// never registered with a witness -- the next [`Self::tick`] registers
    /// the latest checkpoint of the window instead.
    pub fn checkpoint_covering(
        &mut self,
        capsule_id: &str,
        signer: &dyn CheckpointSigner,
        anchor: &AnchorClient,
    ) -> Result<Coverage, CheckpointStateError> {
        if let Some(coverage) = self.existing_coverage(capsule_id)? {
            return Ok(coverage);
        }
        self.checkpoint_now(signer, anchor, false)?;
        self.witness_deferred = true;
        let mut coverage = self
            .existing_coverage(capsule_id)?
            .expect("the checkpoint just cut covers every leaf synced before it");
        coverage.cut_new = true;
        Ok(coverage)
    }

    /// The coverage the LATEST checkpoint already gives `capsule_id`, or
    /// `None` when that leaf is newer than it (or there is no checkpoint
    /// yet). Never cuts -- the pacing caller checks this first, so a burst
    /// waits for one cut instead of cutting per push.
    pub fn existing_coverage(
        &mut self,
        capsule_id: &str,
    ) -> Result<Option<Coverage>, CheckpointStateError> {
        self.sync()?;
        let leaf_index = *self.leaf_index_by_id.get(capsule_id).ok_or_else(|| {
            CheckpointStateError::CapsuleNotInLedger {
                capsule_id: capsule_id.to_string(),
            }
        })?;
        let Some(checkpoint) = self.substrate.last_checkpoint().cloned() else {
            return Ok(None);
        };
        if leaf_index >= checkpoint.leaf_count()? {
            return Ok(None);
        }
        let proof = self
            .substrate
            .inclusion_at(leaf_index, checkpoint.mmr_size)?;
        Ok(Some(Coverage {
            checkpoint,
            leaf_index,
            proof,
            cut_new: false,
        }))
    }

    fn note_pending(&mut self, added: u64) {
        self.entries_since_checkpoint += added;
        if self.entries_since_checkpoint > 0 && self.pending_since.is_none() {
            self.pending_since = Some(Instant::now());
        }
    }

    /// The ~5-minute clock leg, called by the background cadence task on its
    /// interval (never on the serving path). Checkpoints if either cadence
    /// leg is due; otherwise retries any witness registration left pending
    /// by a prior outage.
    ///
    /// **`NodeStoreAheadOfLedger` is retryable-soft HERE, and only here.**
    /// The cadence task reads `capsules.jsonl` with no lock shared with the
    /// writer's `Ledger::append`, so a tick racing an in-flight append can
    /// observe a transiently-short chain (a torn tail the reader tolerates by
    /// dropping the partial line) and momentarily see the durable node store
    /// "ahead" of the file. That is a read artifact, not divergence -- the
    /// next tick re-reads the settled file and recovers -- so this leg logs
    /// and returns `Ok(None)` (retry next tick) instead of wedging the
    /// cadence on a sticky error. It stays a HARD error on [`Self::load`]
    /// (an ahead-state that persists across a process start is a really
    /// truncated ledger, never a torn read), and
    /// [`CheckpointStateError::NodeStoreDivergedFromLedger`] stays hard
    /// everywhere -- a rewritten chain must never be soft-retried into a
    /// stale commitment.
    pub fn tick(
        &mut self,
        signer: &dyn CheckpointSigner,
        anchor: &AnchorClient,
    ) -> Result<Option<CheckpointRecord>, CheckpointStateError> {
        match self.sync() {
            Ok(_) => {}
            Err(err @ CheckpointStateError::NodeStoreAheadOfLedger { .. }) => {
                eprintln!(
                    "[checkpoint] transiently-short capsules.jsonl read on tick ({err}) -- \
                     treating as a torn-tail read racing an append, retrying next tick"
                );
                return Ok(None);
            }
            Err(err) => return Err(err),
        }
        let seconds_since = self
            .pending_since
            .map(|t| t.elapsed().as_secs_f64() + tick_slack_seconds(&self.cfg));
        if due_for_checkpoint(&self.cfg, self.entries_since_checkpoint, seconds_since) {
            return Ok(Some(self.checkpoint_now(signer, anchor, true)?));
        }
        self.offer_deferred_to_witnesses();
        self.retry_pending_witnesses(anchor);
        Ok(None)
    }

    /// The clock leg of push-time checkpoints: a checkpoint cut at push was
    /// never offered to a witness, so the tick offers the LATEST one once
    /// (older push cuts in the window are covered by it through the
    /// prev_size/prev_root chain and its consistency proof). No-op when
    /// witnessing is off.
    fn offer_deferred_to_witnesses(&mut self) {
        if !self.witness_deferred {
            return;
        }
        self.witness_deferred = false;
        if self.pending_witness_urls.is_empty() {
            self.pending_witness_urls = self.cfg.witness_urls.clone();
        }
    }

    /// Latest-checkpoint-on-reconnect: commit everything accrued since the
    /// last witnessed checkpoint in ONE checkpoint, ignoring cadence. Call
    /// once at startup. Mirrors `CheckpointState.reconnect`.
    pub fn reconnect(
        &mut self,
        signer: &dyn CheckpointSigner,
        anchor: &AnchorClient,
    ) -> Result<Option<CheckpointRecord>, CheckpointStateError> {
        self.sync()?;
        if self.leaf_count == 0 {
            return Ok(None);
        }
        if let Some(last) = self.substrate.last_checkpoint() {
            let last_leaf_count = last.leaf_count()?;
            if self.leaf_count <= last_leaf_count {
                self.retry_pending_witnesses(anchor);
                return Ok(None);
            }
        }
        Ok(Some(self.checkpoint_now(signer, anchor, true)?))
    }

    /// On-shutdown flush: anchor any uncommitted backlog so the final
    /// interval isn't lost between the last tick and process exit. Same
    /// self-healing semantics as `reconnect`.
    pub fn checkpoint_on_shutdown(
        &mut self,
        signer: &dyn CheckpointSigner,
        anchor: &AnchorClient,
    ) -> Result<Option<CheckpointRecord>, CheckpointStateError> {
        self.reconnect(signer, anchor)
    }

    /// `register`: offer the new checkpoint to the configured witnesses now
    /// (the clock legs) or not (a push-time cut -- see
    /// [`Self::checkpoint_covering`]).
    fn checkpoint_now(
        &mut self,
        signer: &dyn CheckpointSigner,
        anchor: &AnchorClient,
        register: bool,
    ) -> Result<CheckpointRecord, CheckpointStateError> {
        let cut_leaves = self.cut_leaf_count()?;
        let mut prepared = self.substrate.prepare_checkpoint(
            cut_leaves,
            &self.log_id,
            &crate::timestamp::utc_now_minute(),
            signer,
        )?;
        // The COSE-wire form is best-effort: a build failure never blocks the
        // JSON checkpoint from being signed and persisted, it only means this
        // checkpoint stays self-attested (no witness registration possible
        // without the wire form).
        if let Some(err) = prepared.cose_error() {
            eprintln!(
                "[checkpoint] COSE-wire checkpoint serialization failed (staying JSON-only, \
                 self-attested): {err}"
            );
        }

        if register {
            let ts_urls = self.cfg.witness_urls.clone();
            let cose = prepared.cose().map(<[u8]>::to_vec);
            let still_pending = register_with(
                anchor,
                &mut prepared.checkpoint,
                cose.as_deref(),
                &ts_urls,
                &self.checkpoints_path(),
            );
            self.pending_witness_urls = still_pending;
            self.witness_deferred = false;
        }

        let cp = self.substrate.commit_checkpoint(prepared)?;
        // Leaves folded past the cut (a record that landed between the
        // padding and the cut) are the next checkpoint's backlog.
        self.entries_since_checkpoint = self.leaf_count - cut_leaves;
        if self.entries_since_checkpoint == 0 {
            self.pending_since = None;
        }
        Ok(cp)
    }

    /// `checkpoints.jsonl`, next to `capsules.jsonl`.
    fn checkpoints_path(&self) -> PathBuf {
        self.capsules_path.with_file_name("checkpoints.jsonl")
    }

    /// Retry registering the last checkpoint with whichever witness URLs are
    /// still pending from its original attempt -- called even when nothing
    /// new has landed, so a registration failure never stays unwitnessed
    /// forever just because the node goes idle right after. No-op when
    /// there is no checkpoint yet or nothing pending.
    pub fn retry_pending_witnesses(&mut self, anchor: &AnchorClient) -> bool {
        if self.pending_witness_urls.is_empty() {
            return false;
        }
        let Some(mut cp) = self.substrate.last_checkpoint().cloned() else {
            return false;
        };
        let ts_urls = std::mem::take(&mut self.pending_witness_urls);
        let before = cp.witnesses.len();
        let cose = self.substrate.last_checkpoint_cose().map(<[u8]>::to_vec);
        let still_pending = register_with(
            anchor,
            &mut cp,
            cose.as_deref(),
            &ts_urls,
            &self.checkpoints_path(),
        );
        self.pending_witness_urls = still_pending;
        let added = cp.witnesses.len() > before;
        // `checkpoints.jsonl` already holds this checkpoint without these
        // receipts; record them beside it so a reader (and a restart) sees
        // them. Best-effort, like registration itself.
        for witness in &cp.witnesses[before..] {
            let backfill = WitnessBackfill {
                mmr_size: cp.mmr_size,
                root: cp.root.clone(),
                witness: witness.clone(),
            };
            if let Err(err) = append_witness_backfill(&self.checkpoints_path(), &backfill) {
                eprintln!(
                    "[checkpoint] could not record the receipt from {} for checkpoint {}: {err}",
                    witness.ts_url, cp.mmr_size
                );
            }
        }
        if let Some(witnesses) = self.substrate.last_checkpoint_witnesses_mut() {
            *witnesses = cp.witnesses;
        }
        added
    }
}

/// Attempt registration with each of `ts_urls`, appending any success onto
/// `cp.witnesses` in place. Returns the subset still unregistered. Never
/// lets a network error propagate -- an unreachable witness leaves this
/// checkpoint self-checkpointed, retried later.
///
/// **Catch-up on a continuity refusal.** A witness refuses (409) a
/// checkpoint whose `prev_size`/`prev_root` is not the checkpoint IT last
/// accepted for this log, naming that one in the body. That happens
/// whenever the witness missed a checkpoint in this log's chain: push-time
/// cuts are never offered on their own, and a registration that failed
/// during an outage is superseded by the next checkpoint. When the named
/// checkpoint is in `checkpoints_path`, every later one is sent in order
/// (each chains from the one before, with its own consistency proof), then
/// `cp` again -- so the witness's view catches up to this log without
/// re-signing anything. A witness already at or past `cp` can never
/// register it and is dropped from pending; a witness holding a checkpoint
/// this log does not have stays pending, and the message says a log that
/// lost its local state must start a new log id.
fn register_with(
    anchor_default: &AnchorClient,
    cp: &mut CheckpointRecord,
    checkpoint_cose: Option<&[u8]>,
    ts_urls: &[String],
    checkpoints_path: &Path,
) -> Vec<String> {
    let mut still_pending = Vec::new();
    for ts_url in ts_urls {
        let Some(cose) = checkpoint_cose else {
            eprintln!("[checkpoint] skipping witness registration with {ts_url}: no COSE-wire checkpoint to send");
            still_pending.push(ts_url.clone());
            continue;
        };
        let dispatch_base = dispatch_base_for(ts_url);
        let client = if dispatch_base == ts_url {
            None
        } else {
            Some(AnchorClient::new(dispatch_base))
        };
        let client_ref = client.as_ref().unwrap_or(anchor_default);
        let mut result = client_ref.post_checkpoint_cose(cose);
        if let Err(err) = &result {
            if let Some((accepted_size, accepted_root)) = continuity_refusal(err) {
                if accepted_size >= cp.mmr_size {
                    // Only drop it when the witness's checkpoint is one of
                    // this log's own: then the witness is simply ahead on
                    // this chain. A size this log never checkpointed at (or
                    // a different root) is a different history.
                    let holds = (accepted_size == cp.mmr_size && accepted_root == cp.root)
                        || log_holds_checkpoint(checkpoints_path, accepted_size, &accepted_root);
                    if holds {
                        eprintln!(
                            "[checkpoint] {ts_url} already accepted this log's checkpoint at size \
                             {accepted_size}, at or past this one ({}); it cannot register this \
                             one, dropping it from pending",
                            cp.mmr_size
                        );
                        continue;
                    }
                    eprintln!(
                        "[checkpoint] {ts_url} holds a checkpoint at size {accepted_size} with root \
                         {accepted_root}, which this log does not hold (a fork, or a log restarted \
                         under a reused log id: a node that lost its local state must start a new \
                         log id); keeping it pending"
                    );
                } else {
                    match catch_up_witness(
                    client_ref,
                    checkpoints_path,
                    cp.mmr_size,
                    accepted_size,
                    &accepted_root,
                ) {
                    Ok(sent) => {
                        eprintln!(
                            "[checkpoint] caught {ts_url} up from size {accepted_size} with \
                             {sent} earlier checkpoint(s); resending size {}",
                            cp.mmr_size
                        );
                        result = client_ref.post_checkpoint_cose(cose);
                    }
                    Err(reason) => eprintln!(
                        "[checkpoint] could not catch {ts_url} up from size {accepted_size}: {reason}"
                    ),
                }
                }
            }
        }
        match result {
            Ok(resp) => {
                cp.witnesses.push(CheckpointWitness {
                    ts_url: ts_url.clone(),
                    entry_hash: resp.entry_hash,
                    receipt_b64: resp.receipt_b64,
                    leaf_index: resp.leaf_index,
                    tree_size: resp.tree_size,
                    is_stub: false,
                });
            }
            Err(err) => {
                eprintln!(
                    "[checkpoint] checkpoint registration with {ts_url} failed (staying \
                     self-checkpointed, retrying later): {err}"
                );
                still_pending.push(ts_url.clone());
            }
        }
    }
    still_pending
}

/// The witness's own last-accepted `(mmr_size, root)` from a 409
/// continuity refusal (`{"detail": {"last_accepted_mmr_size",
/// "last_accepted_root", ...}}`), or `None` for any other error.
fn continuity_refusal(err: &AnchorError) -> Option<(u64, String)> {
    let AnchorError::Status { status: 409, body } = err else {
        return None;
    };
    let value: serde_json::Value = serde_json::from_str(body).ok()?;
    let detail = value.get("detail")?;
    let size = detail.get("last_accepted_mmr_size")?.as_u64()?;
    let root = detail.get("last_accepted_root")?.as_str()?.to_string();
    Some((size, root))
}

/// Whether `checkpoints_path` holds a checkpoint at `size` with `root`.
fn log_holds_checkpoint(checkpoints_path: &Path, size: u64, root: &str) -> bool {
    cll::store::read_checkpoints(checkpoints_path).is_ok_and(|lines| {
        lines
            .iter()
            .any(|l| l.record.mmr_size == size && l.record.root == root)
    })
}

/// Send the witness every checkpoint in `checkpoints_path` after the one at
/// `(accepted_size, accepted_root)` and before `up_to_size`, in order,
/// stopping at the first failure. Returns how many were sent.
fn catch_up_witness(
    client: &AnchorClient,
    checkpoints_path: &Path,
    up_to_size: u64,
    accepted_size: u64,
    accepted_root: &str,
) -> Result<usize, String> {
    let lines = cll::store::read_checkpoints(checkpoints_path).map_err(|e| e.to_string())?;
    let start = lines
        .iter()
        .position(|l| l.record.mmr_size == accepted_size && l.record.root == accepted_root)
        .ok_or_else(|| {
            format!(
                "the witness accepted a checkpoint at size {accepted_size} with root \
                 {accepted_root}, which this log does not hold (a fork, or a log restarted \
                 under a reused log id: a node that lost its local state must start a new \
                 log id)"
            )
        })?;
    let mut sent = 0;
    for line in lines[start + 1..]
        .iter()
        .filter(|l| l.record.mmr_size < up_to_size)
    {
        let cose_hex = line.checkpoint_cose_hex.as_deref().ok_or_else(|| {
            format!(
                "the checkpoint at size {} has no COSE-wire form to send",
                line.record.mmr_size
            )
        })?;
        let cose = hex::decode(cose_hex).map_err(|e| e.to_string())?;
        client
            .post_checkpoint_cose(&cose)
            .map_err(|e| format!("sending size {}: {e}", line.record.mmr_size))?;
        sent += 1;
    }
    Ok(sent)
}

/// One newline-terminated `capsules.jsonl` line: its byte span and id.
struct LedgerLine {
    start: u64,
    end: u64,
    capsule_id: String,
}

/// Every complete (newline-terminated) line of `path` from byte `from` on.
/// See [`read_complete_lines_limit`].
fn read_complete_lines(
    path: &Path,
    from: u64,
    lines_before: usize,
) -> Result<Vec<LedgerLine>, CheckpointStateError> {
    read_complete_lines_limit(path, from, lines_before, usize::MAX)
}

/// Up to `limit` complete lines of `path` from byte `from` on, with their
/// byte spans. A trailing line with no newline yet is an append still in
/// flight: not returned, not an error. Blank lines are skipped. A complete
/// line that is not a JSON object with a hex `capsule_id` is a hard error
/// (`lines_before` only numbers the error message).
fn read_complete_lines_limit(
    path: &Path,
    from: u64,
    lines_before: usize,
    limit: usize,
) -> Result<Vec<LedgerLine>, CheckpointStateError> {
    let mut out = Vec::new();
    let mut file = match File::open(path) {
        Ok(f) => f,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(out),
        Err(err) => return Err(err.into()),
    };
    file.seek(SeekFrom::Start(from))?;
    let mut reader = BufReader::new(file);
    let mut offset = from;
    let mut buf = Vec::new();
    let mut line_no = lines_before;
    while out.len() < limit {
        buf.clear();
        let n = reader.read_until(b'\n', &mut buf)?;
        if n == 0 || buf.last() != Some(&b'\n') {
            break;
        }
        let start = offset;
        offset += n as u64;
        line_no += 1;
        let text = &buf[..n - 1];
        if text.iter().all(u8::is_ascii_whitespace) {
            continue;
        }
        let value: serde_json::Value =
            serde_json::from_slice(text).map_err(|source| CheckpointStateError::MalformedLine {
                line: line_no,
                source,
            })?;
        let capsule_id = value
            .get("capsule_id")
            .and_then(serde_json::Value::as_str)
            .ok_or(CheckpointStateError::MissingCapsuleId { line: line_no })?
            .to_string();
        if record_id_from_hex(&capsule_id).is_none() {
            return Err(CheckpointStateError::BadCapsuleId {
                line: line_no,
                capsule_id,
            });
        }
        out.push(LedgerLine {
            start,
            end: offset,
            capsule_id,
        });
    }
    Ok(out)
}

/// Read every `capsule_id` in `path`, in file order. Tolerates a torn
/// trailing line (`Ledger::append` writes+fsyncs a
/// whole line before returning, but a reader racing a still-in-progress
/// write could still observe a partial final line): only the LAST line gets
/// this tolerance -- real corruption anywhere else in the file is NOT this
/// case and is still a hard error, never silently dropped.
/// Test-only since the incremental sync: the whole-file reader the tests use
/// to inspect a ledger.
#[cfg(test)]
fn read_capsule_ids(path: &Path) -> Result<Vec<String>, CheckpointStateError> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let lines: Vec<String> = reader.lines().collect::<Result<_, _>>()?;
    let mut ids = Vec::with_capacity(lines.len());
    let last_index = lines.len().saturating_sub(1);
    for (i, line) in lines.iter().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let parsed: Result<serde_json::Value, _> = serde_json::from_str(line);
        let value = match parsed {
            Ok(v) => v,
            Err(_) if i == last_index => break,
            Err(source) => {
                return Err(CheckpointStateError::MalformedLine {
                    line: i + 1,
                    source,
                })
            }
        };
        let capsule_id = value
            .get("capsule_id")
            .and_then(serde_json::Value::as_str)
            .ok_or(CheckpointStateError::MissingCapsuleId { line: i + 1 })?
            .to_string();
        if record_id_from_hex(&capsule_id).is_none() {
            return Err(CheckpointStateError::BadCapsuleId {
                line: i + 1,
                capsule_id,
            });
        }
        ids.push(capsule_id);
    }
    Ok(ids)
}

#[cfg(test)]
mod tests {
    use super::*;
    use cll::checkpoint::verify_checkpoint_cose_offline;
    use cll::mmr::{
        add_leaf, consistency_proof, leaf_count as mmr_leaf_count, leaf_hash, peaks,
        root_from_peaks, Hash, NodeReader,
    };
    use ed25519_dalek::SigningKey;
    use serde_json::json;

    fn hex_to_digest(s: &str) -> Option<Hash> {
        record_id_from_hex(s)
    }

    fn leaf_positions_and_hashes(
        reader: &impl NodeReader,
        size: u64,
    ) -> Result<Vec<Hash>, cll::mmr::MmrError> {
        Ok(peaks(size)?.iter().map(|&p| reader.node(p)).collect())
    }

    /// The MMR over `capsules.jsonl`, rebuilt in memory independently of the
    /// substrate under test.
    fn independent_mmr(dir: &Path) -> cll::mmr::MemoryNodeStore {
        let mut mem = cll::mmr::MemoryNodeStore::new();
        for id in read_capsule_ids(&dir.join("capsules.jsonl")).unwrap() {
            add_leaf(&mut mem, leaf_hash(&hex_to_digest(&id).unwrap())).unwrap();
        }
        mem
    }

    fn write_capsule(dir: &Path, seed: &str) -> String {
        use sha2::{Digest, Sha256};
        let capsule_id = hex::encode(Sha256::digest(seed.as_bytes()));
        let line = json!({"capsule_id": capsule_id, "seed": seed});
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join("capsules.jsonl"))
            .unwrap();
        writeln!(f, "{}", serde_json::to_string(&line).unwrap()).unwrap();
        capsule_id
    }

    fn signer() -> SigningKey {
        SigningKey::from_bytes(&[7u8; 32])
    }

    #[test]
    fn empty_ledger_loads_with_no_pending_backlog() {
        let dir = tempfile::tempdir().unwrap();
        let (state, report) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        assert_eq!(state.leaf_count(), 0);
        assert!(state.last_checkpoint().is_none());
        assert_eq!(report.leaves_indexed_this_load, 0);
    }

    #[test]
    fn tick_below_cadence_does_not_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        let cfg = CheckpointCadenceConfig {
            cadence_entries: 100,
            cadence_seconds: 300,
            witness_urls: Vec::new(),
            pad_bucket: 0,
        };
        let (mut state, _) = CheckpointState::load(dir.path(), "test-log", cfg).unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let cp = state.tick(&signer(), &anchor).unwrap();
        assert!(
            cp.is_none(),
            "below both cadence legs -- must not checkpoint"
        );
    }

    #[test]
    fn reconnect_commits_backlog_immediately_ignoring_cadence() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        write_capsule(dir.path(), "two");
        let cfg = CheckpointCadenceConfig {
            cadence_entries: 100,
            cadence_seconds: 300,
            witness_urls: Vec::new(),
            pad_bucket: 0,
        };
        let (mut state, _) = CheckpointState::load(dir.path(), "test-log", cfg).unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let cp = state
            .reconnect(&signer(), &anchor)
            .unwrap()
            .expect("reconnect must commit the backlog");
        assert_eq!(cp.log_id, "test-log");
        assert!(cp.mmr_size > 0);
        assert!(
            cp.verify_signature_offline(),
            "self-signed checkpoint must verify offline"
        );
        assert_eq!(state.entries_since_checkpoint, 0);
    }

    #[test]
    fn only_if_new_activity_an_idle_reconnect_is_a_noop() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        let cfg = CheckpointCadenceConfig::default();
        let (mut state, _) = CheckpointState::load(dir.path(), "test-log", cfg.clone()).unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        state.reconnect(&signer(), &anchor).unwrap();

        // A second reconnect with nothing new must be a no-op -- no second
        // checkpoint for a caught-up log.
        let second = state.reconnect(&signer(), &anchor).unwrap();
        assert!(second.is_none());
    }

    #[test]
    fn restart_reuses_durable_node_store_without_rehashing_old_leaves() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        write_capsule(dir.path(), "two");
        {
            let (mut state, report) =
                CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                    .unwrap();
            assert_eq!(report.leaves_indexed_this_load, 2);
            let anchor = AnchorClient::new("http://127.0.0.1:1");
            state.reconnect(&signer(), &anchor).unwrap();
        }
        write_capsule(dir.path(), "three");
        // Second load: only the ONE new leaf should be (re)hashed, not the
        // two already durable in mmr_nodes.dat.
        let (state2, report2) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        assert_eq!(report2.leaves_indexed_this_load, 1);
        assert_eq!(state2.leaf_count(), 3);
    }

    #[test]
    fn checkpoint_root_matches_independent_mmr_computation() {
        let dir = tempfile::tempdir().unwrap();
        let ids = vec![
            write_capsule(dir.path(), "a"),
            write_capsule(dir.path(), "b"),
            write_capsule(dir.path(), "c"),
        ];
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let cp = state.reconnect(&signer(), &anchor).unwrap().unwrap();

        // Independently rebuild the MMR in memory from the same capsule_ids
        // and confirm the root matches -- the checkpoint's root is not just
        // "whatever the durable store produced", it is the actual MMRIVER
        // root over these exact leaves in this exact order.
        let mut mem = cll::mmr::MemoryNodeStore::new();
        for id in &ids {
            let digest = hex_to_digest(id).unwrap();
            add_leaf(&mut mem, leaf_hash(&digest)).unwrap();
        }
        let expected_root = hex::encode(root_from_peaks(
            &leaf_positions_and_hashes(&mem, mem.size()).unwrap(),
        ));
        assert_eq!(cp.root, expected_root);
    }

    #[test]
    fn cose_wire_checkpoint_verifies_offline_and_covers_consistency() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "a");
        write_capsule(dir.path(), "b");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        state.reconnect(&signer(), &anchor).unwrap();
        write_capsule(dir.path(), "c");
        let cp2 = state.reconnect(&signer(), &anchor).unwrap().unwrap();
        assert!(
            cp2.prev_size > 0,
            "second checkpoint must chain from the first"
        );

        let cose = state
            .substrate
            .last_checkpoint_cose()
            .expect("COSE bytes must have been built");
        let verified = verify_checkpoint_cose_offline(cose);
        assert!(
            verified.ok,
            "COSE checkpoint must verify offline: {:?}",
            verified.errors
        );
        let decoded = verified.decoded.unwrap();
        assert!(
            decoded.consistency_proof.is_some(),
            "chained checkpoint must carry a consistency proof"
        );
        assert_eq!(decoded.mmr_size, cp2.mmr_size);
        assert_eq!(decoded.root, cp2.root);
    }

    #[test]
    fn rollback_is_detected_not_silently_re_checkpointed() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "a");
        write_capsule(dir.path(), "b");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        state.reconnect(&signer(), &anchor).unwrap();

        // Simulate a mutated ledger: rewrite the persisted checkpoint so it
        // claims a root the current MMR does not actually have at that size
        // (the replace also rewrites that root inside the COSE wire form; only
        // the JSON record's root matters to the rollback check).
        let path = dir.path().join("checkpoints.jsonl");
        let text = std::fs::read_to_string(&path).unwrap();
        let recorded = state.last_checkpoint().unwrap().root.clone();
        std::fs::write(&path, text.replace(&recorded, &"f".repeat(64))).unwrap();
        drop(state);
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        assert_eq!(state.last_checkpoint().unwrap().root, "f".repeat(64));
        write_capsule(dir.path(), "c");
        state.sync().unwrap();
        let err = state.checkpoint_now(&signer(), &anchor, true).unwrap_err();
        assert!(matches!(err, CheckpointStateError::RollbackRoot { .. }));
    }

    #[test]
    fn empty_mmr_refuses_to_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let err = state.checkpoint_now(&signer(), &anchor, true).unwrap_err();
        assert!(matches!(err, CheckpointStateError::EmptyMmr));
    }

    #[test]
    fn node_store_ahead_of_ledger_is_a_hard_error() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "a");
        write_capsule(dir.path(), "b");
        {
            let (_state, _) =
                CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                    .unwrap();
        }
        // Truncate capsules.jsonl back to one entry -- the durable node
        // store now claims more leaves than the ledger actually has.
        let ids = read_capsule_ids(&dir.path().join("capsules.jsonl")).unwrap();
        assert_eq!(ids.len(), 2);
        let single = json!({"capsule_id": ids[0], "seed": "a"});
        std::fs::write(
            dir.path().join("capsules.jsonl"),
            format!("{}\n", serde_json::to_string(&single).unwrap()),
        )
        .unwrap();

        let err =
            match CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
            {
                Err(e) => e,
                Ok(_) => panic!("expected NodeStoreAheadOfLedger, load succeeded"),
            };
        assert!(matches!(
            err,
            CheckpointStateError::NodeStoreAheadOfLedger { .. }
        ));
    }

    /// The backfill-incident shape,
    /// live-state leg: a chain REWRITTEN in place (same leaf count,
    /// different tail capsule_id) under an already-synced state must make
    /// `tick` error -- never emit a checkpoint whose root commits to the
    /// superseded leaves.
    #[test]
    fn rewritten_chain_under_live_state_errors_on_sync_instead_of_emitting() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        write_capsule(dir.path(), "two");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        assert_eq!(state.leaf_count(), 2);

        // Rewrite the chain: keep entry one, replace entry two with a
        // DIFFERENT capsule -- same count, different leaf set (exactly what
        // the received-half backfill did to the run-5 ledger).
        let ids = read_capsule_ids(&dir.path().join("capsules.jsonl")).unwrap();
        let keep = json!({"capsule_id": ids[0], "seed": "one"});
        std::fs::write(
            dir.path().join("capsules.jsonl"),
            format!("{}\n", serde_json::to_string(&keep).unwrap()),
        )
        .unwrap();
        write_capsule(dir.path(), "two-rewritten");

        // `reconnect` (not `tick`): it checkpoints unconditionally on a
        // backlog, so WITHOUT the guard this call would emit a checkpoint
        // whose root commits to the pre-rewrite leaves -- the exact silent
        // wrong commitment the incident produced.
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let err = state.reconnect(&signer(), &anchor).unwrap_err();
        assert!(
            matches!(
                err,
                CheckpointStateError::NodeStoreDivergedFromLedger { leaf_index: 1, .. }
            ),
            "expected NodeStoreDivergedFromLedger, got: {err}"
        );
        assert!(
            !dir.path().join("checkpoints.jsonl").exists(),
            "a diverged store must never have emitted a checkpoint"
        );
    }

    /// Restart leg: the same rewrite
    /// discovered at `load` time -- including a MID-chain rewrite that
    /// preserves the tail, which only the full prefix check can see.
    #[test]
    fn rewritten_chain_under_existing_node_store_errors_on_load() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        write_capsule(dir.path(), "two");
        write_capsule(dir.path(), "three");
        {
            let (_state, _) =
                CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                    .unwrap();
        }
        // Rewrite the MIDDLE entry only -- first and last leaves unchanged,
        // so a last-leaf-only check would miss it.
        let ids = read_capsule_ids(&dir.path().join("capsules.jsonl")).unwrap();
        std::fs::write(dir.path().join("capsules.jsonl"), "").unwrap();
        let first = json!({"capsule_id": ids[0], "seed": "one"});
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(dir.path().join("capsules.jsonl"))
            .unwrap();
        writeln!(f, "{}", serde_json::to_string(&first).unwrap()).unwrap();
        drop(f);
        write_capsule(dir.path(), "two-rewritten");
        let last = json!({"capsule_id": ids[2], "seed": "three"});
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(dir.path().join("capsules.jsonl"))
            .unwrap();
        writeln!(f, "{}", serde_json::to_string(&last).unwrap()).unwrap();
        drop(f);

        let err =
            match CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
            {
                Err(e) => e,
                Ok(_) => panic!("expected NodeStoreDivergedFromLedger, load succeeded"),
            };
        assert!(
            matches!(
                err,
                CheckpointStateError::NodeStoreDivergedFromLedger { leaf_index: 1, .. }
            ),
            "expected NodeStoreDivergedFromLedger at leaf 1, got: {err}"
        );
    }

    /// The tick leg's torn-tail tolerance: a transiently-short chain read
    /// (the cadence racing `Ledger::append` with no shared lock) makes
    /// `tick` return `Ok(None)` -- a soft skip, never a wedge -- and the
    /// very next tick over the settled file recovers and checkpoints. The
    /// SAME state at `load` stays hard
    /// (`node_store_ahead_of_ledger_is_a_hard_error` above), and a rewritten
    /// chain stays hard on tick
    /// (`rewritten_chain_under_live_state_errors_on_sync_instead_of_emitting`).
    #[test]
    fn transiently_short_chain_read_is_soft_on_tick_and_recovers_next_tick() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        write_capsule(dir.path(), "two");
        let cfg = CheckpointCadenceConfig {
            cadence_entries: 1, // due immediately once sync succeeds
            cadence_seconds: 300,
            witness_urls: Vec::new(),
            pad_bucket: 0,
        };
        let (mut state, _) = CheckpointState::load(dir.path(), "test-log", cfg).unwrap();
        assert_eq!(state.leaf_count(), 2);

        // Simulate the torn-tail read: the reader observes only the first
        // line while the second append is in flight.
        let capsules_path = dir.path().join("capsules.jsonl");
        let settled = std::fs::read_to_string(&capsules_path).unwrap();
        let first_line = settled.lines().next().unwrap();
        std::fs::write(&capsules_path, format!("{first_line}\n")).unwrap();

        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let soft = state.tick(&signer(), &anchor).unwrap();
        assert!(
            soft.is_none(),
            "a transiently-short read must soft-skip the tick"
        );
        assert!(
            !dir.path().join("checkpoints.jsonl").exists(),
            "the soft-skipped tick must not have emitted a checkpoint"
        );

        // The write settles; the next tick recovers and checkpoints.
        std::fs::write(&capsules_path, settled).unwrap();
        let cp = state
            .tick(&signer(), &anchor)
            .unwrap()
            .expect("the next tick over the settled chain must checkpoint");
        assert_eq!(mmr_leaf_count(cp.mmr_size).unwrap(), 2);
    }

    #[test]
    fn torn_trailing_capsule_line_is_tolerated_not_a_hard_error() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "a");
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(dir.path().join("capsules.jsonl"))
            .unwrap();
        write!(f, "{{\"capsule_id\":\"dead").unwrap(); // no trailing newline: a torn write
        drop(f);

        let ids = read_capsule_ids(&dir.path().join("capsules.jsonl")).unwrap();
        assert_eq!(
            ids.len(),
            1,
            "the torn trailing line must be skipped, not raise"
        );
    }

    #[test]
    fn witness_registration_failure_leaves_checkpoint_self_checkpointed_and_pending() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "a");
        let cfg = CheckpointCadenceConfig {
            cadence_entries: 100,
            cadence_seconds: 300,
            // Port 1 refuses connections deterministically -- a stand-in
            // for an unreachable witness without a live network dependency.
            witness_urls: vec!["http://127.0.0.1:1/".to_string()],
            pad_bucket: 0,
        };
        let (mut state, _) = CheckpointState::load(dir.path(), "test-log", cfg).unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let cp = state
            .reconnect(&signer(), &anchor)
            .unwrap()
            .expect("checkpoint must still be produced");
        assert!(
            cp.witnesses.is_empty(),
            "an unreachable witness must not be recorded as succeeded"
        );
        assert_eq!(
            state.pending_witness_urls.len(),
            1,
            "the failed URL must be queued for retry"
        );
    }

    fn checkpoint_lines(dir: &Path) -> usize {
        cll::store::read_checkpoints(dir.join("checkpoints.jsonl"))
            .unwrap()
            .len()
    }

    fn assert_covers(coverage: &Coverage, capsule_id: &str) {
        let root: Hash = hex_to_digest(&coverage.checkpoint.root).unwrap();
        let body = hex_to_digest(capsule_id).unwrap();
        assert!(
            verify_inclusion(
                &root,
                coverage.checkpoint.mmr_size,
                coverage.leaf_index,
                &body,
                &coverage.proof,
            ),
            "the inclusion proof must reconstruct the covering checkpoint's root"
        );
        assert!(coverage.checkpoint.verify_signature_offline());
    }

    #[test]
    fn checkpoint_covering_cuts_a_checkpoint_whose_proof_verifies() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        let two = write_capsule(dir.path(), "two");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");

        let coverage = state.checkpoint_covering(&two, &signer(), &anchor).unwrap();
        assert!(coverage.cut_new);
        assert_eq!(coverage.leaf_index, 1);
        assert_covers(&coverage, &two);
        assert_eq!(
            checkpoint_lines(dir.path()),
            1,
            "a push cut is persisted like any checkpoint"
        );
        assert_eq!(state.entries_since_checkpoint, 0);
    }

    #[test]
    fn checkpoint_covering_reuses_a_checkpoint_that_already_covers_the_leaf() {
        let dir = tempfile::tempdir().unwrap();
        let one = write_capsule(dir.path(), "one");
        let two = write_capsule(dir.path(), "two");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");

        let first = state.checkpoint_covering(&two, &signer(), &anchor).unwrap();
        // A burst: the earlier leaf is already under the checkpoint just cut.
        let second = state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        assert!(
            !second.cut_new,
            "a covered leaf must not cut a second checkpoint"
        );
        assert_eq!(second.checkpoint, first.checkpoint);
        assert_covers(&second, &one);
        assert_eq!(checkpoint_lines(dir.path()), 1);
    }

    #[test]
    fn checkpoint_covering_a_capsule_not_in_the_ledger_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let err = state
            .checkpoint_covering(&"f".repeat(64), &signer(), &anchor)
            .unwrap_err();
        assert!(matches!(
            err,
            CheckpointStateError::CapsuleNotInLedger { .. }
        ));
        assert_eq!(checkpoint_lines(dir.path()), 0);
    }

    #[test]
    fn successive_push_cuts_chain_and_stay_consistent() {
        let dir = tempfile::tempdir().unwrap();
        let one = write_capsule(dir.path(), "one");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let first = state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        let two = write_capsule(dir.path(), "two");
        let three = write_capsule(dir.path(), "three");
        let second = state
            .checkpoint_covering(&three, &signer(), &anchor)
            .unwrap();
        assert!(second.cut_new);
        assert_eq!(second.checkpoint.prev_size, first.checkpoint.mmr_size);
        assert_eq!(second.checkpoint.prev_root, first.checkpoint.root);
        assert_covers(&second, &three);

        // The earlier checkpoint is a prefix of the later one.
        let proof = consistency_proof(
            &independent_mmr(dir.path()),
            first.checkpoint.mmr_size,
            second.checkpoint.mmr_size,
        )
        .unwrap();
        assert!(cll::mmr::verify_consistency(
            &hex_to_digest(&first.checkpoint.root).unwrap(),
            first.checkpoint.mmr_size,
            &hex_to_digest(&second.checkpoint.root).unwrap(),
            second.checkpoint.mmr_size,
            &proof,
        ));

        // A leaf cut under the first checkpoint still proves under the second.
        let reused = state.checkpoint_covering(&two, &signer(), &anchor).unwrap();
        assert!(!reused.cut_new);
        assert_covers(&reused, &two);
    }

    #[test]
    fn a_push_cut_is_not_witnessed_until_the_clock_offers_the_latest_one() {
        let dir = tempfile::tempdir().unwrap();
        let one = write_capsule(dir.path(), "one");
        let cfg = CheckpointCadenceConfig {
            cadence_entries: 100,
            cadence_seconds: 300,
            // Unreachable: any registration attempt fails and stays pending,
            // which is how this test observes whether one was made.
            witness_urls: vec!["http://127.0.0.1:1".to_string()],
            pad_bucket: 0,
        };
        let (mut state, _) = CheckpointState::load(dir.path(), "test-log", cfg).unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");

        state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        assert!(
            state.pending_witness_urls.is_empty(),
            "a push cut must never register with a witness per turn"
        );
        assert!(state.witness_deferred);

        // The clock leg: nothing new to cut, but the deferred checkpoint is
        // offered once (it fails against the unreachable URL and stays queued).
        assert!(state.tick(&signer(), &anchor).unwrap().is_none());
        assert!(!state.witness_deferred);
        assert_eq!(
            state.pending_witness_urls,
            vec!["http://127.0.0.1:1".to_string()]
        );
        assert_eq!(
            checkpoint_lines(dir.path()),
            1,
            "the tick must not cut a second checkpoint"
        );
    }

    #[test]
    fn after_a_restart_a_covered_leaf_reuses_the_persisted_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        let one = write_capsule(dir.path(), "one");
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        {
            let (mut state, _) =
                CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                    .unwrap();
            state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        }
        let (mut reopened, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let coverage = reopened
            .checkpoint_covering(&one, &signer(), &anchor)
            .unwrap();
        assert!(!coverage.cut_new);
        assert_covers(&coverage, &one);
        assert_eq!(checkpoint_lines(dir.path()), 1);
    }

    /// S4: a push must not re-parse the whole ledger. Proof by
    /// behavior: after the first sync, the already-folded prefix is never
    /// read again -- so an in-place change to an early line (which a full
    /// re-parse would trip over) does not stop covering a NEW leaf.
    #[test]
    fn s4_covering_a_new_leaf_reads_only_the_new_tail() {
        let dir = tempfile::tempdir().unwrap();
        for i in 0..50 {
            write_capsule(dir.path(), &format!("early-{i}"));
        }
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        // Garble the FIRST line's bytes in place (same length, no longer JSON).
        let path = dir.path().join("capsules.jsonl");
        let mut bytes = std::fs::read(&path).unwrap();
        bytes[0] = b'#';
        std::fs::write(&path, &bytes).unwrap();

        let fresh = write_capsule(dir.path(), "fresh");
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        let coverage = state
            .checkpoint_covering(&fresh, &signer(), &anchor)
            .unwrap();
        assert_eq!(coverage.leaf_index, 50);
        assert_covers(&coverage, &fresh);
    }

    #[test]
    fn s4_a_line_still_being_written_is_folded_once_its_newline_lands() {
        let dir = tempfile::tempdir().unwrap();
        write_capsule(dir.path(), "one");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        use sha2::{Digest, Sha256};
        use std::io::Write;
        let id = hex::encode(Sha256::digest(b"two"));
        let line = serde_json::to_string(&json!({"capsule_id": id})).unwrap();
        let path = dir.path().join("capsules.jsonl");
        let (head, tail) = line.split_at(10);
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        write!(f, "{head}").unwrap();
        assert_eq!(state.sync().unwrap(), 0, "a torn tail is not folded");
        writeln!(f, "{tail}").unwrap();
        assert_eq!(state.sync().unwrap(), 1);
        assert_eq!(state.leaf_count(), 2);
        assert_eq!(state.leaf_index_by_id.get(&id), Some(&1));
    }

    /// A minimal witness with capsule-anchor's continuity rule: the first
    /// checkpoint for a log is accepted; a later one only if its
    /// `prev_size`/`prev_root` equal the last one accepted, else 409 naming
    /// that one. `tip` seeds the witness's state (a witness that already
    /// holds some checkpoint). Records the sizes it accepted.
    struct ContinuityWitness {
        url: String,
        accepted: std::sync::Arc<std::sync::Mutex<Vec<u64>>>,
    }

    fn continuity_witness(tip: Option<(u64, String)>) -> ContinuityWitness {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let accepted = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let seen = accepted.clone();
        std::thread::spawn(move || {
            let mut tip = tip;
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let mut buf = Vec::new();
                let mut chunk = [0u8; 8192];
                let body = loop {
                    let n = stream.read(&mut chunk).unwrap_or(0);
                    if n == 0 {
                        break None;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") else {
                        continue;
                    };
                    let head = String::from_utf8_lossy(&buf[..end]).to_lowercase();
                    let len: usize = head
                        .lines()
                        .find_map(|l| l.strip_prefix("content-length:"))
                        .map_or(0, |v| v.trim().parse().unwrap());
                    if buf.len() >= end + 4 + len {
                        break Some(buf[end + 4..end + 4 + len].to_vec());
                    }
                };
                let Some(body) = body else { continue };
                let decoded = verify_checkpoint_cose_offline(&body).decoded.unwrap();
                let chains = match &tip {
                    None => true,
                    Some((size, root)) => decoded.prev_size == *size && decoded.prev_root == *root,
                };
                let (status, payload) = if chains {
                    tip = Some((decoded.mmr_size, decoded.root.clone()));
                    seen.lock().unwrap().push(decoded.mmr_size);
                    let entry_hash = "ab".repeat(32);
                    (
                        "200 OK",
                        json!({"receipt_b64": "c3R1Yg==", "entry_hash": entry_hash,
                               "leaf_index": 0, "tree_size": 1})
                        .to_string(),
                    )
                } else {
                    let (size, root) = tip.clone().unwrap();
                    (
                        "409 Conflict",
                        json!({"detail": {"error": "does not chain",
                               "last_accepted_mmr_size": size, "last_accepted_root": root}})
                        .to_string(),
                    )
                };
                let _ = write!(
                    stream,
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                    payload.len()
                );
            }
        });
        ContinuityWitness { url, accepted }
    }

    fn witnessed_cfg(url: &str) -> CheckpointCadenceConfig {
        CheckpointCadenceConfig {
            cadence_entries: 100,
            cadence_seconds: 300,
            witness_urls: vec![url.to_string()],
            pad_bucket: 0,
        }
    }

    #[test]
    fn a_witness_that_missed_push_cuts_is_caught_up_in_order() {
        let witness = continuity_witness(None);
        let dir = tempfile::tempdir().unwrap();
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", witnessed_cfg(&witness.url)).unwrap();
        let anchor = AnchorClient::new(&witness.url);

        // A: cut at push, offered on the next tick, accepted (first seen).
        let one = write_capsule(dir.path(), "one");
        state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        state.tick(&signer(), &anchor).unwrap();
        let a = state.last_checkpoint().unwrap().mmr_size;
        assert_eq!(*witness.accepted.lock().unwrap(), vec![a]);

        // B and C: two push cuts; the tick offers only the latest, C, whose
        // prev is B -- a checkpoint the witness never saw.
        let two = write_capsule(dir.path(), "two");
        state.checkpoint_covering(&two, &signer(), &anchor).unwrap();
        let b = state.last_checkpoint().unwrap().mmr_size;
        let three = write_capsule(dir.path(), "three");
        state
            .checkpoint_covering(&three, &signer(), &anchor)
            .unwrap();
        let c = state.last_checkpoint().unwrap().mmr_size;
        state.tick(&signer(), &anchor).unwrap();

        assert_eq!(*witness.accepted.lock().unwrap(), vec![a, b, c]);
        assert!(state.pending_witness_urls.is_empty());
        let latest = state.last_checkpoint().unwrap();
        assert_eq!(latest.witnesses.len(), 1);
        assert_eq!(latest.witnesses[0].ts_url, witness.url);
    }

    #[test]
    fn a_backlog_first_seen_by_a_tick_is_due_one_interval_later() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = CheckpointCadenceConfig {
            cadence_entries: 100,
            cadence_seconds: 300,
            witness_urls: Vec::new(),
            pad_bucket: 0,
        };
        let (mut state, _) = CheckpointState::load(dir.path(), "test-log", cfg).unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        write_capsule(dir.path(), "one");
        // This tick sees the new leaf and starts its age clock.
        assert!(state.tick(&signer(), &anchor).unwrap().is_none());
        assert!(state.pending_since.is_some());

        // Clearly younger than the cadence: not due.
        state.pending_since = Some(Instant::now() - std::time::Duration::from_secs(250));
        assert!(state.tick(&signer(), &anchor).unwrap().is_none());

        // The next tick fires one interval after the scheduled instant of the
        // one that started the clock, so the age it measures is a hair short.
        state.pending_since = Some(Instant::now() - std::time::Duration::from_millis(299_990));
        let cp = state.tick(&signer(), &anchor).unwrap();
        assert!(
            cp.is_some(),
            "a backlog one interval old must be cut, not left for the next tick"
        );
        assert!(state.pending_since.is_none());
    }

    #[test]
    fn a_push_cut_receipt_from_a_later_tick_is_kept_beside_the_checkpoint() {
        let witness = continuity_witness(None);
        let dir = tempfile::tempdir().unwrap();
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", witnessed_cfg(&witness.url)).unwrap();
        let anchor = AnchorClient::new(&witness.url);
        let one = write_capsule(dir.path(), "one");
        state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        state.tick(&signer(), &anchor).unwrap();
        let cp = state.last_checkpoint().unwrap().clone();
        assert_eq!(
            cp.witnesses.len(),
            1,
            "in memory, the receipt is on the checkpoint"
        );

        // On disk, the checkpoint line predates the receipt...
        let checkpoints = dir.path().join("checkpoints.jsonl");
        let on_disk = cll::store::read_last_checkpoint(&checkpoints)
            .unwrap()
            .unwrap();
        assert!(on_disk.record.witnesses.is_empty());
        // ...and the receipt is recorded beside it, for that checkpoint.
        let backfills = read_witness_backfills(&checkpoints);
        assert_eq!(backfills.len(), 1);
        assert_eq!(
            (backfills[0].mmr_size, backfills[0].root.as_str()),
            (cp.mmr_size, cp.root.as_str())
        );
        assert_eq!(backfills[0].witness.ts_url, witness.url);

        // A restart sees it, and a later tick does not register again.
        drop(state);
        let (mut reloaded, _) =
            CheckpointState::load(dir.path(), "test-log", witnessed_cfg(&witness.url)).unwrap();
        assert_eq!(reloaded.last_checkpoint().unwrap().witnesses.len(), 1);
        reloaded.tick(&signer(), &anchor).unwrap();
        assert_eq!(*witness.accepted.lock().unwrap(), vec![cp.mmr_size]);
        assert_eq!(read_witness_backfills(&checkpoints).len(), 1);
    }

    #[test]
    fn effective_witnesses_merges_only_this_checkpoints_backfills() {
        let entry = |url: &str, receipt: &str| CheckpointWitness {
            ts_url: url.to_string(),
            entry_hash: "ab".repeat(32),
            receipt_b64: receipt.to_string(),
            leaf_index: 0,
            tree_size: 1,
            is_stub: false,
        };
        let dir = tempfile::tempdir().unwrap();
        let one = write_capsule(dir.path(), "one");
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", CheckpointCadenceConfig::default())
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        let mut cp = state.last_checkpoint().unwrap().clone();
        cp.witnesses = vec![entry("https://a.example", "old")];
        let backfills = vec![
            WitnessBackfill {
                mmr_size: cp.mmr_size,
                root: cp.root.clone(),
                witness: entry("https://a.example", "new"),
            },
            WitnessBackfill {
                mmr_size: cp.mmr_size,
                root: cp.root.clone(),
                witness: entry("https://b.example", "b"),
            },
            WitnessBackfill {
                mmr_size: cp.mmr_size + 1,
                root: cp.root.clone(),
                witness: entry("https://c.example", "other size"),
            },
            WitnessBackfill {
                mmr_size: cp.mmr_size,
                root: "00".repeat(32),
                witness: entry("https://d.example", "other root"),
            },
        ];
        let merged = effective_witnesses(&cp, &backfills);
        let got: Vec<(&str, &str)> = merged
            .iter()
            .map(|w| (w.ts_url.as_str(), w.receipt_b64.as_str()))
            .collect();
        assert_eq!(
            got,
            vec![("https://a.example", "new"), ("https://b.example", "b")]
        );
    }

    #[test]
    fn a_witness_holding_a_checkpoint_this_log_lacks_stays_pending() {
        let witness = continuity_witness(Some((1, "cd".repeat(32))));
        let dir = tempfile::tempdir().unwrap();
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", witnessed_cfg(&witness.url)).unwrap();
        let anchor = AnchorClient::new(&witness.url);
        let one = write_capsule(dir.path(), "one");
        state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        let two = write_capsule(dir.path(), "two");
        state.checkpoint_covering(&two, &signer(), &anchor).unwrap();
        state.tick(&signer(), &anchor).unwrap();

        assert!(witness.accepted.lock().unwrap().is_empty());
        assert_eq!(state.pending_witness_urls, vec![witness.url.clone()]);
    }

    #[test]
    fn a_witness_at_a_larger_size_this_log_never_held_stays_pending() {
        // A lost-state or forked node: the witness holds a later checkpoint
        // under this log id that is not one of this log's own. It must stay
        // pending (with the new-log-id message), never be silently dropped.
        let witness = continuity_witness(Some((1_000, "cd".repeat(32))));
        let dir = tempfile::tempdir().unwrap();
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", witnessed_cfg(&witness.url)).unwrap();
        let anchor = AnchorClient::new(&witness.url);
        let one = write_capsule(dir.path(), "one");
        state.checkpoint_covering(&one, &signer(), &anchor).unwrap();
        state.tick(&signer(), &anchor).unwrap();

        assert!(witness.accepted.lock().unwrap().is_empty());
        assert_eq!(state.pending_witness_urls, vec![witness.url.clone()]);
    }

    /// Cut three checkpoints (no witness), return the dir and their lines.
    fn three_local_checkpoints() -> (tempfile::TempDir, Vec<cll::store::CheckpointLine>) {
        let dir = tempfile::tempdir().unwrap();
        let (mut state, _) =
            CheckpointState::load(dir.path(), "test-log", witnessed_cfg("http://127.0.0.1:1"))
                .unwrap();
        let anchor = AnchorClient::new("http://127.0.0.1:1");
        for seed in ["one", "two", "three"] {
            let id = write_capsule(dir.path(), seed);
            state.checkpoint_covering(&id, &signer(), &anchor).unwrap();
        }
        let lines = cll::store::read_checkpoints(dir.path().join("checkpoints.jsonl")).unwrap();
        assert_eq!(lines.len(), 3);
        (dir, lines)
    }

    fn register_line(
        witness: &ContinuityWitness,
        dir: &Path,
        line: &cll::store::CheckpointLine,
    ) -> Vec<String> {
        let mut cp: CheckpointRecord =
            serde_json::from_value(serde_json::to_value(&line.record).unwrap()).unwrap();
        let cose = hex::decode(line.checkpoint_cose_hex.as_ref().unwrap()).unwrap();
        register_with(
            &AnchorClient::new(&witness.url),
            &mut cp,
            Some(&cose),
            std::slice::from_ref(&witness.url),
            &dir.join("checkpoints.jsonl"),
        )
    }

    #[test]
    fn a_witness_ahead_on_this_logs_own_chain_is_dropped_from_pending() {
        let (dir, lines) = three_local_checkpoints();
        // The witness already holds this log's own later checkpoint (the
        // third); offering the second can never register there.
        let third = &lines[2].record;
        let witness = continuity_witness(Some((third.mmr_size, third.root.clone())));
        assert!(register_line(&witness, dir.path(), &lines[1]).is_empty());
        // The same when it holds exactly the checkpoint offered (its earlier
        // acceptance's response was lost).
        let witness = continuity_witness(Some((third.mmr_size, third.root.clone())));
        assert!(register_line(&witness, dir.path(), &lines[2]).is_empty());
        assert!(witness.accepted.lock().unwrap().is_empty());
    }

    #[test]
    fn continuity_refusal_reads_only_a_409_with_the_last_accepted_checkpoint() {
        let body = json!({"detail": {"error": "x", "last_accepted_mmr_size": 7,
                                     "last_accepted_root": "ef"}})
        .to_string();
        assert_eq!(
            continuity_refusal(&AnchorError::Status {
                status: 409,
                body: body.clone()
            }),
            Some((7, "ef".to_string()))
        );
        assert_eq!(
            continuity_refusal(&AnchorError::Status { status: 400, body }),
            None
        );
        assert_eq!(
            continuity_refusal(&AnchorError::Status {
                status: 409,
                body: "nope".into()
            }),
            None
        );
        assert_eq!(
            continuity_refusal(&AnchorError::Transport("down".into())),
            None
        );
    }
}
