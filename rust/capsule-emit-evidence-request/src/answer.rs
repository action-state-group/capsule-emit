//! The artifact response (§4.1): building an answer (responder) and
//! verifying one offline (requester), over a checkpointed local log (`cll`).
//!
//! An answer has three parts:
//!
//! 1. **The artifact**: deterministic RFC 8785 JSON, a function of (subject,
//!    resolved anchor, derivation) only -- never of the requester, the
//!    request's nonce or route, or the time it was assembled (§5):
//!    `{"anchor", "evidence_stream", "subject", "records" | "checkpoints" |
//!    "history", "derivation"?}`. Records are served with their leaf index,
//!    digest and body, in log order.
//! 2. **The verification material**: the anchor checkpoint, and an inclusion
//!    proof for each record (or one range proof for `range` and
//!    `full_history`). It may differ between requesters (§5); it is not
//!    signed. For `checkpoints` and `history_card/1` the artifact itself is
//!    the proof (§4.1).
//! 3. **The envelope**: `{"request_digest", "anchor", "artifact_digest",
//!    "issued_at", "key_id", "sig"}`, signed by the responder (Ed25519 over
//!    RFC 8785 of the first four members). It binds the artifact to this
//!    request and this anchor.
//!
//! [`verify`] checks all of it against the responder key and the request the
//! requester sent: the envelope signature (`verify_strict`) and bindings,
//! the artifact digest, the anchor checkpoint (signed by the same responder
//! key; for an `expected_pin`, exactly the pinned checkpoint), the coverage
//! constraint, every record's body against its digest (with the caller's
//! digest function: how a record's digest is computed is the evidence
//! format's), and every proof. Nothing here trusts the responder beyond its
//! key.
//!
//! Which records a `correlation` or `exchange` subject should include is
//! format-specific (the correlation identifier or the citation lives inside
//! the record). [`verify`] proves each served record is in the log under the
//! anchor; the caller checks that each carries the identifier or cites its
//! half ([`VerifiedAnswer::records`]).
//!
//! Size: an answer carries at most [`MAX_RECORDS`] records or checkpoints.
//! [`verify`] refuses a larger one before parsing or enumerating anything,
//! whatever size the responder's checkpoint claims; [`build`] takes a limit
//! and stops there ([`BuildError::OverLimit`], answered as
//! `policy_declined`).
//!
//! Checkpoint lists (`checkpoints`, `history_card/1`) must start at the
//! stream's first checkpoint (`prev_size` 0, empty `prev_root`), so a
//! responder cannot leave out a prefix of its history.
//!
//! Terms: an anchor is a `cll` checkpoint, identified by its digest
//! ([`CheckpointRecord::digest`], the value registered with a transparency
//! service). Its "size" for coverage is the number of records it covers,
//! and `range` positions are leaf indices (0-based).

use crate::jcs;
use crate::registry::{is_digest, DERIVATION_HISTORY_CARD};
use crate::request::{Coverage, Derivation, Freshness, Request, Subject};
use crate::resolve::ResolvedAnchor;
use crate::time::parse_utc;
use cll::checkpoint::CheckpointRecord;
use cll::mmr::{
    consistency_proof, inclusion_proof, leaf_count, verify_consistency, verify_inclusion,
    ConsistencyProof, Hash, InclusionProof, NodeReader,
};
use cll::range_proof::{range_proof, verify_range, RangeProof};
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

/// The most records (or checkpoints) one answer may carry.
///
/// [`verify`] refuses a larger answer before it parses any record or proof,
/// and before it enumerates a range, so a responder cannot make a requester
/// allocate without bound, whatever size its signed checkpoint claims. A
/// responder builds with a `limit` of at most this ([`build`]) and answers a
/// larger subject with a `policy_declined` refusal: ask for a range instead.
pub const MAX_RECORDS: u64 = 10_000;

/// One record of the responder's evidence stream.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Record {
    /// 0-based position in the log.
    pub leaf_index: u64,
    /// The record's digest (the leaf's body digest), 64 lowercase hex.
    pub digest: String,
    /// The record's bytes.
    pub body: Vec<u8>,
}

/// What a responder answers from.
pub trait EvidenceLog {
    type Nodes: NodeReader;
    /// The log's MMR nodes.
    fn nodes(&self) -> &Self::Nodes;
    /// The stream's checkpoints, oldest first.
    fn checkpoints(&self) -> Vec<CheckpointRecord>;
    /// The record with exactly this digest.
    fn record_by_digest(&self, digest: &str) -> Option<Record>;
    /// The record at this leaf index.
    fn record_at(&self, leaf_index: u64) -> Option<Record>;
    /// Digests of the records carrying exactly this correlation identifier.
    fn correlation(&self, id: &str) -> Vec<String>;
    /// Digests of the records citing exactly this exchange half.
    fn citing(&self, half_digest: &str) -> Vec<String>;
}

