//! Byte parity across the move of the checkpoint substrate into the
//! `evidencebook` crate.
//!
//! `tests/fixtures/evidencebook_parity.json` was written by this test run
//! against the producer BEFORE the move (its own MMR and checkpoint code
//! over raw `cll`). Its inputs, including the `compute_attestation`
//! extension members that producer committed, are
//! `tests/fixtures/evidencebook_parity_inputs.json`, so the same ids also
//! prove the generic seal reproduces that producer's bytes. The test seals the same capsules with a
//! fixed key and fixed store nonces, appends them to a ledger, cuts two
//! checkpoints, and asserts that every value the fixture pinned is produced
//! again byte for byte: each `capsule_id`, each checkpoint's `mmr_size`,
//! `root`, `prev_size`, `prev_root`, `key_id` and signing digest, the
//! signature over that digest with the checkpoint timestamp fixed, and the
//! inclusion proof of every leaf under the second checkpoint.
//!
//! The live checkpoint timestamp is the wall clock (minute-granular), so it is
//! the one field not pinned; the test re-signs a copy with the timestamp set to
//! `FIXED_TIMESTAMP` and compares that signature instead.
//!
//! Beyond the fixture, the test checks what the live timestamp prevents
//! pinning: the production signature equals an Ed25519 signature over the live
//! digest (Ed25519 is deterministic, so this compares the signing path, not
//! just that some signature verifies), and each checkpoint's persisted
//! `checkpoints.jsonl` line carries that same record plus a COSE wire form
//! that verifies offline and decodes to the record's fields. Not covered:
//! witness registration (no witness URLs are configured) and padding (off);
//! `tests/checkpoint_padding.rs` covers padding.
//!
//! Regenerate only on purpose, and only from a tree whose checkpoint path is
//! known to be the reference:
//! `EVIDENCEBOOK_PARITY_REGENERATE=1 cargo test --test evidencebook_parity`.

use capsule_emit::anchor::AnchorClient;
use capsule_emit::capsule::{payload_bytes, seal, CapsuleInput, ChainLink};
use capsule_emit::checkpoint::{
    inclusion_proof_json, CheckpointCadenceConfig, CheckpointRecord, CheckpointState,
};
use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
use capsule_emit::ledger::Ledger;
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

const FIXED_TIMESTAMP: &str = "2026-09-27T00:00:00Z";
const LOG_ID: &str = "evidencebook-parity-log";
const FIRST_CUT: usize = 4;
const TOTAL: usize = 7;

fn fixture_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/evidencebook_parity.json")
}

/// The pinned inputs (`tests/fixtures/evidencebook_parity_inputs.json`):
/// header strings and each record's extension members, in the order the
/// pinned producer committed them.
fn inputs() -> Value {
    serde_json::from_slice(
        &std::fs::read(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("tests/fixtures/evidencebook_parity_inputs.json"),
        )
        .expect("read the parity inputs"),
    )
    .expect("parse the parity inputs")
}

fn capsule_input(n: usize, parent: Option<&str>) -> CapsuleInput {
    let fx = inputs();
    let text = |k: &str| fx[k].as_str().expect("header string").to_string();
    let record = &fx["records"][n];
    assert_eq!(record["n"], json!(n));
    let mut extensions = Map::new();
    for pair in record["compute_attestation_extensions"]
        .as_array()
        .expect("extension pairs")
    {
        extensions.insert(pair[0].as_str().expect("key").to_string(), pair[1].clone());
    }
    CapsuleInput {
        action_id: format!("{}{n:08}", text("action_id_prefix")),
        action_type: "decide".to_string(),
        operator: text("operator"),
        developer: text("developer"),
        timestamp: "2026-09-27T00:00:00Z".to_string(),
        domain: Some("action".to_string()),
        provenance: Some("collector".to_string()),
        model_id: text("model_id"),
        provider: text("provider"),
        agent_input_digest: format!("{n:064x}"),
        agent_output_digest: Some(format!("{:064x}", n + 1)),
        tool_calls_digest: None,
        reasoning_digest: None,
        host_binding: None,
        runtime: fx["runtime"].clone(),
        compute_attestation_extensions: extensions,
        effect_status: "confirmed".to_string(),
        effect_type: "inference_completion".to_string(),
        effect_request_digest: Some(format!("{n:064x}")),
        effect_response_digest: Some(format!("{:064x}", n + 1)),
        effect_attestation: "gate_executed".to_string(),
        disposition_decision: "accept".to_string(),
        disposition_approver: "policy".to_string(),
        disposition_human_disposed: false,
        disposition_verdict_class: "executed".to_string(),
        chain: parent.map(|p| ChainLink {
            parent_capsule_id: p.to_string(),
            relation: "follows".to_string(),
        }),
        store_nonce: format!("{:064x}", 0x5eed_0000 + n),
    }
}

/// Seal and append capsules `from..to`, chaining from `parent`.
fn append(dir: &Path, key: &SigningKey, from: usize, to: usize, parent: &mut Option<String>) {
    let (mut ledger, _) = Ledger::open(dir).expect("open ledger");
    for n in from..to {
        let capsule = seal(&capsule_input(n, parent.as_deref())).expect("seal");
        let capsule_id = capsule["capsule_id"].as_str().unwrap().to_string();
        let statement = build_signed_statement(
            &SignedStatementInput {
                payload: &payload_bytes(&capsule),
                issuer: "evidencebook-parity-node",
                subject: &capsule_id,
                content_type: "application/vnd.agent-action-capsule+json",
            },
            key,
        );
        ledger.append(&capsule, &statement).expect("append");
        *parent = Some(capsule_id);
    }
}

