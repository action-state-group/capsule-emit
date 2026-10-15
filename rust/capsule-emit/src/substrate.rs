//! The commitment substrate under [`crate::checkpoint`]: a
//! checkpointed-local-log (`cll`) MMR with a durable file-backed node store
//! and a `checkpoints.jsonl` checkpoint file, as the Python producer keeps
//! them.
//!
//! The MMR, the node store, the checkpoint signing body and signature, the
//! COSE wire form and the `checkpoints.jsonl` line format are all `cll`'s.
//! This module adds only the guards a store keeps around them (a cut must
//! grow the log, the log must still hold the root the last checkpoint
//! recorded, a prepared checkpoint must not fork the chain, a stored leaf
//! must commit the record the ledger holds at that index) and this crate's
//! public checkpoint types.
//!
//! **Public types.** [`CheckpointRecord`], [`CheckpointWitness`] and
//! [`InclusionProof`] carry the same fields and wire shape as `cll`'s
//! `CheckpointRecord`, `WitnessRecord` and `InclusionProof`, so a verifier
//! reads them unchanged, and convert to them at this module's boundary.
//! [`CheckpointSigner`] is this crate's signing interface. They are their
//! own types, not re-exports, so this crate's public API does not move with
//! `cll`'s.
//!
//! **Cutting a checkpoint is two steps.** [`CllSubstrate::prepare_checkpoint`]
//! checks monotonicity, computes the root, signs, and builds the COSE wire
//! form; the caller may then attach witness receipts to the prepared
//! checkpoint (registration needs the wire bytes) before
//! [`CllSubstrate::commit_checkpoint`] persists it. Checkpoint time is the
//! caller's: coarsening it is a privacy decision of the store, not of the log.

use cll::checkpoint::{
    checkpoint_to_cose, try_sign_checkpoint_digest, CheckpointRecord as LogCheckpoint,
    CheckpointSigner as LogSigner, Signature as LogSignature, SignerError as LogSignerError,
    WitnessRecord as LogWitness,
};
use cll::mmr::{
    add_leaf, consistency_proof, inclusion_proof, leaf_count as mmr_leaf_count, leaf_hash,
    leaf_index_to_pos, node_count, peaks, root_from_peaks, Hash, InclusionProof as LogInclusion,
    NodeReader,
};
use cll::node_store::FileNodeStore;
use cll::store::{append_checkpoint, read_last_checkpoint, CheckpointLine};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// Byte length of a record id (a `capsule_id`, a SHA-256 digest).
pub(crate) const RECORD_ID_LEN: usize = 32;

/// A record id as the substrate commits it.
pub(crate) type RecordId = [u8; RECORD_ID_LEN];

/// The `kind` of every checkpoint this substrate cuts.
const CHECKPOINT_KIND: &str = "mmr_checkpoint";

/// Parse a record id from lowercase or uppercase hex; `None` unless it is
/// exactly [`RECORD_ID_LEN`] bytes.
pub(crate) fn record_id_from_hex(s: &str) -> Option<RecordId> {
    hex::decode(s).ok()?.try_into().ok()
}

/// Signs checkpoint digests. Implemented for a local Ed25519 key; a remote or
/// hardware signer implements it too.
pub trait CheckpointSigner {
    /// Sign `digest` (the checkpoint's hex digest, as bytes).
    fn sign(&self, digest: &[u8]) -> Result<[u8; 64], CheckpointSignError>;

    /// The signer's key id: the raw Ed25519 public key, hex.
    fn key_id(&self) -> String;
}

impl CheckpointSigner for ed25519_dalek::SigningKey {
    fn sign(&self, digest: &[u8]) -> Result<[u8; 64], CheckpointSignError> {
        Ok(ed25519_dalek::Signer::sign(self, digest).to_bytes())
    }

    fn key_id(&self) -> String {
        hex::encode(self.verifying_key().to_bytes())
    }
}

/// Why a [`CheckpointSigner`] could not sign.
#[derive(Debug, thiserror::Error)]
#[error("signing failed: {0}")]
pub struct CheckpointSignError(pub String);

/// Adapts a [`CheckpointSigner`] to the log's own signer interface.
struct SignerAdapter<'a>(&'a dyn CheckpointSigner);

