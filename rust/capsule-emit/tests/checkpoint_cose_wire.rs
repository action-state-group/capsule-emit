//! The COSE checkpoint statements this crate signs, against the
//! cross-language contract in `tests/vectors/cll-checkpoint/` (copied from
//! checkpointed-local-log): the signed claims payload is the RFC 8949 §4.2.1
//! deterministic encoding every implementation emits, so verifiers that
//! require it accept this crate's checkpoints.

mod common;

use capsule_emit::anchor::AnchorClient;
use capsule_emit::capsule::{seal, ChainLink};
use capsule_emit::checkpoint::{CheckpointCadenceConfig, CheckpointState};
use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
use capsule_emit::ledger::Ledger;
use cll::checkpoint::{
    checkpoint_to_cose, encode_checkpoint_claims, verify_checkpoint_cose_offline, CheckpointRecord,
};
use cll::mmr::{ConsistencyProof, Hash, MemoryNodeStore, NodeReader};
use ed25519_dalek::SigningKey;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::Path;

fn vector(name: &str) -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/vectors/cll-checkpoint")
        .join(name);
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_string())
        .collect()
}

/// `needle` occurs in `haystack`: the claims ride in the statement's payload
/// byte string, unchanged.
fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

/// The claims payload a statement carries: re-encoded from what it decodes
/// to, and found unchanged inside it.
fn signed_claims(cose: &[u8]) -> Vec<u8> {
    let result = verify_checkpoint_cose_offline(cose);
    assert!(result.ok, "statement rejected: {:?}", result.errors);
    let decoded = result.decoded.unwrap();
    let prev_peaks = (decoded.prev_size > 0).then_some(decoded.prev_peak_hashes.as_slice());
    let claims = encode_checkpoint_claims(
        &decoded.to_checkpoint_record(),
        &decoded.new_peak_hashes,
        prev_peaks,
        decoded.consistency_proof.as_ref(),
        decoded.cadence_seconds,
    )
    .unwrap();
    assert!(
        contains(cose, &claims),
        "the statement does not carry these claims"
    );
    claims
}

/// Where `key`'s CBOR text-string encoding first occurs in `bytes` at or after `from`.
fn key_offset(bytes: &[u8], key: &str, from: usize) -> Option<usize> {
    let mut encoded = vec![0x60 | key.len() as u8];
    encoded.extend_from_slice(key.as_bytes());
    bytes[from..]
        .windows(encoded.len())
        .position(|w| w == encoded)
        .map(|at| from + at)
}

/// `keys` occur in `bytes` (from `from` on) in RFC 8949 §4.2.1 order: by their
/// encodings, which for short text keys is shorter first, then lexical. Checked
/// without the encoder under test.
fn assert_deterministic_order(bytes: &[u8], keys: &[&str], from: usize) {
    let mut expected = keys.to_vec();
    expected.sort_by(|a, b| a.len().cmp(&b.len()).then(a.cmp(b)));
    let offsets: Vec<usize> = expected
        .iter()
        .map(|k| key_offset(bytes, k, from).unwrap_or_else(|| panic!("no key {k}")))
        .collect();
    assert!(
        offsets.windows(2).all(|w| w[0] < w[1]),
        "claims keys are not in deterministic order: expected {expected:?} at increasing offsets, got {offsets:?}"
    );
}