/// A built answer: the three parts, as bytes and a JSON envelope.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Answer {
    /// RFC 8785 bytes; identical for every requester of the same subject
    /// under the same anchor.
    pub artifact: Vec<u8>,
    /// RFC 8785 bytes of the verification material.
    pub material: Vec<u8>,
    /// The signed envelope.
    pub envelope: Value,
}

/// Why an answer could not be built.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum BuildError {
    #[error("the resolved anchor is not one of the stream's checkpoints")]
    AnchorNotFound,
    #[error("a record the subject names is not in the log under the anchor")]
    RecordNotFound,
    #[error("the subject resolves to no records under the anchor")]
    NoRecords,
    #[error("only the history_card/1 derivation has an answer path")]
    DerivationUnsupported,
    #[error("the request digest or issued_at time is malformed")]
    Malformed,
    /// The subject covers more records (or checkpoints) than the limit.
    /// Answer with a `policy_declined` refusal.
    #[error("the answer would carry more than the limit of records or checkpoints")]
    OverLimit,
    #[error("proof construction failed: {0}")]
    Proof(String),
}

/// Build the answer to `request` (digest `request_digest`, as received)
/// under `anchor`, as resolved by [`crate::resolve::resolve`]. For an
/// `exchange` subject pinned by the requester's own half, the records are
/// proven under the stream's latest checkpoint.
///
/// `limit` caps the records (or checkpoints) the answer carries; it is
/// clamped to [`MAX_RECORDS`], the most a requester accepts. Over the limit,
/// [`BuildError::OverLimit`]: answer with a `policy_declined` refusal. The
/// log is never read beyond the limit.
#[allow(clippy::too_many_arguments)]
pub fn build<L: EvidenceLog>(
    request: &Request,
    request_digest: &str,
    anchor: &ResolvedAnchor,
    log: &L,
    limit: u64,
    signing_key: &SigningKey,
    issued_at: &str,
) -> Result<Answer, BuildError> {
    if !is_digest(request_digest) || parse_utc(issued_at).is_none() {
        return Err(BuildError::Malformed);
    }
    let checkpoints = log.checkpoints();
    let anchor_index = match anchor {
        ResolvedAnchor::Anchor(a) => checkpoints.iter().position(|cp| cp.digest() == a.digest),
        ResolvedAnchor::ExchangeHalf(_) => checkpoints.len().checked_sub(1),
    }
    .ok_or(BuildError::AnchorNotFound)?;
    let anchor_cp = &checkpoints[anchor_index];
    let anchor_digest = anchor_cp.digest();
    let covered = leaf_count(anchor_cp.mmr_size).map_err(|e| BuildError::Proof(e.to_string()))?;
    let limit = limit.min(MAX_RECORDS);
    let checkpoint_count = anchor_index as u64 + 1;

    let mut artifact = Map::new();
    artifact.insert("anchor".into(), json!(anchor_digest));
    artifact.insert("evidence_stream".into(), json!(anchor_cp.log_id));
    artifact.insert("subject".into(), subject_json(&request.subject));
    let mut material = Map::new();
    material.insert("anchor_checkpoint".into(), checkpoint_json(anchor_cp));

    match &request.derivation {
        Some(Derivation::Token(t)) if t == DERIVATION_HISTORY_CARD => {
            if checkpoint_count > limit {
                return Err(BuildError::OverLimit);
            }
            artifact.insert("derivation".into(), json!(DERIVATION_HISTORY_CARD));
            let mut history = Vec::new();
            for (i, cp) in checkpoints[..=anchor_index].iter().enumerate() {
                let consistency = match i.checked_sub(1).map(|p| &checkpoints[p]) {
                    None => Value::Null,
                    Some(prev) => consistency_json(
                        &consistency_proof(log.nodes(), prev.mmr_size, cp.mmr_size)
                            .map_err(|e| BuildError::Proof(e.to_string()))?,
                    ),
                };
                history.push(json!({"checkpoint": checkpoint_json(cp), "consistency_from_previous": consistency}));
            }
            artifact.insert("history".into(), Value::Array(history));
        }
        Some(_) => return Err(BuildError::DerivationUnsupported),
        None => match &request.subject {
            Subject::Checkpoints => {
                if checkpoint_count > limit {
                    return Err(BuildError::OverLimit);
                }
                let list = checkpoints[..=anchor_index]
                    .iter()
                    .map(checkpoint_json)
                    .collect();
                artifact.insert("checkpoints".into(), Value::Array(list));
            }
            Subject::Range(a, b) => {
                let records = range_records(log, *a, *b, covered, limit)?;
                material.insert(
                    "range".into(),
                    range_json(&range_or_err(log, *a, *b, anchor_cp.mmr_size)?),
                );
                artifact.insert("records".into(), records_json(&records));
            }
            Subject::FullHistory => {
                if covered == 0 {
                    return Err(BuildError::NoRecords);
                }
                let records = range_records(log, 0, covered - 1, covered, limit)?;
                material.insert(
                    "range".into(),
                    range_json(&range_or_err(log, 0, covered - 1, anchor_cp.mmr_size)?),
                );
                artifact.insert("records".into(), records_json(&records));
            }
            Subject::Record(d) => {
                let record = log
                    .record_by_digest(d)
                    .filter(|r| r.leaf_index < covered)
                    .ok_or(BuildError::RecordNotFound)?;
                serve_records(
                    log,
                    vec![record],
                    anchor_cp.mmr_size,
                    &mut artifact,
                    &mut material,
                )?;
            }
            Subject::Correlation(id) => {
                let records = records_under(log, log.correlation(id), covered, limit)?;
                serve_records(
                    log,
                    records,
                    anchor_cp.mmr_size,
                    &mut artifact,
                    &mut material,
                )?;
            }
            Subject::Exchange(half) => {
                let records = records_under(log, log.citing(half), covered, limit)?;
                serve_records(
                    log,
                    records,
                    anchor_cp.mmr_size,
                    &mut artifact,
                    &mut material,
                )?;
            }
        },
    }

    let artifact =
        jcs::to_string(&Value::Object(artifact)).map_err(|e| BuildError::Proof(e.to_string()))?;
    let material =
        jcs::to_string(&Value::Object(material)).map_err(|e| BuildError::Proof(e.to_string()))?;
    let artifact_digest = hex::encode(Sha256::digest(artifact.as_bytes()));
    let mut signed = Map::new();
    signed.insert("request_digest".into(), json!(request_digest));
    signed.insert("anchor".into(), json!(anchor_digest));
    signed.insert("artifact_digest".into(), json!(artifact_digest));
    signed.insert("issued_at".into(), json!(issued_at));
    let body = jcs::to_string(&Value::Object(signed.clone()))
        .map_err(|e| BuildError::Proof(e.to_string()))?;
    let sig = signing_key.sign(body.as_bytes());
    let mut envelope = signed;
    envelope.insert(
        "key_id".into(),
        json!(hex::encode(signing_key.verifying_key().to_bytes())),
    );
    envelope.insert("sig".into(), json!(hex::encode(sig.to_bytes())));
    Ok(Answer {
        artifact: artifact.into_bytes(),
        material: material.into_bytes(),
        envelope: Value::Object(envelope),
    })
}