impl LogSigner for SignerAdapter<'_> {
    fn sign(&self, digest: &[u8]) -> Result<LogSignature, LogSignerError> {
        self.0
            .sign(digest)
            .map(LogSignature)
            .map_err(|e| LogSignerError::Failed(e.0))
    }

    fn key_id(&self) -> String {
        self.0.key_id()
    }
}

/// One witness receipt attached to a checkpoint. Same fields and wire shape
/// as the log's witness record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CheckpointWitness {
    pub ts_url: String,
    /// sha256(bytes.fromhex(checkpoint_digest)).hex() -- witness-derived.
    pub entry_hash: String,
    /// base64-encoded COSE Receipt.
    pub receipt_b64: String,
    pub leaf_index: i64,
    pub tree_size: i64,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub is_stub: bool,
}

/// A signed checkpoint: one committed state of the log. Same fields and wire
/// shape as the log's checkpoint record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CheckpointRecord {
    pub v: u32,
    pub kind: String,
    pub log_id: String,
    pub mmr_size: u64,
    /// Hex MMR root at `mmr_size`.
    pub root: String,
    /// 0 for the first checkpoint.
    pub prev_size: u64,
    /// Hex root at `prev_size`; empty for the first checkpoint.
    pub prev_root: String,
    pub key_id: String,
    pub timestamp: String,
    /// Hex Ed25519 signature over [`CheckpointRecord::digest`].
    pub signature: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub witnesses: Vec<CheckpointWitness>,
}

impl CheckpointRecord {
    /// Records this checkpoint covers.
    pub fn leaf_count(&self) -> Result<u64, SubstrateError> {
        mmr_leaf_count(self.mmr_size).log_err()
    }

    /// The canonical JSON the signature covers.
    pub fn signing_body(&self) -> String {
        self.to_log().signing_body()
    }

    /// Hex SHA-256 of [`CheckpointRecord::signing_body`]: what is signed and
    /// what a witness registers.
    pub fn digest(&self) -> String {
        self.to_log().digest()
    }

    /// Verify `signature` from this record alone (key from `key_id`).
    pub fn verify_signature_offline(&self) -> bool {
        self.to_log().verify_signature_offline()
    }

    fn to_log(&self) -> LogCheckpoint {
        LogCheckpoint {
            v: self.v,
            kind: self.kind.clone(),
            log_id: self.log_id.clone(),
            mmr_size: self.mmr_size,
            root: self.root.clone(),
            prev_size: self.prev_size,
            prev_root: self.prev_root.clone(),
            key_id: self.key_id.clone(),
            timestamp: self.timestamp.clone(),
            signature: self.signature.clone(),
            witnesses: self
                .witnesses
                .iter()
                .map(|w| LogWitness {
                    ts_url: w.ts_url.clone(),
                    entry_hash: w.entry_hash.clone(),
                    receipt_b64: w.receipt_b64.clone(),
                    leaf_index: w.leaf_index,
                    tree_size: w.tree_size,
                    is_stub: w.is_stub,
                })
                .collect(),
        }
    }

    fn from_log(cp: LogCheckpoint) -> Self {
        Self {
            v: cp.v,
            kind: cp.kind,
            log_id: cp.log_id,
            mmr_size: cp.mmr_size,
            root: cp.root,
            prev_size: cp.prev_size,
            prev_root: cp.prev_root,
            key_id: cp.key_id,
            timestamp: cp.timestamp,
            signature: cp.signature,
            witnesses: cp
                .witnesses
                .into_iter()
                .map(|w| CheckpointWitness {
                    ts_url: w.ts_url,
                    entry_hash: w.entry_hash,
                    receipt_b64: w.receipt_b64,
                    leaf_index: w.leaf_index,
                    tree_size: w.tree_size,
                    is_stub: w.is_stub,
                })
                .collect(),
        }
    }
}

/// Evidence that one record occupies one position under a checkpoint. Same
/// fields and wire shape as the log's inclusion proof.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InclusionProof {
    pub v: u32,
    pub kind: String,
    pub size: u64,
    pub leaf_index: u64,
    pub witness: Vec<String>,
    pub peaks_left: Vec<String>,
    pub peaks_right: Vec<String>,
}

