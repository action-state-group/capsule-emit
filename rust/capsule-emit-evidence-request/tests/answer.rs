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

/// A requester that holds no witness keys.
fn no_witness_key(_ts_url: &str) -> Option<ed25519_dalek::VerifyingKey> {
    None
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
        &no_witness_key,
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
        &record_digest,
        &no_witness_key
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
            &record_digest,
            &no_witness_key
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
        &no_witness_key,
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
            &record_digest,
            &no_witness_key
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
            &record_digest,
            &no_witness_key
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
            &record_digest,
            &no_witness_key
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
            &record_digest,
            &no_witness_key
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
            &record_digest,
            &no_witness_key
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
            &record_digest,
            &no_witness_key
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

// ---------------------------------------------------------------------------
// Regressions for three reported gaps: carried witness receipts, a fixed
// exchange-half pin, and the refusal's CBOR (the last in `src/refusal.rs`).
// ---------------------------------------------------------------------------

const WITNESS: &str = "https://witness.example";

fn witness_signing_key() -> SigningKey {
    SigningKey::from_bytes(&[7u8; 32])
}

fn witness_key(ts_url: &str) -> Option<ed25519_dalek::VerifyingKey> {
    (ts_url == WITNESS).then(|| witness_signing_key().verifying_key())
}

/// The entry a checkpoint's receipt proves: SHA-256 of its digest's bytes.
fn entry_of(cp: &CheckpointRecord) -> [u8; 32] {
    Sha256::digest(hex::decode(cp.digest()).unwrap()).into()
}

fn rfc9162_leaf(entry: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update([0x00]);
    h.update(entry);
    h.finalize().into()
}

fn rfc9162_node(l: &[u8; 32], r: &[u8; 32]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update([0x01]);
    h.update(l);
    h.update(r);
    h.finalize().into()
}

fn split(n: usize) -> usize {
    let mut k = 1;
    while k * 2 < n {
        k *= 2;
    }
    k
}

fn mth(leaves: &[[u8; 32]]) -> [u8; 32] {
    if leaves.len() == 1 {
        return leaves[0];
    }
    let k = split(leaves.len());
    rfc9162_node(&mth(&leaves[..k]), &mth(&leaves[k..]))
}

fn audit_path(leaves: &[[u8; 32]], m: usize) -> Vec<[u8; 32]> {
    if leaves.len() == 1 {
        return Vec::new();
    }
    let k = split(leaves.len());
    if m < k {
        let mut p = audit_path(&leaves[..k], m);
        p.push(mth(&leaves[k..]));
        p
    } else {
        let mut p = audit_path(&leaves[k..], m - k);
        p.push(mth(&leaves[..k]));
        p
    }
}

/// A genuine COSE receipt from `key` proving `entries[index]` in an RFC 9162
/// tree of `entries`.
fn issue_receipt(key: &SigningKey, entries: &[[u8; 32]], index: usize) -> Vec<u8> {
    use coset::cbor::value::Value as C;
    use coset::{CoseSign1Builder, HeaderBuilder, TaggedCborSerializable};
    use ed25519_dalek::Signer;
    let leaves: Vec<[u8; 32]> = entries.iter().map(|e| rfc9162_leaf(e)).collect();
    let root = mth(&leaves);
    let proof = C::Array(vec![
        C::Integer((leaves.len() as u64).into()),
        C::Integer((index as u64).into()),
        C::Array(
            audit_path(&leaves, index)
                .into_iter()
                .map(|h| C::Bytes(h.to_vec()))
                .collect(),
        ),
    ]);
    let mut proof_bytes = Vec::new();
    ciborium::into_writer(&proof, &mut proof_bytes).unwrap();
    let protected = HeaderBuilder::new()
        .algorithm(coset::iana::Algorithm::EdDSA)
        .value(395, C::Integer(1.into()))
        .build();
    let unprotected = HeaderBuilder::new()
        .value(
            396,
            C::Map(vec![(
                C::Integer((-1).into()),
                C::Array(vec![C::Bytes(proof_bytes)]),
            )]),
        )
        .build();
    CoseSign1Builder::new()
        .protected(protected)
        .unprotected(unprotected)
        .create_detached_signature(&root, &[], |tbs| key.sign(tbs).to_bytes().to_vec())
        .build()
        .to_tagged_vec()
        .unwrap()
}

fn witness_record(entry: [u8; 32], receipt: &[u8]) -> cll::checkpoint::WitnessRecord {
    use base64::Engine as _;
    cll::checkpoint::WitnessRecord {
        ts_url: WITNESS.into(),
        entry_hash: hex::encode(entry),
        receipt_b64: base64::engine::general_purpose::STANDARD.encode(receipt),
        leaf_index: 0,
        tree_size: 1,
        is_stub: false,
    }
}

/// The history card pinned at the last checkpoint, built as the responder
/// (with the legitimate responder key) and verified as the requester.
fn history_card(
    log: &TestLog,
    keys: &dyn Fn(&str) -> Option<ed25519_dalek::VerifyingKey>,
) -> Result<answer::VerifiedAnswer, VerifyError> {
    let (b, q) = request(
        json!({"checkpoints": null}),
        pin(log, log.checkpoints.len() - 1),
        json!({"derivation": "history_card/1"}),
    );
    let digest = request_digest(&b);
    let Resolution::Artifact(anchor) = resolve(&q, log) else {
        panic!("expected an artifact")
    };
    let a = build(
        &q,
        &digest,
        &anchor,
        log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    answer::verify(
        &a.envelope,
        &a.artifact,
        &a.material,
        &q,
        &digest,
        &responder_key().verifying_key(),
        &record_digest,
        keys,
    )
}

#[test]
fn a_history_card_carrying_bogus_receipt_bytes_is_refused() {
    // A valid signed checkpoint chain with valid consistency proofs, and a
    // structurally valid witness with bogus receipt bytes on a middle
    // checkpoint (witnesses are outside the checkpoint's signature).
    let mut log = test_log(&responder_key());
    let entry = entry_of(&log.checkpoints[1]);
    log.checkpoints[1]
        .witnesses
        .push(witness_record(entry, b"not a receipt"));
    assert_eq!(
        history_card(&log, &no_witness_key),
        Err(VerifyError::ReceiptMalformed)
    );
    assert_eq!(
        history_card(&log, &witness_key),
        Err(VerifyError::ReceiptMalformed)
    );
}

#[test]
fn a_history_card_carrying_a_valid_receipt_for_another_checkpoint_is_refused() {
    let mut log = test_log(&responder_key());
    let entries: Vec<[u8; 32]> = log.checkpoints.iter().map(entry_of).collect();
    // A genuine receipt from the witness, for checkpoint 0's entry...
    let for_cp0 = issue_receipt(&witness_signing_key(), &entries, 0);
    // ...carried on checkpoint 1 under checkpoint 1's entry hash: caught by
    // its proof and signature under the witness's key.
    log.checkpoints[1]
        .witnesses
        .push(witness_record(entries[1], &for_cp0));
    assert_eq!(
        history_card(&log, &witness_key),
        Err(VerifyError::ReceiptBinding)
    );
    // ...or under checkpoint 0's entry hash: caught by the binding alone.
    log.checkpoints[1].witnesses = vec![witness_record(entries[0], &for_cp0)];
    assert_eq!(
        history_card(&log, &no_witness_key),
        Err(VerifyError::ReceiptBinding)
    );
    assert_eq!(
        history_card(&log, &witness_key),
        Err(VerifyError::ReceiptBinding)
    );
}

#[test]
fn a_receipt_for_its_own_checkpoint_is_verified_only_under_the_witness_key() {
    use answer::ReceiptStatus;
    let mut log = test_log(&responder_key());
    let entries: Vec<[u8; 32]> = log.checkpoints.iter().map(entry_of).collect();
    let for_cp1 = issue_receipt(&witness_signing_key(), &entries, 1);
    log.checkpoints[1]
        .witnesses
        .push(witness_record(entries[1], &for_cp1));
    let mut stub = witness_record(entries[2], b"");
    stub.is_stub = true;
    log.checkpoints[2].witnesses.push(stub);
    let statuses = |v: answer::VerifiedAnswer| -> Vec<ReceiptStatus> {
        v.receipts.into_iter().map(|r| r.status).collect()
    };
    assert_eq!(
        statuses(history_card(&log, &witness_key).unwrap()),
        vec![ReceiptStatus::Verified, ReceiptStatus::Stub]
    );
    assert_eq!(
        statuses(history_card(&log, &no_witness_key).unwrap()),
        vec![ReceiptStatus::NoKey, ReceiptStatus::Stub]
    );
    // Under another key, the same receipt does not verify.
    let other = |_: &str| Some(other_key().verifying_key());
    assert_eq!(history_card(&log, &other), Err(VerifyError::ReceiptBinding));
}

/// Append unrelated records and one more checkpoint to `log`.
fn grow(log: &mut TestLog, checkpoint_key: &SigningKey) {
    let next = log.records.len() as u64;
    for i in next..next + 3 {
        let body = format!("unrelated-{i}").into_bytes();
        let digest = sha(&body);
        let raw: [u8; 32] = hex::decode(&digest).unwrap().try_into().unwrap();
        add_leaf(&mut log.nodes, leaf_hash(&raw)).unwrap();
        log.records.push(Record {
            leaf_index: i,
            digest,
            body,
        });
    }
    let size = log.nodes.size();
    let peak_hashes: Vec<_> = peaks(size)
        .unwrap()
        .iter()
        .map(|&p| log.nodes.node(p))
        .collect();
    let prev = log.checkpoints.last().cloned().unwrap();
    let mut cp = CheckpointRecord {
        v: 1,
        kind: "mmr_checkpoint".into(),
        log_id: STREAM.into(),
        mmr_size: size,
        root: hex::encode(root_from_peaks(&peak_hashes)),
        prev_size: prev.mmr_size,
        prev_root: prev.root.clone(),
        key_id: hex::encode(checkpoint_key.verifying_key().to_bytes()),
        timestamp: "2026-09-30T00:00:00Z".into(),
        signature: String::new(),
        witnesses: Vec::new(),
    };
    cp.signature = sign_checkpoint_digest(&cp, checkpoint_key);
    log.checkpoints.push(cp);
}

#[test]
fn unrelated_log_growth_does_not_change_a_fixed_exchange_half_artifact() {
    // subject={"exchange":H}, coverage={"expected_pin":H}: build and verify,
    // append unrelated records and another checkpoint, answer the same
    // request again with the same returned records; the artifact is the
    // same bytes (§5, fixed-pin artifact invariance).
    let mut log = test_log(&responder_key());
    let (b, q) = request(
        json!({"exchange": half()}),
        json!({"expected_pin": half()}),
        json!({}),
    );
    let digest = request_digest(&b);
    let answer_once = |log: &TestLog| {
        let Resolution::Artifact(anchor) = resolve(&q, log) else {
            panic!("expected an artifact")
        };
        let a = build(
            &q,
            &digest,
            &anchor,
            log,
            MAX_RECORDS,
            &responder_key(),
            ISSUED_AT,
        )
        .unwrap();
        let v = answer::verify(
            &a.envelope,
            &a.artifact,
            &a.material,
            &q,
            &digest,
            &responder_key().verifying_key(),
            &record_digest,
            &no_witness_key,
        )
        .unwrap();
        (a.artifact, v.records)
    };
    let (first, records_first) = answer_once(&log);
    grow(&mut log, &responder_key());
    let (second, records_second) = answer_once(&log);
    assert_eq!(
        records_first, records_second,
        "the returned records are the same"
    );
    assert_eq!(
        first, second,
        "unrelated growth changed a fixed-pin artifact"
    );
}

/// Answer an exchange half pinned by itself; return the artifact, the anchor
/// digest and the verified records.
fn answer_exchange_half(log: &TestLog) -> (Vec<u8>, String, Vec<Record>) {
    let (b, q) = request(
        json!({"exchange": half()}),
        json!({"expected_pin": half()}),
        json!({}),
    );
    let digest = request_digest(&b);
    let Resolution::Artifact(anchor) = resolve(&q, log) else {
        panic!("expected an artifact")
    };
    let a = build(
        &q,
        &digest,
        &anchor,
        log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    let v = answer::verify(
        &a.envelope,
        &a.artifact,
        &a.material,
        &q,
        &digest,
        &responder_key().verifying_key(),
        &record_digest,
        &no_witness_key,
    )
    .unwrap();
    (a.artifact, v.anchor.digest(), v.records)
}

#[test]
fn a_new_citing_record_advances_the_exchange_half_anchor() {
    // Records 2 and 8 cite the half; the earliest checkpoint covering record
    // 8 is checkpoint 2. A new citing record, once a checkpoint covers it,
    // legitimately moves the answer to that checkpoint, and the artifact
    // lists the records it covers.
    let mut log = test_log(&responder_key());
    let (_, anchor_before, records_before) = answer_exchange_half(&log);
    assert_eq!(anchor_before, log.checkpoints[2].digest());
    assert_eq!(
        records_before
            .iter()
            .map(|r| r.leaf_index)
            .collect::<Vec<_>>(),
        vec![2, 8]
    );
    // A new citing record not yet in any checkpoint changes nothing.
    let body = b"citing-10".to_vec();
    let digest = sha(&body);
    let raw: [u8; 32] = hex::decode(&digest).unwrap().try_into().unwrap();
    add_leaf(&mut log.nodes, leaf_hash(&raw)).unwrap();
    log.records.push(Record {
        leaf_index: 10,
        digest: digest.clone(),
        body,
    });
    log.citations.entry(half()).or_default().push(digest);
    let (_, anchor_pending, _) = answer_exchange_half(&log);
    assert_eq!(anchor_pending, anchor_before);
    // Once a checkpoint covers it, the anchor advances to that checkpoint.
    grow(&mut log, &responder_key());
    let (_, anchor_after, records_after) = answer_exchange_half(&log);
    assert_eq!(anchor_after, log.checkpoints.last().unwrap().digest());
    assert_eq!(
        records_after
            .iter()
            .map(|r| r.leaf_index)
            .collect::<Vec<_>>(),
        vec![2, 8, 10]
    );
}

#[test]
fn an_exchange_half_answer_under_a_later_checkpoint_is_refused() {
    // A responder that answers under the latest checkpoint instead of the
    // earliest covering one: the anchor's own prev_size already covers the
    // last served record, so verify refuses it.
    let mut log = test_log(&responder_key());
    grow(&mut log, &responder_key());
    let (b, q) = request(
        json!({"exchange": half()}),
        json!({"expected_pin": half()}),
        json!({}),
    );
    let digest = request_digest(&b);
    let latest = log.anchors().into_iter().last().unwrap();
    let a = build(
        &q,
        &digest,
        &ResolvedAnchor::Anchor(latest),
        &log,
        MAX_RECORDS,
        &responder_key(),
        ISSUED_AT,
    )
    .unwrap();
    assert_eq!(
        answer::verify(
            &a.envelope,
            &a.artifact,
            &a.material,
            &q,
            &digest,
            &responder_key().verifying_key(),
            &record_digest,
            &no_witness_key,
        ),
        Err(VerifyError::CoverageUnmet)
    );
}

/// Replace the unsigned proof array without changing the service's signature.
fn rewrite_receipt_proofs(receipt: &[u8], proofs: Vec<coset::cbor::value::Value>) -> Vec<u8> {
    use coset::{CoseSign1, Label, TaggedCborSerializable};
    let mut sign1 = CoseSign1::from_tagged_slice(receipt).unwrap();
    let (_, vdp) = sign1
        .unprotected
        .rest
        .iter_mut()
        .find(|(label, _)| *label == Label::Int(396))
        .unwrap();
    let (_, array) = vdp
        .as_map_mut()
        .unwrap()
        .iter_mut()
        .find(|(key, _)| key.as_integer().map(i128::from) == Some(-1))
        .unwrap();
    *array = coset::cbor::value::Value::Array(proofs);
    sign1.to_tagged_vec().unwrap()
}

fn receipt_proof(receipt: &[u8]) -> coset::cbor::value::Value {
    use coset::{CoseSign1, Label, TaggedCborSerializable};
    let sign1 = CoseSign1::from_tagged_slice(receipt).unwrap();
    let (_, vdp) = sign1
        .unprotected
        .rest
        .iter()
        .find(|(label, _)| *label == Label::Int(396))
        .unwrap();
    vdp.as_map()
        .unwrap()
        .iter()
        .find(|(key, _)| key.as_integer().map(i128::from) == Some(-1))
        .unwrap()
        .1
        .as_array()
        .unwrap()[0]
        .clone()
}

#[test]
fn receipt_verification_is_independent_of_proof_order() {
    use answer::ReceiptStatus;
    let mut log = test_log(&responder_key());
    let entries: Vec<_> = log.checkpoints.iter().map(entry_of).collect();
    let for_cp0 = issue_receipt(&witness_signing_key(), &entries, 0);
    let for_cp1 = issue_receipt(&witness_signing_key(), &entries, 1);
    for proofs in [
        vec![receipt_proof(&for_cp0), receipt_proof(&for_cp1)],
        vec![receipt_proof(&for_cp1), receipt_proof(&for_cp0)],
    ] {
        let receipt = rewrite_receipt_proofs(&for_cp1, proofs);
        log.checkpoints[1].witnesses = vec![witness_record(entries[1], &receipt)];
        assert_eq!(
            history_card(&log, &witness_key).unwrap().receipts[0].status,
            ReceiptStatus::Verified
        );
        assert_eq!(
            history_card(&log, &no_witness_key).unwrap().receipts[0].status,
            ReceiptStatus::NoKey
        );
    }
    log.checkpoints[1].witnesses = vec![witness_record(entries[1], &for_cp0)];
    assert_eq!(
        history_card(&log, &witness_key),
        Err(VerifyError::ReceiptBinding)
    );
}

#[test]
fn impossible_receipt_proofs_are_refused_even_without_a_witness_key() {
    use coset::cbor::value::Value as C;
    let mut log = test_log(&responder_key());
    let entries: Vec<_> = log.checkpoints.iter().map(entry_of).collect();
    let receipt = issue_receipt(&witness_signing_key(), &entries, 1);
    for (size, index, path) in [
        (0u64, 0u64, vec![]),
        (3, 3, vec![C::Bytes(vec![0; 32]); 2]),
        ((1u64 << 62) + 1, 0, vec![]),
        (3, 1, vec![]),
        (1, 0, vec![C::Bytes(vec![0; 32])]),
    ] {
        let proof = C::Array(vec![
            C::Integer(size.into()),
            C::Integer(index.into()),
            C::Array(path),
        ]);
        let mut bytes = Vec::new();
        ciborium::into_writer(&proof, &mut bytes).unwrap();
        let mutated = rewrite_receipt_proofs(&receipt, vec![C::Bytes(bytes)]);
        log.checkpoints[1].witnesses = vec![witness_record(entries[1], &mutated)];
        for keys in [&no_witness_key as &dyn Fn(&str) -> _, &witness_key] {
            assert_eq!(history_card(&log, keys), Err(VerifyError::ReceiptMalformed));
        }
    }
    // A nested proof must contain exactly one CBOR value, even with a key.
    let mut proof = receipt_proof(&receipt).as_bytes().unwrap().clone();
    proof.push(0xf6);
    let mutated = rewrite_receipt_proofs(&receipt, vec![C::Bytes(proof)]);
    log.checkpoints[1].witnesses = vec![witness_record(entries[1], &mutated)];
    assert_eq!(
        history_card(&log, &no_witness_key),
        Err(VerifyError::ReceiptMalformed)
    );
    assert_eq!(
        history_card(&log, &witness_key),
        Err(VerifyError::ReceiptMalformed)
    );
}