#[test]
fn the_cross_language_cose_checkpoint_vectors_hold() {
    let doc = vector("vectors.json");
    let pinned = vector("cose-vectors.json");
    let seed: Hash = hex::decode(doc["signing_key_seed_hex"].as_str().unwrap())
        .unwrap()
        .try_into()
        .unwrap();
    let signing_key = SigningKey::from_bytes(&seed);
    let mut store = MemoryNodeStore::new();
    for seq in 1..=7u64 {
        let digest: Hash =
            Sha256::digest(format!("asg-ledger-mmr-vector-leaf-{seq}").as_bytes()).into();
        cll::mmr::add_leaf(&mut store, cll::mmr::leaf_hash(&digest)).unwrap();
    }
    let peaks = |size: u64| -> Vec<Hash> {
        cll::mmr::peaks(size)
            .unwrap()
            .iter()
            .map(|&p| store.node(p))
            .collect()
    };
    let cases = doc["cases"].as_array().unwrap();
    for vector in pinned["cases"].as_array().unwrap() {
        let name = vector["name"].as_str().unwrap();
        let case = cases
            .iter()
            .find(|c| c["name"] == vector["checkpoint_case"])
            .unwrap();
        let cp = CheckpointRecord {
            v: 1,
            kind: case["kind"].as_str().unwrap().to_string(),
            log_id: case["log_id"].as_str().unwrap().to_string(),
            mmr_size: case["mmr_size"].as_u64().unwrap(),
            root: case["root"].as_str().unwrap().to_string(),
            prev_size: case["prev_size"].as_u64().unwrap(),
            prev_root: case["prev_root"].as_str().unwrap().to_string(),
            key_id: case["key_id"].as_str().unwrap().to_string(),
            timestamp: case["timestamp"].as_str().unwrap().to_string(),
            signature: case["signature"].as_str().unwrap().to_string(),
            witnesses: Vec::new(),
        };
        let proof = case.get("consistency_proof").map(|p| ConsistencyProof {
            v: 1,
            kind: "consistency".to_string(),
            size_a: p["size_a"].as_u64().unwrap(),
            size_b: p["size_b"].as_u64().unwrap(),
            old_peaks: strings(&p["old_peaks"]),
            witness: p["witness"]
                .as_array()
                .unwrap()
                .iter()
                .map(strings)
                .collect(),
            new_peaks: strings(&p["new_peaks"]),
        });
        let new_peaks = peaks(cp.mmr_size);
        let prev_peaks = (cp.prev_size > 0).then(|| peaks(cp.prev_size));
        let claims = hex::decode(vector["claims_hex"].as_str().unwrap()).unwrap();
        let own = checkpoint_to_cose(
            &cp,
            &signing_key,
            &new_peaks,
            prev_peaks.as_deref(),
            proof.as_ref(),
            None,
        )
        .unwrap();
        assert!(
            contains(&own, &claims),
            "{name}: this build signs other claims bytes than every other implementation"
        );
        let reference = hex::decode(vector["cose_hex"].as_str().unwrap()).unwrap();
        assert!(
            verify_checkpoint_cose_offline(&reference).ok,
            "{name}: the reference statement is rejected"
        );
    }
}

/// Two checkpoints cut through this crate's ledger and checkpoint state, the
/// second chained (it carries a consistency proof), and the records they cover.
fn cut_two_checkpoints() -> (Vec<cll::store::CheckpointLine>, Vec<Value>) {
    let dir = tempfile::tempdir().unwrap();
    let key = SigningKey::from_bytes(&[7u8; 32]);
    let (mut ledger, _) = Ledger::open(dir.path()).unwrap();
    let anchor = AnchorClient::new("http://127.0.0.1:1");
    let mut records = Vec::new();
    // Two checkpoints: the second carries a consistency proof (a nested map).
    for round in 0..2 {
        let mut last = String::new();
        for n in 0..2 {
            let chain = ledger.chain_head().map(|parent| ChainLink {
                parent_capsule_id: parent.to_string(),
                relation: "follows".to_string(),
            });
            let mut capsule =
                seal(&common::capsule_input("cose-wire", round * 2 + n, chain)).unwrap();
            capsule_emit::capsule::attach_producer_envelope(&mut capsule, &key).unwrap();
            let statement = build_signed_statement(
                &SignedStatementInput {
                    payload: &serde_json::to_vec(&capsule).unwrap(),
                    issuer: "cose-wire-test",
                    subject: capsule["capsule_id"].as_str().unwrap(),
                    content_type: "application/vnd.agent-action-capsule+json",
                },
                &key,
            );
            ledger.append(&capsule, &statement).unwrap();
            last = capsule["capsule_id"].as_str().unwrap().to_string();
            records.push(capsule);
        }
        let (mut state, _) = CheckpointState::load(
            dir.path(),
            "cose-wire-log",
            CheckpointCadenceConfig::default(),
        )
        .unwrap();
        state.checkpoint_covering(&last, &key, &anchor).unwrap();
    }
    let lines = cll::store::read_checkpoints(dir.path().join("checkpoints.jsonl")).unwrap();
    assert_eq!(lines.len(), 2);
    assert!(lines[1].record.prev_size > 0);
    (lines, records)
}