impl InclusionProof {
    fn from_log(p: LogInclusion) -> Self {
        Self {
            v: p.v,
            kind: p.kind,
            size: p.size,
            leaf_index: p.leaf_index,
            witness: p.witness,
            peaks_left: p.peaks_left,
            peaks_right: p.peaks_right,
        }
    }

    fn to_log(&self) -> LogInclusion {
        LogInclusion {
            v: self.v,
            kind: self.kind.clone(),
            size: self.size,
            leaf_index: self.leaf_index,
            witness: self.witness.clone(),
            peaks_left: self.peaks_left.clone(),
            peaks_right: self.peaks_right.clone(),
        }
    }

    /// The wire JSON of this proof (the member set a `scitt_cose`
    /// inclusion-proof reader takes).
    pub fn to_wire_json(&self) -> serde_json::Value {
        serde_json::json!({
            "v": self.v,
            "kind": self.kind,
            "size": self.size,
            "leaf_index": self.leaf_index,
            "witness": self.witness,
            "peaks_left": self.peaks_left,
            "peaks_right": self.peaks_right,
        })
    }
}

/// Check `proof` places `record_id` at `leaf_index` under a checkpoint of
/// `size` with root `root`. Offline; needs nothing from the store.
pub fn verify_inclusion(
    root: &[u8; 32],
    size: u64,
    leaf_index: u64,
    record_id: &[u8; 32],
    proof: &InclusionProof,
) -> bool {
    cll::mmr::verify_inclusion(root, size, leaf_index, record_id, &proof.to_log())
}

/// What opening the durable node store found: its node count, and how many
/// bytes of a torn trailing write were truncated away.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct OpenReport {
    pub node_count: u64,
    pub truncated_bytes: u64,
}

/// The substrate's errors. `crate::checkpoint` maps them onto its own
/// `CheckpointStateError`.
#[derive(Debug, thiserror::Error)]
pub enum SubstrateError {
    #[error("node store error: {0}")]
    NodeStore(String),
    #[error("checkpoints.jsonl error: {0}")]
    Store(String),
    #[error("mmr error: {0}")]
    Mmr(String),
    #[error("signer error: {0}")]
    Signer(String),
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
    #[error("leaf {leaf_index} is committed as {stored_leaf}, but the record id given leaf-hashes to {expected_leaf}")]
    LeafMismatch {
        leaf_index: u64,
        stored_leaf: String,
        expected_leaf: String,
    },
    #[error("cutting at {requested} leaves, but only {available} are committed")]
    CutBeyondLog { requested: u64, available: u64 },
    #[error(
        "prepared checkpoint chains to size {prev_size}, but the latest committed checkpoint \
         is at size {latest_size} -- prepare it again"
    )]
    StalePrepared { prev_size: u64, latest_size: u64 },
}

/// Converts the log's errors into [`SubstrateError`].
trait FromLog<T> {
    fn log_err(self) -> Result<T, SubstrateError>;
}

macro_rules! from_log {
    ($err:ty, $variant:ident) => {
        impl<T> FromLog<T> for Result<T, $err> {
            fn log_err(self) -> Result<T, SubstrateError> {
                self.map_err(|e| SubstrateError::$variant(e.to_string()))
            }
        }
    };
}

from_log!(cll::mmr::MmrError, Mmr);
from_log!(cll::node_store::NodeStoreError, NodeStore);
from_log!(cll::store::StoreError, Store);
from_log!(LogSignerError, Signer);

/// A signed checkpoint not yet persisted. Witness receipts may be attached
/// to `checkpoint.witnesses` before [`CllSubstrate::commit_checkpoint`].
#[derive(Debug, Clone)]
pub(crate) struct PreparedCheckpoint {
    pub(crate) checkpoint: CheckpointRecord,
    cose: Option<Vec<u8>>,
    cose_error: Option<String>,
}

impl PreparedCheckpoint {
    /// The COSE wire form, when it could be built. Building it is
    /// best-effort: a failure never blocks the JSON checkpoint, it only means
    /// the checkpoint stays self-attested (no witness can register it).
    pub(crate) fn cose(&self) -> Option<&[u8]> {
        self.cose.as_deref()
    }

    /// Why the COSE wire form could not be built, if it could not.
    pub(crate) fn cose_error(&self) -> Option<&str> {
        self.cose_error.as_deref()
    }
}