fn range_records<L: EvidenceLog>(
    log: &L,
    a: u64,
    b: u64,
    covered: u64,
    limit: u64,
) -> Result<Vec<Record>, BuildError> {
    if b >= covered || a > b {
        return Err(BuildError::RecordNotFound);
    }
    // b - a + 1 > limit, without overflow.
    if b - a >= limit {
        return Err(BuildError::OverLimit);
    }
    (a..=b)
        .map(|i| {
            log.record_at(i)
                .filter(|r| r.leaf_index == i)
                .ok_or(BuildError::RecordNotFound)
        })
        .collect()
}

fn range_or_err<L: EvidenceLog>(
    log: &L,
    a: u64,
    b: u64,
    size: u64,
) -> Result<RangeProof, BuildError> {
    range_proof(log.nodes(), a, b, size).map_err(|e| BuildError::Proof(e.to_string()))
}

fn records_under<L: EvidenceLog>(
    log: &L,
    digests: Vec<String>,
    covered: u64,
    limit: u64,
) -> Result<Vec<Record>, BuildError> {
    if digests.len() as u64 > limit {
        return Err(BuildError::OverLimit);
    }
    let mut records: Vec<Record> = digests
        .iter()
        .filter_map(|d| log.record_by_digest(d))
        .filter(|r| r.leaf_index < covered)
        .collect();
    records.sort_by_key(|r| r.leaf_index);
    records.dedup_by_key(|r| r.leaf_index);
    if records.is_empty() {
        return Err(BuildError::NoRecords);
    }
    Ok(records)
}

fn serve_records<L: EvidenceLog>(
    log: &L,
    records: Vec<Record>,
    size: u64,
    artifact: &mut Map<String, Value>,
    material: &mut Map<String, Value>,
) -> Result<(), BuildError> {
    let proofs = records
        .iter()
        .map(|r| inclusion_proof(log.nodes(), r.leaf_index, size).map(|p| inclusion_json(&p)))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| BuildError::Proof(e.to_string()))?;
    artifact.insert("records".into(), records_json(&records));
    material.insert("inclusion".into(), Value::Array(proofs));
    Ok(())
}