#[test]
fn checkpoints_this_crate_cuts_sign_deterministic_claims() {
    let (lines, _) = cut_two_checkpoints();
    for line in &lines {
        let cose = hex::decode(line.checkpoint_cose_hex.as_deref().unwrap()).unwrap();
        let claims = signed_claims(&cose);
        let mut keys = vec![
            "kind",
            "log_size",
            "commitment",
            "prev_size",
            "prev_commitment",
            "issued_at",
        ];
        if line.record.prev_size > 0 {
            keys.push("consistency_proof");
            let nested = key_offset(&claims, "consistency_proof", 0).unwrap();
            assert_deterministic_order(
                &claims,
                &["size_a", "size_b", "old_peaks", "witness", "new_peaks"],
                nested,
            );
        }
        assert_deterministic_order(&claims, &keys, 0);
    }
}

/// The TypeScript CLL verifier's timestamp rule (`formatTime(t) === t`): an
/// RFC 3339 UTC time ending in `Z`, whose fraction, if any, has no trailing
/// zero. Ported here so this crate's committed times are held to it.
fn cll_ts_accepts_time(t: &str) -> bool {
    let b = t.as_bytes();
    let digits = |r: std::ops::Range<usize>| {
        b.get(r.clone())
            .is_some_and(|s| s.iter().all(u8::is_ascii_digit))
    };
    let shape = b.len() >= 20
        && digits(0..4)
        && b[4] == b'-'
        && digits(5..7)
        && b[7] == b'-'
        && digits(8..10)
        && b[10] == b'T'
        && digits(11..13)
        && b[13] == b':'
        && digits(14..16)
        && b[16] == b':'
        && digits(17..19)
        && b[b.len() - 1] == b'Z';
    let fraction_ok = match &t[19..t.len() - 1] {
        "" => true,
        f => {
            f.len() >= 2
                && f.len() <= 10
                && f.starts_with('.')
                && f[1..].bytes().all(|c| c.is_ascii_digit())
                && !f.ends_with('0')
        }
    };
    shape && fraction_ok && chrono::DateTime::parse_from_rfc3339(t).is_ok()
}

#[test]
fn the_ported_timestamp_rule_matches_the_typescript_verifier() {
    for ok in [
        "2026-10-01T23:04:00Z",
        "2026-10-01T23:04:00.5Z",
        "2026-10-01T23:04:00.123456Z",
    ] {
        assert!(cll_ts_accepts_time(ok), "{ok}");
    }
    for refused in [
        "2026-10-01T23:04:00.000Z",
        "2026-10-01T23:04:00.120Z",
        "2026-10-01T23:04:00+00:00",
        "2026-10-01 23:04:00Z",
        "2026-13-01T23:04:00Z",
    ] {
        assert!(!cll_ts_accepts_time(refused), "{refused}");
    }
}

#[test]
fn committed_times_are_whole_seconds_the_typescript_verifier_accepts() {
    let (lines, records) = cut_two_checkpoints();
    let whole_second = |t: &str| t.len() == 20 && cll_ts_accepts_time(t);
    for line in &lines {
        let t = &line.record.timestamp;
        assert!(whole_second(t), "checkpoint time {t}");
        // The COSE statement's issued_at is the same string, signed.
        let cose = hex::decode(line.checkpoint_cose_hex.as_deref().unwrap()).unwrap();
        let mut issued_at = vec![0x60 | 9u8];
        issued_at.extend_from_slice(b"issued_at");
        issued_at.push(0x60 | t.len() as u8);
        issued_at.extend_from_slice(t.as_bytes());
        assert!(contains(&cose, &issued_at), "issued_at is not {t}");
    }
    for record in &records {
        let t = record["timestamp"].as_str().unwrap();
        assert!(whole_second(t), "record time {t}");
    }
}