/// A Checkpointed Local Log with a durable MMR node store and a JSON-lines
/// checkpoint file.
pub(crate) struct CllSubstrate {
    node_store: FileNodeStore,
    checkpoints_path: PathBuf,
    last_checkpoint: Option<CheckpointRecord>,
    last_checkpoint_cose: Option<Vec<u8>>,
}

impl CllSubstrate {
    /// Open (or create) the node store at `node_store_path` and resume from
    /// the last line of `checkpoints_path` if it exists. A torn trailing
    /// node write is truncated away and reported.
    pub(crate) fn open(
        node_store_path: &Path,
        checkpoints_path: &Path,
    ) -> Result<(Self, OpenReport), SubstrateError> {
        let (node_store, report) = FileNodeStore::open(node_store_path).log_err()?;
        let last_line = read_last_checkpoint(checkpoints_path).log_err()?;
        let last_checkpoint = last_line
            .as_ref()
            .map(|l| CheckpointRecord::from_log(l.record.clone()));
        let last_checkpoint_cose = last_line
            .and_then(|l| l.checkpoint_cose_hex)
            .and_then(|hex_str| hex::decode(hex_str).ok());
        Ok((
            Self {
                node_store,
                checkpoints_path: checkpoints_path.to_path_buf(),
                last_checkpoint,
                last_checkpoint_cose,
            },
            OpenReport {
                node_count: report.node_count,
                truncated_bytes: report.truncated_bytes,
            },
        ))
    }

    /// Records committed so far.
    pub(crate) fn leaf_count(&self) -> Result<u64, SubstrateError> {
        mmr_leaf_count(self.node_store.size()).log_err()
    }

    /// Commit `record_id` as the next leaf; returns its leaf index.
    pub(crate) fn append_leaf(&mut self, record_id: &RecordId) -> Result<u64, SubstrateError> {
        let index = self.leaf_count()?;
        add_leaf(&mut self.node_store, leaf_hash(record_id)).log_err()?;
        Ok(index)
    }

    /// Check the leaf at `leaf_index` commits `record_id`, so a record file
    /// rewritten under an existing node store is detected: the substrate
    /// must never checkpoint over superseded leaves.
    pub(crate) fn check_leaf(
        &self,
        leaf_index: u64,
        record_id: &RecordId,
    ) -> Result<(), SubstrateError> {
        let expected_leaf = leaf_hash(record_id);
        let stored_leaf = self
            .node_store
            .node(leaf_index_to_pos(leaf_index).log_err()?);
        if stored_leaf != expected_leaf {
            return Err(SubstrateError::LeafMismatch {
                leaf_index,
                stored_leaf: hex::encode(stored_leaf),
                expected_leaf: hex::encode(expected_leaf),
            });
        }
        Ok(())
    }

    /// The latest persisted checkpoint.
    pub(crate) fn last_checkpoint(&self) -> Option<&CheckpointRecord> {
        self.last_checkpoint.as_ref()
    }

    /// The latest persisted checkpoint's COSE wire form, if it had one.
    pub(crate) fn last_checkpoint_cose(&self) -> Option<&[u8]> {
        self.last_checkpoint_cose.as_deref()
    }

    /// Mutable access to the latest checkpoint's witness list, for a late
    /// witness registration. The addition is in memory only; the next
    /// persisted checkpoint supersedes it.
    pub(crate) fn last_checkpoint_witnesses_mut(&mut self) -> Option<&mut Vec<CheckpointWitness>> {
        self.last_checkpoint.as_mut().map(|cp| &mut cp.witnesses)
    }