// ---------------------------------------------------------------------------
// Verification (requester side).
// ---------------------------------------------------------------------------

/// Why an answer did not verify. Any of these makes the answer, to the
/// requester, not evidence (§4.1): record it as
/// `artifact_verification_failed`.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum VerifyError {
    #[error("the envelope is malformed")]
    EnvelopeMalformed,
    #[error("the envelope is not signed by the expected responder key")]
    WrongKey,
    #[error("the envelope names a different request")]
    WrongRequest,
    #[error("the artifact does not match the envelope's artifact digest")]
    ArtifactDigest,
    #[error("the artifact is malformed")]
    ArtifactMalformed,
    #[error("the artifact is not for the requested subject or derivation")]
    WrongSubject,
    #[error("the anchor checkpoint is malformed, unsigned, or not the responder's")]
    AnchorInvalid,
    #[error("the anchor does not satisfy the requested coverage")]
    CoverageUnmet,
    #[error("a record's body does not match its digest")]
    RecordDigest,
    #[error("the records do not match the subject")]
    RecordsMismatch,
    #[error("a proof does not verify against the anchor")]
    ProofInvalid,
    #[error("a checkpoint in the artifact is malformed, unsigned, or does not chain from the stream's first checkpoint")]
    CheckpointChain,
    /// The answer carries, or its subject spans, more than [`MAX_RECORDS`]
    /// records or checkpoints; refused before anything is parsed.
    #[error("the answer is larger than MAX_RECORDS")]
    TooLarge,
    /// A checkpoint carries a witness receipt that is not a receipt this
    /// crate reads (§4.1: receipts are verified on their own terms).
    #[error("a witness receipt on a checkpoint is malformed")]
    ReceiptMalformed,
    /// A checkpoint carries a witness receipt that is not for that
    /// checkpoint: its entry hash is another checkpoint's, or, under the
    /// requester's key for that witness, its proof and signature do not cover
    /// this checkpoint's entry (§8.1).
    #[error("a witness receipt does not cover the checkpoint it is carried on")]
    ReceiptBinding,
}

/// What became of one witness receipt carried on a checkpoint.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReceiptCheck {
    /// The digest of the checkpoint the receipt is carried on.
    pub checkpoint_digest: String,
    /// The witness (transparency service) that issued it.
    pub ts_url: String,
    pub status: ReceiptStatus,
}

/// A carried receipt's standing after verification. Only `Verified` counts
/// as independent witnessing; how many are required is the requester's
/// policy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReceiptStatus {
    /// It proves this checkpoint's entry under the witness's key.
    Verified,
    /// Well formed and bound to this checkpoint by its entry hash, but the
    /// requester gave no key for this witness, so it was not authenticated.
    NoKey,
    /// A stub stamp: never registered with any service. Never verified.
    Stub,
}

/// A verified answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedAnswer {
    /// The anchor checkpoint the answer is proven under.
    pub anchor: CheckpointRecord,
    /// The served records, proven to be in the log under the anchor. For a
    /// `correlation` or `exchange` subject the caller checks each one's
    /// content (the identifier, the citation).
    pub records: Vec<Record>,
    /// For `checkpoints` and `history_card/1`: the checkpoints, oldest first,
    /// each signed by the responder and chaining to the anchor.
    pub checkpoints: Vec<CheckpointRecord>,
    /// Every witness receipt carried on the anchor and on those checkpoints.
    /// A malformed or unbound receipt fails verification; the rest are listed
    /// here with what could be established for each.
    pub receipts: Vec<ReceiptCheck>,
}

