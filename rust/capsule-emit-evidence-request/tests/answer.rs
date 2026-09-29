//! The answer path end to end over a real checkpointed local log: every
//! subject form and the history card build and verify, artifacts are
//! caller-invariant, and every way a responder (or anyone in between) can
//! get an answer wrong is caught by `answer::verify`.

use capsule_emit_evidence_request::answer::{
    self, build, BuildError, EvidenceLog, Record, VerifyError, MAX_RECORDS,
};
use capsule_emit_evidence_request::digest::request_digest;
use capsule_emit_evidence_request::registry::SubjectForm;
use capsule_emit_evidence_request::request::{parse_json, Derivation, Request};
use capsule_emit_evidence_request::resolve::{
    resolve, Anchor, Resolution, ResolvedAnchor, Responder,
};
use cll::checkpoint::{sign_checkpoint_digest, CheckpointRecord};
use cll::mmr::{
    add_leaf, leaf_count, leaf_hash, node_count, peaks, root_from_peaks, MemoryNodeStore,
    NodeReader,
};
use ed25519_dalek::SigningKey;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

const STREAM: &str = "stream-a";
const ISSUED_AT: &str = "2026-09-29T00:00:00Z";

fn sha(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

fn record_digest(body: &[u8]) -> Option<String> {
    Some(sha(body))
}

struct TestLog {
    nodes: MemoryNodeStore,
    records: Vec<Record>,
    checkpoints: Vec<CheckpointRecord>,
    correlations: BTreeMap<String, Vec<String>>,
    citations: BTreeMap<String, Vec<String>>,
}

fn responder_key() -> SigningKey {
    SigningKey::from_bytes(&[11u8; 32])
}

fn other_key() -> SigningKey {
    SigningKey::from_bytes(&[12u8; 32])
}

fn half() -> String {
    sha(b"the requester's own half")
}

/// Ten records; checkpoints after 4, 7 and 10 of them, signed by
/// `checkpoint_key`. Records 1 and 5 carry correlation "c-1"; records 2 and
/// 8 cite the exchange half [`half`].
fn test_log(checkpoint_key: &SigningKey) -> TestLog {
    let mut log = TestLog {
        nodes: MemoryNodeStore::new(),
        records: Vec::new(),
        checkpoints: Vec::new(),
        correlations: BTreeMap::new(),
        citations: BTreeMap::new(),
    };
    for i in 0..10u64 {
        let body = format!("record-{i}").into_bytes();
        let digest = sha(&body);
        let raw: [u8; 32] = hex::decode(&digest).unwrap().try_into().unwrap();
        add_leaf(&mut log.nodes, leaf_hash(&raw)).unwrap();
        log.records.push(Record {
            leaf_index: i,
            digest: digest.clone(),
            body,
        });
        if i == 1 || i == 5 {
            log.correlations
                .entry("c-1".into())
                .or_default()
                .push(digest.clone());
        }
        if i == 2 || i == 8 {
            log.citations
                .entry(half())
                .or_default()
                .push(digest.clone());
        }
        if [3, 6, 9].contains(&i) {
            let size = log.nodes.size();
            let peak_hashes: Vec<_> = peaks(size)
                .unwrap()
                .iter()
                .map(|&p| log.nodes.node(p))
                .collect();
            let prev = log.checkpoints.last();
            let mut cp = CheckpointRecord {
                v: 1,
                kind: "mmr_checkpoint".into(),
                log_id: STREAM.into(),
                mmr_size: size,
                root: hex::encode(root_from_peaks(&peak_hashes)),
                prev_size: prev.map_or(0, |p| p.mmr_size),
                prev_root: prev.map_or(String::new(), |p| p.root.clone()),
                key_id: hex::encode(checkpoint_key.verifying_key().to_bytes()),
                timestamp: format!("2026-09-2{}T00:00:00Z", 6 + log.checkpoints.len()),
                signature: String::new(),
                witnesses: Vec::new(),
            };
            cp.signature = sign_checkpoint_digest(&cp, checkpoint_key);
            log.checkpoints.push(cp);
        }
    }
    log
}

impl EvidenceLog for TestLog {
    type Nodes = MemoryNodeStore;
    fn nodes(&self) -> &MemoryNodeStore {
        &self.nodes
    }
    fn checkpoints(&self) -> Vec<CheckpointRecord> {
        self.checkpoints.clone()
    }
    fn record_by_digest(&self, digest: &str) -> Option<Record> {
        self.records.iter().find(|r| r.digest == digest).cloned()
    }
    fn record_at(&self, leaf_index: u64) -> Option<Record> {
        self.records.get(leaf_index as usize).cloned()
    }
    fn correlation(&self, id: &str) -> Vec<String> {
        self.correlations.get(id).cloned().unwrap_or_default()
    }
    fn citing(&self, half: &str) -> Vec<String> {
        self.citations.get(half).cloned().unwrap_or_default()
    }
}

impl Responder for TestLog {
    fn holds_record(&self, digest: &str) -> bool {
        self.record_by_digest(digest).is_some()
    }
    fn holds_correlation(&self, id: &str) -> bool {
        self.correlations.contains_key(id)
    }
    fn cites_exchange(&self, half: &str) -> bool {
        self.citations.contains_key(half)
    }
    fn positional_ordering(&self) -> bool {
        true
    }
    fn anchors(&self) -> Vec<Anchor> {
        self.checkpoints
            .iter()
            .map(|cp| Anchor {
                digest: cp.digest(),
                size: leaf_count(cp.mmr_size).unwrap(),
                issued_at: cp.timestamp.clone(),
            })
            .collect()
    }
    fn derivation_forms(&self, derivation: &Derivation) -> Option<Vec<SubjectForm>> {
        match derivation {
            Derivation::Token(t) if t == "history_card/1" => {
                Some(vec![SubjectForm::FullHistory, SubjectForm::Checkpoints])
            }
            _ => None,
        }
    }
}

/// Request bytes (as sent) and the parsed request.
fn request(subject: Value, coverage: Value, extra: Value) -> (Vec<u8>, Request) {
    let mut map = json!({"subject": subject, "coverage": coverage});
    if let Value::Object(e) = extra {
        for (k, v) in e {
            map[k] = v;
        }
    }
    let bytes = serde_json::to_vec(&map).unwrap();
    let parsed = parse_json(&bytes).unwrap();
    (bytes, parsed)
}

fn pin(log: &TestLog, i: usize) -> Value {
    json!({"expected_pin": log.checkpoints[i].digest()})
}

/// Resolve and build as the responder, then verify as the requester.
fn round_trip(
    log: &TestLog,
    bytes: &[u8],
    req: &Request,
) -> Result<answer::VerifiedAnswer, VerifyError> {
    let Resolution::Artifact(anchor) = resolve(req, log) else {
        panic!("expected the request to resolve to an artifact");
    };
    let digest = request_digest(bytes);
    let built = build(
        req,
        &digest,
        &anchor,
        log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    answer::verify(
        &built.envelope,
        &built.artifact,
        &built.material,
        req,
        &digest,
        &responder_key().verifying_key(),
        &record_digest,
    )
}

#[test]
fn every_subject_form_builds_and_verifies() {
    let log = test_log(&responder_key());
    let r3 = log.records[3].digest.clone();

    let (b, q) = request(json!({"record": r3}), pin(&log, 1), json!({"nonce": "n1"}));
    let v = round_trip(&log, &b, &q).unwrap();
    assert_eq!(v.records.len(), 1);
    assert_eq!(v.records[0].body, b"record-3");
    assert_eq!(v.anchor, log.checkpoints[1]);

    let (b, q) = request(json!({"range": [2, 5]}), pin(&log, 1), json!({}));
    let v = round_trip(&log, &b, &q).unwrap();
    assert_eq!(
        v.records.iter().map(|r| r.leaf_index).collect::<Vec<_>>(),
        vec![2, 3, 4, 5]
    );

    let (b, q) = request(
        json!({"full_history": null}),
        json!({"min_freshness": 7}),
        json!({}),
    );
    let v = round_trip(&log, &b, &q).unwrap();
    assert_eq!(
        v.records.len(),
        10,
        "min_freshness picks the freshest anchor"
    );

    let (b, q) = request(json!({"correlation": "c-1"}), pin(&log, 2), json!({}));
    let v = round_trip(&log, &b, &q).unwrap();
    assert_eq!(
        v.records.iter().map(|r| r.leaf_index).collect::<Vec<_>>(),
        vec![1, 5]
    );

    // Only the records under the pinned anchor are served.
    let (b, q) = request(json!({"correlation": "c-1"}), pin(&log, 0), json!({}));
    let v = round_trip(&log, &b, &q).unwrap();
    assert_eq!(
        v.records.iter().map(|r| r.leaf_index).collect::<Vec<_>>(),
        vec![1]
    );

    // An exchange subject pinned by the requester's own half.
    let (b, q) = request(
        json!({"exchange": half()}),
        json!({"expected_pin": half()}),
        json!({}),
    );
    let v = round_trip(&log, &b, &q).unwrap();
    assert_eq!(
        v.records.iter().map(|r| r.leaf_index).collect::<Vec<_>>(),
        vec![2, 8]
    );

    let (b, q) = request(json!({"checkpoints": null}), pin(&log, 2), json!({}));
    let v = round_trip(&log, &b, &q).unwrap();
    assert_eq!(v.checkpoints, log.checkpoints);

    let (b, q) = request(
        json!({"checkpoints": null}),
        pin(&log, 2),
        json!({"derivation": "history_card/1"}),
    );
    let v = round_trip(&log, &b, &q).unwrap();
    assert_eq!(v.checkpoints, log.checkpoints);
    assert!(v.records.is_empty());
}

#[test]
fn artifacts_are_caller_invariant_and_envelopes_are_per_request() {
    let log = test_log(&responder_key());
    let subject = json!({"range": [0, 3]});
    let (b1, q1) = request(
        subject.clone(),
        pin(&log, 0),
        json!({"nonce": "requester-a", "route": "stream"}),
    );
    let (b2, q2) = request(
        subject,
        pin(&log, 0),
        json!({"nonce": "requester-b", "route": "http"}),
    );
    let anchor = ResolvedAnchor::Anchor(log.anchors()[0].clone());
    let a1 = build(
        &q1,
        &request_digest(&b1),
        &anchor,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    let a2 = build(
        &q2,
        &request_digest(&b2),
        &anchor,
        &log,
        MAX_RECORDS,
        &responder_key(),
        "2026-09-29T01:00:00Z",
    )
    .unwrap();
    assert_eq!(a1.artifact, a2.artifact);
    assert_ne!(a1.envelope, a2.envelope);
    // Each envelope verifies only for its own request.
    let key = responder_key().verifying_key();
    assert!(answer::verify(
        &a1.envelope,
        &a1.artifact,
        &a1.material,
        &q1,
        &request_digest(&b1),
        &key,
        &record_digest
    )
    .is_ok());
    assert_eq!(
        answer::verify(
            &a1.envelope,
            &a1.artifact,
            &a1.material,
            &q2,
            &request_digest(&b2),
            &key,
            &record_digest
        ),
        Err(VerifyError::WrongRequest)
    );
}

/// A built answer to a record request under checkpoint 1, and its request.
fn record_answer(log: &TestLog) -> (answer::Answer, Request, String) {
    let (b, q) = request(
        json!({"record": log.records[3].digest}),
        pin(log, 1),
        json!({}),
    );
    let digest = request_digest(&b);
    let Resolution::Artifact(anchor) = resolve(&q, log) else {
        panic!()
    };
    (
        build(
            &q,
            &digest,
            &anchor,
            log,
            MAX_RECORDS,
            &responder_key(),
            ISSUED_AT,
        )
        .unwrap(),
        q,
        digest,
    )
}

fn verify_parts(
    a: &answer::Answer,
    q: &Request,
    digest: &str,
) -> Result<answer::VerifiedAnswer, VerifyError> {
    answer::verify(
        &a.envelope,
        &a.artifact,
        &a.material,
        q,
        digest,
        &responder_key().verifying_key(),
        &record_digest,
    )
}

#[test]
fn wrong_key_wrong_request_and_tampered_artifact_are_caught() {
    let log = test_log(&responder_key());
    let (a, q, digest) = record_answer(&log);
    assert!(verify_parts(&a, &q, &digest).is_ok());

    assert_eq!(
        answer::verify(
            &a.envelope,
            &a.artifact,
            &a.material,
            &q,
            &digest,
            &other_key().verifying_key(),
            &record_digest
        ),
        Err(VerifyError::WrongKey)
    );
    assert_eq!(
        verify_parts(&a, &q, &"0".repeat(64)),
        Err(VerifyError::WrongRequest)
    );

    let mut artifact = a.artifact.clone();
    let last = artifact.len() - 2;
    artifact[last] ^= 1;
    assert_eq!(
        answer::verify(
            &a.envelope,
            &artifact,
            &a.material,
            &q,
            &digest,
            &responder_key().verifying_key(),
            &record_digest
        ),
        Err(VerifyError::ArtifactDigest)
    );

    // Re-labelling the envelope (a different anchor, artifact or time)
    // breaks its signature.
    for member in ["anchor", "artifact_digest", "request_digest"] {
        let mut env = a.envelope.clone();
        env[member] = json!("1".repeat(64));
        assert!(
            verify_parts(
                &answer::Answer {
                    envelope: env,
                    ..a.clone()
                },
                &q,
                &digest
            )
            .is_err(),
            "{member}"
        );
    }
}

#[test]
fn a_responder_that_misstates_its_records_is_caught() {
    // A record whose body does not match its digest.
    let mut log = test_log(&responder_key());
    log.records[3].body = b"something else".to_vec();
    let (a, q, digest) = record_answer(&log);
    assert_eq!(
        verify_parts(&a, &q, &digest),
        Err(VerifyError::RecordDigest)
    );

    // A record served at a position it does not hold.
    let mut log = test_log(&responder_key());
    let impostor = Record {
        leaf_index: 3,
        digest: sha(b"not in the log"),
        body: b"not in the log".to_vec(),
    };
    log.records[3] = impostor;
    let (a, q, digest) = record_answer(&log);
    assert_eq!(
        verify_parts(&a, &q, &digest),
        Err(VerifyError::ProofInvalid)
    );

    // A range with a record left out.
    let log = test_log(&responder_key());
    let (b, q) = request(json!({"range": [2, 5]}), pin(&log, 1), json!({}));
    let digest = request_digest(&b);
    let Resolution::Artifact(anchor) = resolve(&q, &log) else {
        panic!()
    };
    let a = build(
        &q,
        &digest,
        &anchor,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    let mut art: Value = serde_json::from_slice(&a.artifact).unwrap();
    art["records"].as_array_mut().unwrap().remove(1);
    let artifact = capsule_emit_evidence_request::jcs::to_string(&art)
        .unwrap()
        .into_bytes();
    // The responder signs the shortened artifact: the signature is fine, the
    // range is not.
    let env = resign(&a.envelope, &artifact);
    assert_eq!(
        answer::verify(
            &env,
            &artifact,
            &a.material,
            &q,
            &digest,
            &responder_key().verifying_key(),
            &record_digest
        ),
        Err(VerifyError::RecordsMismatch)
    );
}

/// The responder's own envelope over different artifact bytes.
fn resign(envelope: &Value, artifact: &[u8]) -> Value {
    use ed25519_dalek::Signer;
    let mut signed = json!({
        "request_digest": envelope["request_digest"],
        "anchor": envelope["anchor"],
        "artifact_digest": sha(artifact),
        "issued_at": envelope["issued_at"],
    });
    let body = capsule_emit_evidence_request::jcs::to_string(&signed).unwrap();
    signed["key_id"] = envelope["key_id"].clone();
    signed["sig"] = json!(hex::encode(
        responder_key().sign(body.as_bytes()).to_bytes()
    ));
    signed
}

#[test]
fn anchors_must_be_the_pinned_one_fresh_enough_and_the_responders() {
    let log = test_log(&responder_key());

    // Pinned checkpoint 0, answered under checkpoint 2: not the pinned anchor.
    let (b, q) = request(
        json!({"record": log.records[1].digest}),
        pin(&log, 0),
        json!({}),
    );
    let digest = request_digest(&b);
    let other = ResolvedAnchor::Anchor(log.anchors()[2].clone());
    let a = build(
        &q,
        &digest,
        &other,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    assert_eq!(
        verify_parts(&a, &q, &digest),
        Err(VerifyError::CoverageUnmet)
    );

    // min_freshness 8, answered under checkpoint 1 (7 records).
    let (b, q) = request(
        json!({"record": log.records[1].digest}),
        json!({"min_freshness": 8}),
        json!({}),
    );
    let digest = request_digest(&b);
    let stale = ResolvedAnchor::Anchor(log.anchors()[1].clone());
    let a = build(
        &q,
        &digest,
        &stale,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    assert_eq!(
        verify_parts(&a, &q, &digest),
        Err(VerifyError::CoverageUnmet)
    );

    // min_freshness by time, answered under an older checkpoint.
    let (b, q) = request(
        json!({"record": log.records[1].digest}),
        json!({"min_freshness": "2026-09-28T00:00:00Z"}),
        json!({}),
    );
    let digest = request_digest(&b);
    let a = build(
        &q,
        &digest,
        &stale,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    assert_eq!(
        verify_parts(&a, &q, &digest),
        Err(VerifyError::CoverageUnmet)
    );

    // Checkpoints signed by a different key than the answer's.
    let foreign = test_log(&other_key());
    let (a, q, digest) = record_answer(&foreign);
    assert_eq!(
        verify_parts(&a, &q, &digest),
        Err(VerifyError::AnchorInvalid)
    );
}

#[test]
fn proofs_and_checkpoint_chains_are_checked() {
    let log = test_log(&responder_key());

    // Material from another answer: the proof is for another leaf.
    let (a, q, digest) = record_answer(&log);
    let (b2, q2) = request(
        json!({"record": log.records[4].digest}),
        pin(&log, 1),
        json!({}),
    );
    let Resolution::Artifact(anchor) = resolve(&q2, &log) else {
        panic!()
    };
    let a2 = build(
        &q2,
        &request_digest(&b2),
        &anchor,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    assert_eq!(
        verify_parts(
            &answer::Answer {
                material: a2.material,
                ..a.clone()
            },
            &q,
            &digest
        ),
        Err(VerifyError::ProofInvalid)
    );

    // A history card whose middle checkpoint does not chain.
    let mut broken = test_log(&responder_key());
    broken.checkpoints[1].prev_root = "2".repeat(64);
    broken.checkpoints[1].signature =
        sign_checkpoint_digest(&broken.checkpoints[1], &responder_key());
    let (b, q) = request(
        json!({"checkpoints": null}),
        pin(&broken, 2),
        json!({"derivation": "history_card/1"}),
    );
    let digest = request_digest(&b);
    let Resolution::Artifact(anchor) = resolve(&q, &broken) else {
        panic!()
    };
    let a = build(
        &q,
        &digest,
        &anchor,
        &broken,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    assert_eq!(
        verify_parts(&a, &q, &digest),
        Err(VerifyError::CheckpointChain)
    );
}

#[test]
fn a_history_card_with_a_false_consistency_proof_is_caught() {
    // Checkpoints signed and linked correctly, but the consistency proof
    // between two of them altered (and the envelope re-signed by the
    // responder): only the proof check can catch it.
    let log = test_log(&responder_key());
    let (b, q) = request(
        json!({"checkpoints": null}),
        pin(&log, 2),
        json!({"derivation": "history_card/1"}),
    );
    let digest = request_digest(&b);
    let Resolution::Artifact(anchor) = resolve(&q, &log) else {
        panic!()
    };
    let a = build(
        &q,
        &digest,
        &anchor,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    assert!(verify_parts(&a, &q, &digest).is_ok());
    let mut art: Value = serde_json::from_slice(&a.artifact).unwrap();
    let proof = &mut art["history"][2]["consistency_from_previous"];
    let peaks = proof["new_peaks"].as_array_mut().unwrap();
    peaks[0] = json!("3".repeat(64));
    let artifact = capsule_emit_evidence_request::jcs::to_string(&art)
        .unwrap()
        .into_bytes();
    let env = resign(&a.envelope, &artifact);
    assert_eq!(
        answer::verify(
            &env,
            &artifact,
            &a.material,
            &q,
            &digest,
            &responder_key().verifying_key(),
            &record_digest
        ),
        Err(VerifyError::CheckpointChain)
    );
}

#[test]
fn garbage_is_an_error_never_a_panic() {
    let log = test_log(&responder_key());
    let (a, q, digest) = record_answer(&log);
    let key = responder_key().verifying_key();
    for envelope in [json!(null), json!([]), json!({"anchor": 7}), json!({})] {
        assert!(answer::verify(
            &envelope,
            &a.artifact,
            &a.material,
            &q,
            &digest,
            &key,
            &record_digest
        )
        .is_err());
    }
    for material in [
        b"".as_slice(),
        b"null",
        b"{\"anchor_checkpoint\":[]}",
        b"{\"inclusion\":[{\"v\":\"x\"}]}",
    ] {
        assert!(answer::verify(
            &a.envelope,
            &a.artifact,
            material,
            &q,
            &digest,
            &key,
            &record_digest
        )
        .is_err());
    }
}

// ---------------------------------------------------------------------------
// Size limits: nothing a responder claims, and no range a requester asks
// for, makes either side allocate without bound.
// ---------------------------------------------------------------------------

/// A responder-signed checkpoint claiming `leaves` records, with a made-up
/// root.
fn hostile_checkpoint(leaves: u64) -> CheckpointRecord {
    let mut cp = CheckpointRecord {
        v: 1,
        kind: "mmr_checkpoint".into(),
        log_id: STREAM.into(),
        mmr_size: node_count(leaves),
        root: "4".repeat(64),
        prev_size: 0,
        prev_root: String::new(),
        key_id: hex::encode(responder_key().verifying_key().to_bytes()),
        timestamp: "2026-09-29T00:00:00Z".into(),
        signature: String::new(),
        witnesses: Vec::new(),
    };
    cp.signature = sign_checkpoint_digest(&cp, &responder_key());
    cp
}

/// A hand-made answer (signed by the responder) with these parts.
fn hand_answer(
    req_digest: &str,
    cp: &CheckpointRecord,
    art: Value,
    material: Value,
) -> answer::Answer {
    let artifact = capsule_emit_evidence_request::jcs::to_string(&art)
        .unwrap()
        .into_bytes();
    let material = capsule_emit_evidence_request::jcs::to_string(&material)
        .unwrap()
        .into_bytes();
    let base = json!({
        "request_digest": req_digest,
        "anchor": cp.digest(),
        "artifact_digest": "0".repeat(64),
        "issued_at": ISSUED_AT,
        "key_id": hex::encode(responder_key().verifying_key().to_bytes()),
    });
    let envelope = resign(&base, &artifact);
    answer::Answer {
        artifact,
        material,
        envelope,
    }
}

#[test]
fn a_hostile_checkpoint_size_is_refused_before_any_allocation() {
    let cp = hostile_checkpoint(1 << 40);
    let pin = json!({"expected_pin": cp.digest()});
    for subject in [
        json!({"full_history": null}),
        json!({"range": [0, 5_000_000]}),
    ] {
        let (b, q) = request(subject.clone(), pin.clone(), json!({}));
        let digest = request_digest(&b);
        let art = json!({"anchor": cp.digest(), "evidence_stream": STREAM, "subject": subject, "records": []});
        let a = hand_answer(
            &digest,
            &cp,
            art,
            json!({"anchor_checkpoint": serde_json::to_value(&cp).unwrap()}),
        );
        assert_eq!(
            verify_parts(&a, &q, &digest),
            Err(VerifyError::TooLarge),
            "{subject}"
        );
    }
}

#[test]
fn a_range_to_u64_max_is_refused_on_both_sides() {
    let log = test_log(&responder_key());
    let (b, q) = request(json!({"range": [0, u64::MAX]}), pin(&log, 2), json!({}));
    let digest = request_digest(&b);
    // The responder: beyond the tree, and never enumerated.
    let anchor = ResolvedAnchor::Anchor(log.anchors()[2].clone());
    assert_eq!(
        build(
            &q,
            &digest,
            &anchor,
            &log,
            MAX_RECORDS,
            &responder_key(),
            ISSUED_AT
        ),
        Err(BuildError::RecordNotFound)
    );
    // The requester: refused before the records are looked at.
    let cp = &log.checkpoints[2];
    let art = json!({"anchor": cp.digest(), "evidence_stream": STREAM, "subject": {"range": [0, u64::MAX]}, "records": []});
    let a = hand_answer(
        &digest,
        cp,
        art,
        json!({"anchor_checkpoint": serde_json::to_value(cp).unwrap()}),
    );
    assert_eq!(verify_parts(&a, &q, &digest), Err(VerifyError::TooLarge));
}

#[test]
fn build_stops_at_the_limit() {
    let log = test_log(&responder_key());
    let cases = [
        (json!({"range": [0, 5]}), json!({}), 5),
        (json!({"full_history": null}), json!({}), 9),
        (json!({"correlation": "c-1"}), json!({}), 1),
        (json!({"checkpoints": null}), json!({}), 2),
        (
            json!({"checkpoints": null}),
            json!({"derivation": "history_card/1"}),
            2,
        ),
    ];
    for (subject, extra, limit) in cases {
        let (b, q) = request(subject.clone(), pin(&log, 2), extra);
        let digest = request_digest(&b);
        let Resolution::Artifact(anchor) = resolve(&q, &log) else {
            panic!()
        };
        assert_eq!(
            build(
                &q,
                &digest,
                &anchor,
                &log,
                limit,
                &responder_key(),
                ISSUED_AT
            ),
            Err(BuildError::OverLimit),
            "{subject} at limit {limit}"
        );
        // One more is enough.
        assert!(
            build(
                &q,
                &digest,
                &anchor,
                &log,
                limit + 1,
                &responder_key(),
                ISSUED_AT
            )
            .is_ok(),
            "{subject}"
        );
    }
    // A range beyond the tree is not found, whatever the limit.
    let (b, q) = request(json!({"range": [8, 12]}), pin(&log, 2), json!({}));
    let anchor = ResolvedAnchor::Anchor(log.anchors()[2].clone());
    assert_eq!(
        build(
            &q,
            &request_digest(&b),
            &anchor,
            &log,
            MAX_RECORDS,
            &responder_key(),
            ISSUED_AT
        ),
        Err(BuildError::RecordNotFound)
    );
}

#[test]
fn verify_refuses_more_records_or_proofs_than_the_cap() {
    let log = test_log(&responder_key());
    let (b, q) = request(json!({"correlation": "c-1"}), pin(&log, 2), json!({}));
    let digest = request_digest(&b);
    let cp = &log.checkpoints[2];
    let many = || Value::Array(vec![json!({}); MAX_RECORDS as usize + 1]);
    let art = json!({"anchor": cp.digest(), "evidence_stream": STREAM, "subject": {"correlation": "c-1"}, "records": many()});
    let a = hand_answer(
        &digest,
        cp,
        art,
        json!({"anchor_checkpoint": serde_json::to_value(cp).unwrap()}),
    );
    assert_eq!(verify_parts(&a, &q, &digest), Err(VerifyError::TooLarge));
    let art = json!({"anchor": cp.digest(), "evidence_stream": STREAM, "subject": {"correlation": "c-1"}, "records": []});
    let a = hand_answer(
        &digest,
        cp,
        art,
        json!({"anchor_checkpoint": serde_json::to_value(cp).unwrap(), "inclusion": many()}),
    );
    assert_eq!(verify_parts(&a, &q, &digest), Err(VerifyError::TooLarge));
}

// ---------------------------------------------------------------------------
// Checkpoint lists start at the stream's genesis.
// ---------------------------------------------------------------------------

#[test]
fn a_checkpoint_list_with_its_prefix_left_out_is_refused() {
    let log = test_log(&responder_key());
    let anchor = &log.checkpoints[2];
    let cp_json = |cp: &CheckpointRecord| serde_json::to_value(cp).unwrap();
    let (b, q) = request(json!({"checkpoints": null}), pin(&log, 2), json!({}));
    let digest = request_digest(&b);
    for kept in [vec![2], vec![1, 2]] {
        let list: Vec<Value> = kept.iter().map(|&i| cp_json(&log.checkpoints[i])).collect();
        let art = json!({"anchor": anchor.digest(), "evidence_stream": STREAM, "subject": {"checkpoints": null}, "checkpoints": list});
        let a = hand_answer(
            &digest,
            anchor,
            art,
            json!({"anchor_checkpoint": cp_json(anchor)}),
        );
        assert_eq!(
            verify_parts(&a, &q, &digest),
            Err(VerifyError::CheckpointChain),
            "checkpoints {kept:?}"
        );
    }

    let (b, q) = request(
        json!({"checkpoints": null}),
        pin(&log, 2),
        json!({"derivation": "history_card/1"}),
    );
    let digest = request_digest(&b);
    let Resolution::Artifact(resolved) = resolve(&q, &log) else {
        panic!()
    };
    let full = build(
        &q,
        &digest,
        &resolved,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    let full: Value = serde_json::from_slice(&full.artifact).unwrap();
    for drop in [1, 2] {
        let mut art = full.clone();
        let history = art["history"].as_array_mut().unwrap();
        history.drain(..drop);
        // The new first entry claims to be the first: no proof from before.
        history[0]["consistency_from_previous"] = Value::Null;
        let a = hand_answer(
            &digest,
            anchor,
            art,
            json!({"anchor_checkpoint": cp_json(anchor)}),
        );
        assert_eq!(
            verify_parts(&a, &q, &digest),
            Err(VerifyError::CheckpointChain),
            "history without {drop} first"
        );
    }
}

// ---------------------------------------------------------------------------
// More envelope and record-order checks.
// ---------------------------------------------------------------------------

#[test]
fn a_changed_issue_time_breaks_the_envelope() {
    let log = test_log(&responder_key());
    let (a, q, digest) = record_answer(&log);
    let mut env = a.envelope.clone();
    env["issued_at"] = json!("2026-09-30T00:00:00Z");
    assert_eq!(
        verify_parts(&answer::Answer { envelope: env, ..a }, &q, &digest),
        Err(VerifyError::WrongKey)
    );
}

#[test]
fn correlation_records_out_of_log_order_are_refused() {
    let log = test_log(&responder_key());
    let (b, q) = request(json!({"correlation": "c-1"}), pin(&log, 2), json!({}));
    let digest = request_digest(&b);
    let Resolution::Artifact(resolved) = resolve(&q, &log) else {
        panic!()
    };
    let good = build(
        &q,
        &digest,
        &resolved,
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    let mut art: Value = serde_json::from_slice(&good.artifact).unwrap();
    let mut mat: Value = serde_json::from_slice(&good.material).unwrap();
    art["records"].as_array_mut().unwrap().reverse();
    mat["inclusion"].as_array_mut().unwrap().reverse();
    let a = hand_answer(&digest, &log.checkpoints[2], art, mat);
    assert_eq!(
        verify_parts(&a, &q, &digest),
        Err(VerifyError::RecordsMismatch)
    );
}