    /// Sign a checkpoint over the first `leaf_count` leaves, chained to the
    /// last persisted checkpoint. Refuses an empty cut, a cut that does not
    /// grow the log, and a log whose root at the previous checkpoint's size
    /// no longer matches the root that checkpoint recorded.
    pub(crate) fn prepare_checkpoint(
        &self,
        leaf_count: u64,
        log_id: &str,
        timestamp: &str,
        signer: &dyn CheckpointSigner,
    ) -> Result<PreparedCheckpoint, SubstrateError> {
        let available = self.leaf_count()?;
        if leaf_count > available {
            return Err(SubstrateError::CutBeyondLog {
                requested: leaf_count,
                available,
            });
        }
        let current_size = node_count(leaf_count);
        if current_size == 0 {
            return Err(SubstrateError::EmptyMmr);
        }

        let (prev_size, prev_root) = match &self.last_checkpoint {
            None => (0u64, String::new()),
            Some(prev) => {
                if current_size <= prev.mmr_size {
                    return Err(SubstrateError::RollbackSize {
                        current_size,
                        prev_size: prev.mmr_size,
                    });
                }
                let actual_prev_root =
                    hex::encode(root_from_peaks(&self.peak_hashes(prev.mmr_size)?));
                if actual_prev_root != prev.root {
                    return Err(SubstrateError::RollbackRoot {
                        prev_size: prev.mmr_size,
                        actual_root: actual_prev_root,
                        recorded_root: prev.root.clone(),
                    });
                }
                (prev.mmr_size, prev.root.clone())
            }
        };

        let new_peak_hashes = self.peak_hashes(current_size)?;
        let root = hex::encode(root_from_peaks(&new_peak_hashes));
        let adapter = SignerAdapter(signer);
        let mut cp = LogCheckpoint {
            v: 1,
            kind: CHECKPOINT_KIND.to_string(),
            log_id: log_id.to_string(),
            mmr_size: current_size,
            root,
            prev_size,
            prev_root,
            key_id: adapter.key_id(),
            timestamp: timestamp.to_string(),
            signature: String::new(),
            witnesses: Vec::new(),
        };
        cp.signature = try_sign_checkpoint_digest(&cp, &adapter).log_err()?;

        // COSE wire form, best-effort: `cadence_seconds` is omitted, as the
        // Python reference omits it.
        let prev_peak_hashes = if prev_size > 0 {
            Some(self.peak_hashes(prev_size)?)
        } else {
            None
        };
        let consistency = if prev_size > 0 {
            Some(consistency_proof(&self.node_store, prev_size, current_size).log_err()?)
        } else {
            None
        };
        let (cose, cose_error) = match checkpoint_to_cose(
            &cp,
            &adapter,
            &new_peak_hashes,
            prev_peak_hashes.as_deref(),
            consistency.as_ref(),
            None,
        ) {
            Ok(bytes) => (Some(bytes), None),
            Err(err) => (None, Some(err.to_string())),
        };

        Ok(PreparedCheckpoint {
            checkpoint: CheckpointRecord::from_log(cp),
            cose,
            cose_error,
        })
    }

    /// Persist a prepared checkpoint (with whatever witness receipts were
    /// attached) as the new latest checkpoint.
    pub(crate) fn commit_checkpoint(
        &mut self,
        prepared: PreparedCheckpoint,
    ) -> Result<CheckpointRecord, SubstrateError> {
        let PreparedCheckpoint {
            checkpoint, cose, ..
        } = prepared;
        // A prepared checkpoint chains to the checkpoint that was latest when
        // it was prepared; if another was committed since, it would fork.
        let (latest_size, latest_root) = self
            .last_checkpoint
            .as_ref()
            .map_or((0, ""), |cp| (cp.mmr_size, cp.root.as_str()));
        if checkpoint.prev_size != latest_size || checkpoint.prev_root != latest_root {
            return Err(SubstrateError::StalePrepared {
                prev_size: checkpoint.prev_size,
                latest_size,
            });
        }
        append_checkpoint(
            &self.checkpoints_path,
            &CheckpointLine {
                record: checkpoint.to_log(),
                checkpoint_cose_hex: cose.as_ref().map(hex::encode),
            },
        )
        .log_err()?;
        self.last_checkpoint = Some(checkpoint.clone());
        self.last_checkpoint_cose = cose;
        Ok(checkpoint)
    }

    /// Inclusion proof for the leaf at `leaf_index` under a checkpoint of
    /// `mmr_size`.
    pub(crate) fn inclusion_at(
        &self,
        leaf_index: u64,
        mmr_size: u64,
    ) -> Result<InclusionProof, SubstrateError> {
        Ok(InclusionProof::from_log(
            inclusion_proof(&self.node_store, leaf_index, mmr_size).log_err()?,
        ))
    }