/// Verify an answer offline: `envelope`, `artifact` and `material` as
/// received, against the `request` the requester sent (and its digest as
/// sent) and the responder's key. `record_digest` computes a record's digest
/// from its bytes, as the evidence format defines it (`None`: not a record
/// of this format). `witness_key` is the requester's trust policy for witness
/// receipts: the key it holds for a witness (by its `ts_url`), if any.
// Each argument is one independent input the requester supplies (what it
// received, what it sent, whom it trusts); bundling them would only rename
// them.
#[allow(clippy::too_many_arguments)]
pub fn verify(
    envelope: &Value,
    artifact: &[u8],
    material: &[u8],
    request: &Request,
    request_digest: &str,
    responder_key: &VerifyingKey,
    record_digest: &dyn Fn(&[u8]) -> Option<String>,
    witness_key: &dyn Fn(&str) -> Option<VerifyingKey>,
) -> Result<VerifiedAnswer, VerifyError> {
    // 1. The envelope: responder, request, anchor, artifact digest.
    let env = envelope.as_object().ok_or(VerifyError::EnvelopeMalformed)?;
    let text = |k: &str| {
        env.get(k)
            .and_then(Value::as_str)
            .ok_or(VerifyError::EnvelopeMalformed)
    };
    let (env_request, env_anchor, env_artifact, issued_at) = (
        text("request_digest")?,
        text("anchor")?,
        text("artifact_digest")?,
        text("issued_at")?,
    );
    let (key_id, sig) = (text("key_id")?, text("sig")?);
    if ![env_request, env_anchor, env_artifact]
        .iter()
        .all(|d| is_digest(d))
        || parse_utc(issued_at).is_none()
    {
        return Err(VerifyError::EnvelopeMalformed);
    }
    if key_id != hex::encode(responder_key.to_bytes()) {
        return Err(VerifyError::WrongKey);
    }
    let signed = json!({
        "request_digest": env_request,
        "anchor": env_anchor,
        "artifact_digest": env_artifact,
        "issued_at": issued_at,
    });
    let body = jcs::to_string(&signed).map_err(|_| VerifyError::EnvelopeMalformed)?;
    let sig = decode_signature(sig).ok_or(VerifyError::EnvelopeMalformed)?;
    responder_key
        .verify_strict(body.as_bytes(), &sig)
        .map_err(|_| VerifyError::WrongKey)?;
    if env_request != request_digest {
        return Err(VerifyError::WrongRequest);
    }
    if hex::encode(Sha256::digest(artifact)) != env_artifact {
        return Err(VerifyError::ArtifactDigest);
    }

    // 2. The artifact's own statements.
    let art: Value =
        serde_json::from_slice(artifact).map_err(|_| VerifyError::ArtifactMalformed)?;
    let art = art.as_object().ok_or(VerifyError::ArtifactMalformed)?;
    if art.get("anchor").and_then(Value::as_str) != Some(env_anchor) {
        return Err(VerifyError::ArtifactMalformed);
    }
    if art.get("subject") != Some(&subject_json(&request.subject)) {
        return Err(VerifyError::WrongSubject);
    }
    let history_card = match &request.derivation {
        None => false,
        Some(Derivation::Token(t)) if t == DERIVATION_HISTORY_CARD => true,
        Some(_) => return Err(VerifyError::WrongSubject),
    };
    let art_derivation = art.get("derivation").and_then(Value::as_str);
    if history_card != (art_derivation == Some(DERIVATION_HISTORY_CARD))
        || (!history_card && art.contains_key("derivation"))
    {
        return Err(VerifyError::WrongSubject);
    }

    // 3. The anchor checkpoint: the responder's, and the one named.
    let mat: Value =
        serde_json::from_slice(material).map_err(|_| VerifyError::ArtifactMalformed)?;
    let anchor = mat
        .get("anchor_checkpoint")
        .and_then(checkpoint_from_json)
        .filter(|cp| cp.digest() == env_anchor && signed_by(cp, responder_key))
        .ok_or(VerifyError::AnchorInvalid)?;
    if art.get("evidence_stream").and_then(Value::as_str) != Some(anchor.log_id.as_str()) {
        return Err(VerifyError::ArtifactMalformed);
    }
    let root = parse_hash(&anchor.root).ok_or(VerifyError::AnchorInvalid)?;
    let covered = leaf_count(anchor.mmr_size).map_err(|_| VerifyError::AnchorInvalid)?;

    // 4. Coverage.
    match &request.coverage {
        Coverage::ExpectedPin(pin) => {
            let pinned_by_own_half =
                matches!(&request.subject, Subject::Exchange(half) if half == pin);
            if !pinned_by_own_half && pin != env_anchor {
                return Err(VerifyError::CoverageUnmet);
            }
        }
        Coverage::MinFreshness(Freshness::Size(n)) if covered < *n => {
            return Err(VerifyError::CoverageUnmet)
        }
        Coverage::MinFreshness(Freshness::Time(t)) => {
            if parse_utc(&anchor.timestamp).is_none_or(|at| at < *t) {
                return Err(VerifyError::CoverageUnmet);
            }
        }
        Coverage::MinFreshness(Freshness::Size(_)) => {}
    }

    // 5. The body of the answer.
    if history_card {
        let history = art
            .get("history")
            .and_then(Value::as_array)
            .ok_or(VerifyError::ArtifactMalformed)?;
        if history.len() as u64 > MAX_RECORDS {
            return Err(VerifyError::TooLarge);
        }
        let checkpoints = verify_history(history, &anchor, responder_key)?;
        let receipts = check_receipts(&anchor, &checkpoints, witness_key)?;
        return Ok(VerifiedAnswer {
            anchor,
            records: Vec::new(),
            checkpoints,
            receipts,
        });
    }
    if let Subject::Checkpoints = request.subject {
        let list = art
            .get("checkpoints")
            .and_then(Value::as_array)
            .ok_or(VerifyError::ArtifactMalformed)?;
        if list.len() as u64 > MAX_RECORDS {
            return Err(VerifyError::TooLarge);
        }
        let checkpoints = verify_checkpoint_list(list, &anchor, responder_key)?;
        let receipts = check_receipts(&anchor, &checkpoints, witness_key)?;
        return Ok(VerifiedAnswer {
            anchor,
            records: Vec::new(),
            checkpoints,
            receipts,
        });
    }

    // Size checks before anything is parsed or enumerated: the range the
    // subject spans (the checkpoint's size is the responder's claim), the
    // records and the proofs actually sent.
    let range = match &request.subject {
        Subject::Range(a, b) => Some((*a, *b)),
        Subject::FullHistory => Some((
            0,
            covered.checked_sub(1).ok_or(VerifyError::RecordsMismatch)?,
        )),
        _ => None,
    };
    if range.is_some_and(|(a, b)| b.saturating_sub(a) >= MAX_RECORDS) {
        return Err(VerifyError::TooLarge);
    }
    let sent_records = art.get("records").and_then(Value::as_array);
    let sent_proofs = mat.get("inclusion").and_then(Value::as_array);
    if sent_records.is_some_and(|r| r.len() as u64 > MAX_RECORDS)
        || sent_proofs.is_some_and(|p| p.len() as u64 > MAX_RECORDS)
    {
        return Err(VerifyError::TooLarge);
    }

    let records = parse_records(art.get("records"))?;
    for r in &records {
        if record_digest(&r.body).as_deref() != Some(r.digest.as_str()) {
            return Err(VerifyError::RecordDigest);
        }
        if r.leaf_index >= covered {
            return Err(VerifyError::RecordsMismatch);
        }
    }
    if let Some((a, b)) = range {
        if !records.iter().map(|r| r.leaf_index).eq(a..=b) {
            return Err(VerifyError::RecordsMismatch);
        }
        let proof = mat
            .get("range")
            .and_then(range_from_json)
            .ok_or(VerifyError::ProofInvalid)?;
        let digests = records
            .iter()
            .map(|r| parse_hash(&r.digest))
            .collect::<Option<Vec<Hash>>>()
            .ok_or(VerifyError::RecordDigest)?;
        if !verify_range(&root, anchor.mmr_size, a, b, &digests, &proof) {
            return Err(VerifyError::ProofInvalid);
        }
    } else {
        match &request.subject {
            Subject::Record(d) if records.len() != 1 || &records[0].digest != d => {
                return Err(VerifyError::RecordsMismatch)
            }
            _ if records.is_empty() => return Err(VerifyError::RecordsMismatch),
            _ => {}
        }
        if records
            .windows(2)
            .any(|w| w[0].leaf_index >= w[1].leaf_index)
        {
            return Err(VerifyError::RecordsMismatch);
        }
        let proofs = mat
            .get("inclusion")
            .and_then(Value::as_array)
            .ok_or(VerifyError::ProofInvalid)?;
        if proofs.len() != records.len() {
            return Err(VerifyError::ProofInvalid);
        }
        for (r, p) in records.iter().zip(proofs) {
            let proof = inclusion_from_json(p).ok_or(VerifyError::ProofInvalid)?;
            let digest = parse_hash(&r.digest).ok_or(VerifyError::RecordDigest)?;
            if !verify_inclusion(&root, anchor.mmr_size, r.leaf_index, &digest, &proof) {
                return Err(VerifyError::ProofInvalid);
            }
        }
    }
    let receipts = check_receipts(&anchor, &[], witness_key)?;
    Ok(VerifiedAnswer {
        anchor,
        records,
        checkpoints: Vec::new(),
        receipts,
    })
}

