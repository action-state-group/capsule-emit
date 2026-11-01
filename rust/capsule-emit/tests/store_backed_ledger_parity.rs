//! The store-backed ledger's cross-language fixture
//! (`test-vectors/store-backed-ledger/`, capsule-emit#399): the appends Rust's
//! `Ledger::open_stores` writes, the log values and artifact records they
//! give and the checkpoint root over them, which the Python port
//! (`capsule_emit.store_backed_ledger`) reproduces byte for byte; and the reverse,
//! a sequence the Python port wrote (its own padding and signed statements),
//! which Rust replays to the same bytes and reopens.
//!
//! `CAPSULE_EMIT_UPDATE_VECTORS=1 cargo test --test store_backed_ledger_parity`
//! rewrites `rust-written.json`; `python-written.json` is Python's
//! (`python/tests/test_store_backed_ledger.py`).

mod common;

use std::path::PathBuf;
use std::sync::Arc;

use capsule_emit::anchor::AnchorClient;
use capsule_emit::capsule::{attach_producer_envelope, payload_bytes, seal, ChainLink};
use capsule_emit::checkpoint::{CheckpointCadenceConfig, CheckpointState};
use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
use capsule_emit::ledger::Ledger;
use capsule_emit::padding::{padding_record_with_nonce, PADDING_CONTENT_TYPE};
use capsule_emit_artifact::{encode_record, Store};
use cll::log::{EntrySource, MAX_SCAN_LIMIT};
use ed25519_dalek::SigningKey;
use serde_json::{json, Value};

const ISSUER: &str = "store-ledger-vectors";
const NAMESPACE: &str = "ledger";
const LOG_ID: &str = "store-ledger-vectors";
const CONTENT_TYPE: &str = "application/vnd.agent-action-capsule+json";

fn key() -> SigningKey {
    SigningKey::from_bytes(&[7u8; 32])
}

fn vectors_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../test-vectors/store-backed-ledger")
}

fn statement(line: &Value, content_type: &str) -> Vec<u8> {
    build_signed_statement(
        &SignedStatementInput {
            payload: &payload_bytes(line),
            issuer: ISSUER,
            subject: line["capsule_id"].as_str().unwrap(),
            content_type,
        },
        &key(),
    )
}

fn capsule(n: usize, parent: Option<&Value>, operator: &str) -> Value {
    let chain = parent.map(|p| ChainLink {
        parent_capsule_id: p["capsule_id"].as_str().unwrap().to_string(),
        relation: "follows".to_string(),
    });
    let mut input = common::capsule_input("store-backed-ledger", n, chain);
    input.operator = operator.to_string();
    let mut capsule = seal(&input).unwrap();
    attach_producer_envelope(&mut capsule, &key()).unwrap();
    capsule
}

fn padding(nonce_byte: u8) -> Value {
    let mut record = padding_record_with_nonce(&hex::encode([nonce_byte; 32])).unwrap();
    attach_producer_envelope(&mut record, &key()).unwrap();
    record
}

fn stores() -> (
    Arc<cll::log::memory::MemoryStore>,
    Arc<dyn Store + Send + Sync>,
) {
    let artifacts = capsule_emit_artifact::memory::MemoryStore::new(
        NAMESPACE,
        vec![key().verifying_key().to_bytes().to_vec()],
    )
    .unwrap();
    artifacts.init().unwrap();
    (
        Arc::new(cll::log::memory::MemoryStore::new()),
        Arc::new(artifacts),
    )
}