    fn peak_hashes(&self, size: u64) -> Result<Vec<Hash>, SubstrateError> {
        Ok(peaks(size)
            .log_err()?
            .iter()
            .map(|&p| self.node_store.node(p))
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::SigningKey;

    fn id(n: u8) -> RecordId {
        [n; RECORD_ID_LEN]
    }

    fn open(dir: &Path) -> CllSubstrate {
        CllSubstrate::open(&dir.join("mmr_nodes.dat"), &dir.join("checkpoints.jsonl"))
            .unwrap()
            .0
    }

    fn cut(
        s: &mut CllSubstrate,
        leaves: u64,
        key: &SigningKey,
    ) -> Result<CheckpointRecord, SubstrateError> {
        let p = s.prepare_checkpoint(leaves, "log", "2026-09-27T00:00:00.000Z", key)?;
        s.commit_checkpoint(p)
    }

    #[test]
    fn checkpoints_chain_prove_inclusion_and_survive_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let key = SigningKey::from_bytes(&[9u8; 32]);
        let mut s = open(dir.path());
        for n in 0..3 {
            assert_eq!(s.append_leaf(&id(n)).unwrap(), n as u64);
        }
        let first = cut(&mut s, 3, &key).unwrap();
        assert!(first.verify_signature_offline());
        assert_eq!((first.prev_size, first.prev_root.as_str()), (0, ""));
        for n in 3..5 {
            s.append_leaf(&id(n)).unwrap();
        }
        drop(s);

        let mut s = open(dir.path());
        assert_eq!(s.leaf_count().unwrap(), 5);
        assert_eq!(s.last_checkpoint(), Some(&first));
        let second = cut(&mut s, 5, &key).unwrap();
        assert_eq!(
            (second.prev_size, &second.prev_root),
            (first.mmr_size, &first.root)
        );
        let root = record_id_from_hex(&second.root).unwrap();
        for n in 0..5u8 {
            let proof = s.inclusion_at(n as u64, second.mmr_size).unwrap();
            assert!(verify_inclusion(
                &root,
                second.mmr_size,
                n as u64,
                &id(n),
                &proof
            ));
            assert!(!verify_inclusion(
                &root,
                second.mmr_size,
                n as u64,
                &id(99),
                &proof
            ));
        }
    }

    #[test]
    fn a_cut_must_grow_the_log_and_cannot_be_empty_or_beyond_it() {
        let dir = tempfile::tempdir().unwrap();
        let key = SigningKey::from_bytes(&[9u8; 32]);
        let mut s = open(dir.path());
        assert!(matches!(
            cut(&mut s, 0, &key),
            Err(SubstrateError::EmptyMmr)
        ));
        s.append_leaf(&id(1)).unwrap();
        assert!(matches!(
            cut(&mut s, 2, &key),
            Err(SubstrateError::CutBeyondLog {
                requested: 2,
                available: 1
            })
        ));
        cut(&mut s, 1, &key).unwrap();
        assert!(matches!(
            cut(&mut s, 1, &key),
            Err(SubstrateError::RollbackSize { .. })
        ));
    }

    #[test]
    fn a_rewritten_history_under_a_checkpoint_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let key = SigningKey::from_bytes(&[9u8; 32]);
        let mut s = open(dir.path());
        s.append_leaf(&id(1)).unwrap();
        cut(&mut s, 1, &key).unwrap();
        drop(s);
        // Rebuild the node store over a different first leaf, keep the
        // checkpoint file: the recorded root no longer matches.
        std::fs::remove_file(dir.path().join("mmr_nodes.dat")).unwrap();
        let mut s = open(dir.path());
        s.append_leaf(&id(2)).unwrap();
        s.append_leaf(&id(3)).unwrap();
        assert!(matches!(
            cut(&mut s, 2, &key),
            Err(SubstrateError::RollbackRoot { .. })
        ));
    }

    #[test]
    fn a_prepared_checkpoint_goes_stale_once_another_is_committed() {
        let dir = tempfile::tempdir().unwrap();
        let key = SigningKey::from_bytes(&[9u8; 32]);
        let mut s = open(dir.path());
        s.append_leaf(&id(1)).unwrap();
        s.append_leaf(&id(2)).unwrap();
        let first = s
            .prepare_checkpoint(1, "log", "2026-09-27T00:00:00.000Z", &key)
            .unwrap();
        let second = s
            .prepare_checkpoint(2, "log", "2026-09-27T00:00:00.000Z", &key)
            .unwrap();
        s.commit_checkpoint(second).unwrap();
        assert!(matches!(
            s.commit_checkpoint(first),
            Err(SubstrateError::StalePrepared {
                prev_size: 0,
                latest_size: 3
            })
        ));
        assert_eq!(s.last_checkpoint().unwrap().mmr_size, 3);
    }