/// Check every witness receipt carried on `anchor` and `checkpoints` (the
/// anchor is usually also the last of them; each checkpoint is checked once).
///
/// A receipt must be bound to the checkpoint it is carried on: its entry hash
/// is `SHA-256(checkpoint digest bytes)`, the leaf it must prove. It must be a
/// receipt this crate reads. When the requester holds a key for its witness,
/// its proof and signature must cover that entry under the key. Stubs were
/// never registered and are never verified.
fn check_receipts(
    anchor: &CheckpointRecord,
    checkpoints: &[CheckpointRecord],
    witness_key: &dyn Fn(&str) -> Option<VerifyingKey>,
) -> Result<Vec<ReceiptCheck>, VerifyError> {
    use base64::Engine as _;
    let mut seen = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for cp in checkpoints.iter().chain(std::iter::once(anchor)) {
        let digest = cp.digest();
        if !seen.insert(digest.clone()) {
            continue;
        }
        let digest_bytes = hex::decode(&digest).map_err(|_| VerifyError::CheckpointChain)?;
        let entry: [u8; 32] = Sha256::digest(&digest_bytes).into();
        for w in &cp.witnesses {
            let check = |status| ReceiptCheck {
                checkpoint_digest: digest.clone(),
                ts_url: w.ts_url.clone(),
                status,
            };
            if w.is_stub {
                out.push(check(ReceiptStatus::Stub));
                continue;
            }
            if w.entry_hash != hex::encode(entry) {
                return Err(VerifyError::ReceiptBinding);
            }
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(&w.receipt_b64)
                .map_err(|_| VerifyError::ReceiptMalformed)?;
            match witness_key(&w.ts_url) {
                Some(key) => match crate::receipt::verify(&bytes, &entry, &key) {
                    Ok(()) => out.push(check(ReceiptStatus::Verified)),
                    Err(crate::receipt::ReceiptError::Malformed(_)) => {
                        return Err(VerifyError::ReceiptMalformed)
                    }
                    Err(crate::receipt::ReceiptError::NotForThisLeaf) => {
                        return Err(VerifyError::ReceiptBinding)
                    }
                },
                None => {
                    crate::receipt::check_form(&bytes)
                        .map_err(|_| VerifyError::ReceiptMalformed)?;
                    out.push(check(ReceiptStatus::NoKey));
                }
            }
        }
    }
    Ok(out)
}