/// Replays `steps` on fresh stores; returns the log values, the records as
/// their artifact JSONL wire lines, and the checkpoint over them.
fn replay(steps: &[Value]) -> (Vec<String>, Vec<String>, Value) {
    let (log, artifacts) = stores();
    let (mut ledger, _) = Ledger::open_stores(log.clone(), artifacts.clone()).unwrap();
    for step in steps {
        let line = &step["line"];
        let statement = hex::decode(step["statement"].as_str().unwrap()).unwrap();
        match step["op"].as_str().unwrap() {
            "append" => ledger.append(line, &statement).unwrap(),
            "append_padding" => ledger.append_padding(line, &statement).unwrap(),
            other => panic!("unknown op {other}"),
        }
    }
    let entries: Vec<String> = log
        .scan_entries(0, MAX_SCAN_LIMIT)
        .unwrap()
        .into_iter()
        .map(|e| hex::encode(e.value))
        .collect();
    let records = entries
        .iter()
        .map(|id| encode_record(&artifacts.get(id).unwrap()))
        .collect();
    // The log reopens with every check, and is checkpointed on its backend.
    let (reopened, report) = Ledger::open_stores(log.clone(), artifacts).unwrap();
    assert_eq!(report.valid_entries, steps.len());
    assert_eq!(reopened.lines().len(), steps.len());
    let (mut state, _) =
        CheckpointState::load_log(log, LOG_ID, CheckpointCadenceConfig::default(), &[&key()])
            .unwrap();
    let cut = state
        .reconnect(&key(), &AnchorClient::new("http://127.0.0.1:1"))
        .unwrap()
        .unwrap();
    let checkpoint = json!({
        "leaf_count": cut.leaf_count().unwrap(),
        "mmr_size": cut.mmr_size,
        "root": cut.root,
    });
    (entries, records, checkpoint)
}

fn fixture(steps: Vec<Value>) -> Value {
    let (entries, records, checkpoint) = replay(&steps);
    json!({
        "trusted": [hex::encode(key().verifying_key().to_bytes())],
        "namespace": NAMESPACE,
        "steps": steps,
        "entries": entries,
        "records": records,
        "checkpoint": checkpoint,
    })
}

fn step(op: &str, line: Value, content_type: &str) -> Value {
    let statement = hex::encode(statement(&line, content_type));
    json!({"op": op, "line": line, "statement": statement})
}

#[test]
fn rust_written_fixture_is_current() {
    let r0 = capsule(0, None, "example-operator");
    let r1 = capsule(1, Some(&r0), "example-operator");
    // Non-ASCII text, which serde_json writes unescaped.
    let r2 = capsule(2, Some(&r1), "opérateur ✓ \u{7f}");
    let steps = vec![
        step("append", r0, CONTENT_TYPE),
        step("append", r1, CONTENT_TYPE),
        step("append_padding", padding(0xaa), PADDING_CONTENT_TYPE),
        step("append_padding", padding(0xbb), PADDING_CONTENT_TYPE),
        step("append", r2, CONTENT_TYPE),
    ];
    let mut text = serde_json::to_string_pretty(&fixture(steps)).unwrap();
    text.push('\n');
    let path = vectors_dir().join("rust-written.json");
    if std::env::var_os("CAPSULE_EMIT_UPDATE_VECTORS").is_some() {
        std::fs::write(&path, &text).unwrap();
    }
    let committed = std::fs::read_to_string(&path).unwrap();
    assert_eq!(
        committed, text,
        "rust-written.json is stale: rerun with CAPSULE_EMIT_UPDATE_VECTORS=1"
    );
}

#[test]
fn python_written_fixture_replays_to_the_same_bytes() {
    let path = vectors_dir().join("python-written.json");
    let fixture: Value = capsule_emit::jcs::decode_record(&std::fs::read(path).unwrap()).unwrap();
    let steps = fixture["steps"].as_array().unwrap().clone();
    let (entries, records, checkpoint) = replay(&steps);
    assert_eq!(json!(entries), fixture["entries"]);
    assert_eq!(json!(records), fixture["records"]);
    assert_eq!(checkpoint["root"], fixture["checkpoint"]["root"]);
    assert_eq!(checkpoint["mmr_size"], fixture["checkpoint"]["mmr_size"]);
}