    #[test]
    fn check_leaf_detects_a_different_record_at_a_position() {
        let dir = tempfile::tempdir().unwrap();
        let mut s = open(dir.path());
        s.append_leaf(&id(1)).unwrap();
        s.check_leaf(0, &id(1)).unwrap();
        assert!(matches!(
            s.check_leaf(0, &id(2)),
            Err(SubstrateError::LeafMismatch { leaf_index: 0, .. })
        ));
    }

    #[test]
    fn witnesses_attached_before_commit_are_persisted() {
        let dir = tempfile::tempdir().unwrap();
        let key = SigningKey::from_bytes(&[9u8; 32]);
        let mut s = open(dir.path());
        s.append_leaf(&id(1)).unwrap();
        let mut p = s
            .prepare_checkpoint(1, "log", "2026-09-27T00:00:00.000Z", &key)
            .unwrap();
        assert!(p.cose().is_some(), "{:?}", p.cose_error());
        p.checkpoint.witnesses.push(CheckpointWitness {
            ts_url: "https://witness.example".into(),
            entry_hash: "e".repeat(64),
            receipt_b64: "cmVjZWlwdA==".into(),
            leaf_index: 0,
            tree_size: 1,
            is_stub: false,
        });
        let committed = s.commit_checkpoint(p).unwrap();
        drop(s);
        let s = open(dir.path());
        assert_eq!(s.last_checkpoint(), Some(&committed));
        assert_eq!(s.last_checkpoint().unwrap().witnesses.len(), 1);
        assert!(s.last_checkpoint_cose().is_some());
    }

    /// A signer that fails surfaces as a signer error, never as a checkpoint.
    #[test]
    fn a_failing_signer_cuts_nothing() {
        struct Refuses;
        impl CheckpointSigner for Refuses {
            fn sign(&self, _: &[u8]) -> Result<[u8; 64], CheckpointSignError> {
                Err(CheckpointSignError("key unavailable".into()))
            }
            fn key_id(&self) -> String {
                "00".repeat(32)
            }
        }
        let dir = tempfile::tempdir().unwrap();
        let mut s = open(dir.path());
        s.append_leaf(&id(1)).unwrap();
        let err = s
            .prepare_checkpoint(1, "log", "2026-09-27T00:00:00.000Z", &Refuses)
            .unwrap_err();
        assert!(
            matches!(&err, SubstrateError::Signer(m) if m.contains("key unavailable")),
            "{err}"
        );
        assert!(s.last_checkpoint().is_none());
    }

    #[test]
    fn wire_shapes_match_the_logs_own_records() {
        let cp = CheckpointRecord {
            v: 1,
            kind: CHECKPOINT_KIND.into(),
            log_id: "log".into(),
            mmr_size: 1,
            root: "a".repeat(64),
            prev_size: 0,
            prev_root: String::new(),
            key_id: "b".repeat(64),
            timestamp: "2026-09-27T00:00:00.000Z".into(),
            signature: "c".repeat(128),
            witnesses: vec![CheckpointWitness {
                ts_url: "https://witness.example".into(),
                entry_hash: "e".repeat(64),
                receipt_b64: "cmVjZWlwdA==".into(),
                leaf_index: 3,
                tree_size: 4,
                is_stub: true,
            }],
        };
        assert_eq!(
            serde_json::to_value(&cp).unwrap(),
            serde_json::to_value(cp.to_log()).unwrap()
        );
        assert_eq!(CheckpointRecord::from_log(cp.to_log()), cp);
        assert_eq!(cp.digest(), cp.to_log().digest());

        let proof = InclusionProof {
            v: 1,
            kind: "inclusion".into(),
            size: 3,
            leaf_index: 1,
            witness: vec!["d".repeat(64)],
            peaks_left: vec![],
            peaks_right: vec!["f".repeat(64)],
        };
        assert_eq!(InclusionProof::from_log(proof.to_log()), proof);
        assert_eq!(serde_json::to_value(&proof).unwrap(), proof.to_wire_json());
    }
}