fn verify_checkpoint_list(
    list: &[Value],
    anchor: &CheckpointRecord,
    key: &VerifyingKey,
) -> Result<Vec<CheckpointRecord>, VerifyError> {
    let checkpoints = list
        .iter()
        .map(|v| checkpoint_from_json(v).filter(|cp| signed_by(cp, key)))
        .collect::<Option<Vec<_>>>()
        .ok_or(VerifyError::CheckpointChain)?;
    let (Some(first), Some(last)) = (checkpoints.first(), checkpoints.last()) else {
        return Err(VerifyError::CheckpointChain);
    };
    // The list starts at the stream's genesis, so no prefix can be left out.
    if first.prev_size != 0 || !first.prev_root.is_empty() {
        return Err(VerifyError::CheckpointChain);
    }
    if last != anchor {
        return Err(VerifyError::CheckpointChain);
    }
    for pair in checkpoints.windows(2) {
        let (prev, next) = (&pair[0], &pair[1]);
        if next.mmr_size <= prev.mmr_size
            || next.prev_size != prev.mmr_size
            || next.prev_root != prev.root
            || next.log_id != prev.log_id
        {
            return Err(VerifyError::CheckpointChain);
        }
    }
    Ok(checkpoints)
}

fn verify_history(
    history: &[Value],
    anchor: &CheckpointRecord,
    key: &VerifyingKey,
) -> Result<Vec<CheckpointRecord>, VerifyError> {
    let list: Vec<Value> = history
        .iter()
        .map(|e| e.get("checkpoint").cloned().unwrap_or(Value::Null))
        .collect();
    let checkpoints = verify_checkpoint_list(&list, anchor, key)?;
    for (i, entry) in history.iter().enumerate() {
        let proof = entry
            .get("consistency_from_previous")
            .ok_or(VerifyError::CheckpointChain)?;
        match i.checked_sub(1).map(|p| &checkpoints[p]) {
            None if proof.is_null() => {}
            None => return Err(VerifyError::CheckpointChain),
            Some(prev) => {
                let cur = &checkpoints[i];
                let proof = consistency_from_json(proof).ok_or(VerifyError::CheckpointChain)?;
                let (ra, rb) = (parse_hash(&prev.root), parse_hash(&cur.root));
                let (Some(ra), Some(rb)) = (ra, rb) else {
                    return Err(VerifyError::CheckpointChain);
                };
                if !verify_consistency(&ra, prev.mmr_size, &rb, cur.mmr_size, &proof) {
                    return Err(VerifyError::CheckpointChain);
                }
            }
        }
    }
    Ok(checkpoints)
}

/// Whether `cp` is signed by `key` (its `key_id` is that key, and the
/// signature over its digest verifies strictly).
fn signed_by(cp: &CheckpointRecord, key: &VerifyingKey) -> bool {
    cp.key_id == hex::encode(key.to_bytes())
        && decode_signature(&cp.signature)
            .is_some_and(|sig| key.verify_strict(cp.digest().as_bytes(), &sig).is_ok())
}

fn decode_signature(s: &str) -> Option<Signature> {
    let bytes: [u8; 64] = hex::decode(s).ok()?.try_into().ok()?;
    Some(Signature::from_bytes(&bytes))
}