fn read_capsule_ids(dir: &Path) -> Vec<String> {
    std::fs::read_to_string(dir.join("capsules.jsonl"))
        .unwrap()
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            let v: Value = serde_json::from_str(l).unwrap();
            v["capsule_id"].as_str().unwrap().to_string()
        })
        .collect()
}

/// The pinned view of one checkpoint: everything but the wall-clock
/// timestamp, plus the signing digest and signature with that timestamp fixed.
fn pinned(cp: &CheckpointRecord, key: &SigningKey) -> Value {
    assert!(cp.verify_signature_offline(), "live signature must verify");
    assert_eq!(
        cp.signature,
        hex::encode(key.sign(cp.digest().as_bytes()).to_bytes()),
        "production signature is the Ed25519 signature over the live digest"
    );
    let mut fixed = cp.clone();
    fixed.timestamp = FIXED_TIMESTAMP.to_string();
    fixed.signature = String::new();
    let digest = fixed.digest();
    let signature = hex::encode(key.sign(digest.as_bytes()).to_bytes());
    json!({
        "v": cp.v,
        "kind": cp.kind,
        "log_id": cp.log_id,
        "mmr_size": cp.mmr_size,
        "root": cp.root,
        "prev_size": cp.prev_size,
        "prev_root": cp.prev_root,
        "key_id": cp.key_id,
        "digest_at_fixed_timestamp": digest,
        "signature_at_fixed_timestamp": signature,
        "timestamp_is_minute_granular": capsule_emit::timestamp::is_minute_granular(&cp.timestamp),
    })
}

fn observe() -> Value {
    let dir = tempfile::tempdir().unwrap();
    let key = SigningKey::from_bytes(&[7u8; 32]);
    let anchor = AnchorClient::new("http://127.0.0.1:1"); // no witness urls: never dialled
    let cfg = CheckpointCadenceConfig {
        pad_bucket: 0,
        ..CheckpointCadenceConfig::default()
    };

    let mut parent = None;
    append(dir.path(), &key, 0, FIRST_CUT, &mut parent);
    let (mut state, _) = CheckpointState::load(dir.path(), LOG_ID, cfg.clone()).unwrap();
    let first = state.reconnect(&key, &anchor).unwrap().expect("first cut");
    drop(state);

    append(dir.path(), &key, FIRST_CUT, TOTAL, &mut parent);
    // Reload so the second cut also exercises resuming the durable node
    // store and the previous checkpoint from disk.
    let (mut state, report) = CheckpointState::load(dir.path(), LOG_ID, cfg).unwrap();
    assert_eq!(report.leaves_indexed_this_load, (TOTAL - FIRST_CUT) as u64);
    let second = state.reconnect(&key, &anchor).unwrap().expect("second cut");

    // The persisted lines: the same records, each with a COSE wire form that
    // verifies offline and decodes to the record's own fields.
    let lines = cll::store::read_checkpoints(dir.path().join("checkpoints.jsonl")).unwrap();
    assert_eq!(lines.len(), 2);
    for (line, cp) in lines.iter().zip([&first, &second]) {
        assert_eq!(
            serde_json::to_value(&line.record).unwrap(),
            serde_json::to_value(cp).unwrap()
        );
        let cose = hex::decode(
            line.checkpoint_cose_hex
                .as_ref()
                .expect("COSE form persisted"),
        )
        .unwrap();
        let verified = cll::checkpoint::verify_checkpoint_cose_offline(&cose);
        assert!(verified.ok, "{:?}", verified.errors);
        let decoded = verified.decoded.unwrap();
        assert_eq!(
            (
                decoded.log_id.as_str(),
                decoded.mmr_size,
                decoded.root.as_str()
            ),
            (cp.log_id.as_str(), cp.mmr_size, cp.root.as_str())
        );
        assert_eq!(
            (
                decoded.prev_size,
                decoded.prev_root.as_str(),
                decoded.key_id.as_str()
            ),
            (cp.prev_size, cp.prev_root.as_str(), cp.key_id.as_str())
        );
        assert_eq!(decoded.timestamp, cp.timestamp);
    }

    let ids = read_capsule_ids(dir.path());
    let proofs: Vec<Value> = ids
        .iter()
        .map(|id| {
            let coverage = state.existing_coverage(id).unwrap().expect("covered");
            assert!(!coverage.cut_new);
            assert_eq!(coverage.checkpoint.root, second.root);
            json!({
                "leaf_index": coverage.leaf_index,
                "proof": inclusion_proof_json(&coverage.proof),
            })
        })
        .collect();

    json!({
        "capsule_ids": ids,
        "checkpoints": [pinned(&first, &key), pinned(&second, &key)],
        "inclusion_under_second": proofs,
    })
}

#[test]
fn checkpoint_substrate_output_is_byte_identical_to_the_pre_move_fixture() {
    let observed = observe();
    if std::env::var_os("EVIDENCEBOOK_PARITY_REGENERATE").is_some() {
        let mut text = serde_json::to_string_pretty(&observed).unwrap();
        text.push('\n');
        std::fs::write(fixture_path(), text).unwrap();
        eprintln!("wrote {}", fixture_path().display());
        return;
    }
    let expected: Value =
        serde_json::from_str(&std::fs::read_to_string(fixture_path()).expect("fixture present"))
            .unwrap();
    assert_eq!(observed["capsule_ids"], expected["capsule_ids"]);
    assert_eq!(observed["checkpoints"], expected["checkpoints"]);
    assert_eq!(
        observed["inclusion_under_second"],
        expected["inclusion_under_second"]
    );
}