fn parse_hash(s: &str) -> Option<Hash> {
    if !is_digest(s) {
        return None;
    }
    hex::decode(s).ok()?.try_into().ok()
}

// ---------------------------------------------------------------------------
// JSON forms (all members text or unsigned integers, so RFC 8785 applies).
// ---------------------------------------------------------------------------

/// The subject as the request binding writes it (A1).
pub fn subject_json(subject: &Subject) -> Value {
    match subject {
        Subject::FullHistory => json!({"full_history": null}),
        Subject::Checkpoints => json!({"checkpoints": null}),
        Subject::Record(d) => json!({"record": d}),
        Subject::Range(a, b) => json!({"range": [a, b]}),
        Subject::Correlation(id) => json!({"correlation": id}),
        Subject::Exchange(d) => json!({"exchange": d}),
    }
}

fn records_json(records: &[Record]) -> Value {
    Value::Array(
        records
            .iter()
            .map(|r| json!({"leaf_index": r.leaf_index, "digest": r.digest, "body": hex::encode(&r.body)}))
            .collect(),
    )
}

fn parse_records(value: Option<&Value>) -> Result<Vec<Record>, VerifyError> {
    let list = value
        .and_then(Value::as_array)
        .ok_or(VerifyError::ArtifactMalformed)?;
    list.iter()
        .map(|v| {
            let leaf_index = v.get("leaf_index").and_then(Value::as_u64)?;
            let digest = v
                .get("digest")
                .and_then(Value::as_str)
                .filter(|d| is_digest(d))?
                .to_string();
            let body = hex::decode(v.get("body").and_then(Value::as_str)?).ok()?;
            Some(Record {
                leaf_index,
                digest,
                body,
            })
        })
        .collect::<Option<Vec<_>>>()
        .ok_or(VerifyError::ArtifactMalformed)
}

fn checkpoint_json(cp: &CheckpointRecord) -> Value {
    serde_json::to_value(cp).unwrap_or(Value::Null)
}

fn checkpoint_from_json(v: &Value) -> Option<CheckpointRecord> {
    serde_json::from_value(v.clone()).ok()
}

fn inclusion_json(p: &InclusionProof) -> Value {
    json!({"v": p.v, "kind": p.kind, "size": p.size, "leaf_index": p.leaf_index,
           "witness": p.witness, "peaks_left": p.peaks_left, "peaks_right": p.peaks_right})
}

fn inclusion_from_json(v: &Value) -> Option<InclusionProof> {
    Some(InclusionProof {
        v: u32::try_from(v.get("v")?.as_u64()?).ok()?,
        kind: v.get("kind")?.as_str()?.to_string(),
        size: v.get("size")?.as_u64()?,
        leaf_index: v.get("leaf_index")?.as_u64()?,
        witness: strings(v.get("witness")?)?,
        peaks_left: strings(v.get("peaks_left")?)?,
        peaks_right: strings(v.get("peaks_right")?)?,
    })
}

fn range_json(p: &RangeProof) -> Value {
    json!({"v": p.v, "kind": p.kind, "size": p.size, "from_index": p.from_index,
           "to_index": p.to_index, "witness": p.witness})
}

fn range_from_json(v: &Value) -> Option<RangeProof> {
    Some(RangeProof {
        v: u32::try_from(v.get("v")?.as_u64()?).ok()?,
        kind: v.get("kind")?.as_str()?.to_string(),
        size: v.get("size")?.as_u64()?,
        from_index: v.get("from_index")?.as_u64()?,
        to_index: v.get("to_index")?.as_u64()?,
        witness: strings(v.get("witness")?)?,
    })
}

fn consistency_json(p: &ConsistencyProof) -> Value {
    json!({"v": p.v, "kind": p.kind, "size_a": p.size_a, "size_b": p.size_b,
           "old_peaks": p.old_peaks, "witness": p.witness, "new_peaks": p.new_peaks})
}

fn consistency_from_json(v: &Value) -> Option<ConsistencyProof> {
    let witness = v
        .get("witness")?
        .as_array()?
        .iter()
        .map(strings)
        .collect::<Option<Vec<_>>>()?;
    Some(ConsistencyProof {
        v: u32::try_from(v.get("v")?.as_u64()?).ok()?,
        kind: v.get("kind")?.as_str()?.to_string(),
        size_a: v.get("size_a")?.as_u64()?,
        size_b: v.get("size_b")?.as_u64()?,
        old_peaks: strings(v.get("old_peaks")?)?,
        witness,
        new_peaks: strings(v.get("new_peaks")?)?,
    })
}

fn strings(v: &Value) -> Option<Vec<String>> {
    v.as_array()?
        .iter()
        .map(|s| s.as_str().map(str::to_string))
        .collect()
}
