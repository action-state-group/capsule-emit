//! A ledger kept in a CLL entry log plus an artifact store
//! (`Ledger::open_stores`, `CheckpointState::load_log`; capsule-emit#341):
//! the same history as the flat layout, each guarantee the flat ledger keeps,
//! and checkpoints an Evidence Bundle producer reads from the CLL backend.

use std::path::Path;
use std::sync::Arc;

use aac_bundle::bundle::{
    build_evidence_bundle, proof_source_from_cll, BuildOptions, Interval, RecordSource,
    StoredRecord,
};
use capsule_emit::anchor::AnchorClient;
use capsule_emit::capsule::{
    attach_producer_envelope, payload_bytes, seal, CapsuleInput, ChainLink,
};
use capsule_emit::checkpoint::{
    verify_inclusion, CheckpointCadenceConfig, CheckpointLog, CheckpointState, CheckpointStateError,
};
use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
use capsule_emit::ledger::{Ledger, LedgerError, LedgerLine};
use capsule_emit::padding::{padding_record_with_nonce, PADDING_CONTENT_TYPE};
use capsule_emit_artifact::{ArtifactError, Store};
use cll::log::{
    AppendInput, CheckpointStateStore, EntrySource, EntryStore, Timestamp, MAX_SCAN_LIMIT,
};
use ed25519_dalek::SigningKey;
use serde_json::{json, Map, Value};

const ISSUER: &str = "stores-test";
const LOG_ID: &str = "stores-test-log";
const NAMESPACE: &str = "ledger";
const CONTENT_TYPE: &str = "application/vnd.agent-action-capsule+json";

type Log = Arc<dyn EntryStore + Send + Sync>;
type Artifacts = Arc<dyn Store + Send + Sync>;

fn key() -> SigningKey {
    SigningKey::from_bytes(&[7u8; 32])
}

fn other_key() -> SigningKey {
    SigningKey::from_bytes(&[8u8; 32])
}

fn trusted() -> Vec<Vec<u8>> {
    [key(), other_key()]
        .iter()
        .map(|k| k.verifying_key().to_bytes().to_vec())
        .collect()
}

fn anchor() -> AnchorClient {
    AnchorClient::new("http://127.0.0.1:1")
}

fn statement(line: &Value, signer: &SigningKey, content_type: &str) -> Vec<u8> {
    build_signed_statement(
        &SignedStatementInput {
            payload: &payload_bytes(line),
            issuer: ISSUER,
            subject: line["capsule_id"].as_str().unwrap(),
            content_type,
        },
        signer,
    )
}

fn input(n: usize, chain: Option<ChainLink>) -> CapsuleInput {
    CapsuleInput {
        action_id: format!("example/stores/{n:08}"),
        action_type: "decide".to_string(),
        operator: "example-operator".to_string(),
        developer: "capsule-emit/stores-test".to_string(),
        timestamp: format!("2026-10-20T00:{n:02}:00Z"),
        domain: Some("action".to_string()),
        provenance: Some("collector".to_string()),
        model_id: "example-model".to_string(),
        provider: "example-provider".to_string(),
        agent_input_digest: format!("{n:064x}"),
        agent_output_digest: Some(format!("{:064x}", n + 1)),
        tool_calls_digest: None,
        reasoning_digest: None,
        host_binding: None,
        runtime: json!({"name": "stores-runtime"}),
        compute_attestation_extensions: Map::new(),
        effect_status: "confirmed".to_string(),
        effect_type: "inference_completion".to_string(),
        effect_request_digest: Some(format!("{n:064x}")),
        effect_response_digest: Some(format!("{:064x}", n + 1)),
        effect_attestation: "gate_executed".to_string(),
        disposition_decision: "accept".to_string(),
        disposition_approver: "policy".to_string(),
        disposition_human_disposed: false,
        disposition_verdict_class: "executed".to_string(),
        chain,
        store_nonce: format!("{:064x}", 0x5eed_0000 + n),
    }
}

/// Capsule `n`, chained to `parent`, with the producer envelope of `signer`.
fn capsule(n: usize, parent: Option<&str>, signer: &SigningKey) -> Value {
    let chain = parent.map(|p| ChainLink {
        parent_capsule_id: p.to_string(),
        relation: "follows".to_string(),
    });
    let mut capsule = seal(&input(n, chain)).unwrap();
    attach_producer_envelope(&mut capsule, signer).unwrap();
    capsule
}

/// A padding record with a fixed nonce, so two ledgers can hold the same one.
fn padding(nonce_byte: u8) -> Value {
    let mut record = padding_record_with_nonce(&hex::encode([nonce_byte; 32])).unwrap();
    attach_producer_envelope(&mut record, &key()).unwrap();
    record
}

fn id(line: &Value) -> String {
    line["capsule_id"].as_str().unwrap().to_string()
}

fn append(ledger: &mut Ledger, line: &Value) {
    ledger
        .append(line, &statement(line, &key(), CONTENT_TYPE))
        .unwrap();
}

fn append_padding(ledger: &mut Ledger, record: &Value) {
    ledger
        .append_padding(record, &statement(record, &key(), PADDING_CONTENT_TYPE))
        .unwrap();
}

/// The CLL SQLite log and the SQLite artifact store, in one database file.
fn sqlite(dir: &Path) -> (Arc<cll::log::sqlite::SqliteStore>, Artifacts) {
    let db = dir.join("ledger.db");
    cll::log::sqlite::SqliteStore::init(&db, LOG_ID).unwrap();
    let log = Arc::new(cll::log::sqlite::SqliteStore::open(&db, LOG_ID).unwrap());
    let artifacts = capsule_emit_artifact::sqlite::SqliteStore::new(
        rusqlite::Connection::open(&db).unwrap(),
        NAMESPACE,
        trusted(),
    )
    .unwrap();
    artifacts.init().unwrap();
    (log, Arc::new(artifacts))
}

/// A memory log and a memory artifact store.
fn memory() -> (Arc<cll::log::memory::MemoryStore>, Artifacts) {
    let artifacts = capsule_emit_artifact::memory::MemoryStore::new(NAMESPACE, trusted()).unwrap();
    artifacts.init().unwrap();
    (
        Arc::new(cll::log::memory::MemoryStore::new()),
        Arc::new(artifacts),
    )
}

fn open(log: &Log, artifacts: &Artifacts) -> Result<Ledger, LedgerError> {
    Ledger::open_stores(log.clone(), artifacts.clone()).map(|(ledger, _)| ledger)
}

fn entries(log: &dyn EntrySource) -> Vec<String> {
    log.scan_entries(0, MAX_SCAN_LIMIT)
        .unwrap()
        .into_iter()
        .map(|e| hex::encode(e.value))
        .collect()
}

/// The parts of a line both layouts share (its offset is a byte offset on
/// one and a seq on the other).
fn shape(lines: &[LedgerLine]) -> Vec<(String, bool, Option<String>)> {
    lines
        .iter()
        .map(|l| (l.capsule_id.clone(), l.padding, l.timestamp.clone()))
        .collect()
}

/// Two records, two padding records, one more record: the same sequence on
/// any ledger, byte for byte.
fn write_sequence(ledger: &mut Ledger) -> Vec<Value> {
    let r0 = capsule(0, None, &key());
    let r1 = capsule(1, Some(&id(&r0)), &key());
    let p0 = padding(0xaa);
    let p1 = padding(0xbb);
    let r2 = capsule(2, Some(&id(&r1)), &key());
    append(ledger, &r0);
    append(ledger, &r1);
    append_padding(ledger, &p0);
    append_padding(ledger, &p1);
    append(ledger, &r2);
    vec![r0, r1, p0, p1, r2]
}

fn cfg() -> CheckpointCadenceConfig {
    CheckpointCadenceConfig::default()
}

fn load_log(
    log: Arc<dyn CheckpointLog>,
    signers: &[&dyn capsule_emit::checkpoint::CheckpointSigner],
) -> Result<CheckpointState, CheckpointStateError> {
    CheckpointState::load_log(log, LOG_ID, cfg(), signers).map(|(state, _)| state)
}

// -- the same history on both layouts ------------------------------------------

#[test]
fn flat_and_sqlite_give_the_same_ids_and_checkpoint_root() {
    let dir = tempfile::tempdir().unwrap();
    let flat_dir = dir.path().join("flat");
    let (mut flat, _) = Ledger::open(&flat_dir).unwrap();
    let written = write_sequence(&mut flat);

    let (log, artifacts) = sqlite(dir.path());
    let entry_log: Log = log.clone();
    let mut stored = open(&entry_log, &artifacts).unwrap();
    assert_eq!(write_sequence(&mut stored), written);

    assert_eq!(shape(flat.lines()), shape(stored.lines()));
    assert_eq!(flat.chain_head(), stored.chain_head());
    assert_eq!(flat.chain_head(), Some(id(&written[4]).as_str()));
    assert_eq!(flat.known_capsule_ids(), stored.known_capsule_ids());
    assert_eq!(flat.capsule_ids_in_order(), stored.capsule_ids_in_order());
    assert_eq!(
        entries(log.as_ref()),
        written.iter().map(id).collect::<Vec<_>>()
    );

    let (mut flat_cp, _) = CheckpointState::load(&flat_dir, LOG_ID, cfg()).unwrap();
    let flat_cut = flat_cp.reconnect(&key(), &anchor()).unwrap().unwrap();
    let mut stored_cp = load_log(log.clone(), &[&key()]).unwrap();
    let stored_cut = stored_cp.reconnect(&key(), &anchor()).unwrap().unwrap();
    assert_eq!(flat_cut.root, stored_cut.root);
    assert_eq!(flat_cut.mmr_size, stored_cut.mmr_size);
    assert_eq!(stored_cut.leaf_count().unwrap(), 5);

    // The CLL and artifact JSONL backends give the same history and root.
    let jsonl_dir = dir.path().join("jsonl");
    std::fs::create_dir(&jsonl_dir).unwrap();
    let (journal, jsonl_artifacts) = jsonl(&jsonl_dir, true);
    let journal_log: Log = journal.clone();
    let mut on_jsonl = open(&journal_log, &jsonl_artifacts).unwrap();
    assert_eq!(write_sequence(&mut on_jsonl), written);
    assert_eq!(shape(flat.lines()), shape(on_jsonl.lines()));
    let mut jsonl_cp = load_log(journal, &[&key()]).unwrap();
    let jsonl_cut = jsonl_cp.reconnect(&key(), &anchor()).unwrap().unwrap();
    assert_eq!(jsonl_cut.root, flat_cut.root);
    assert_eq!(jsonl_cut.mmr_size, flat_cut.mmr_size);

    // The checkpoint is in the CLL backend's own state.
    let state = log.load_cll().unwrap();
    assert_eq!(state.indexed_seq, 5);
    assert_eq!(state.checkpoint.unwrap().size, stored_cut.mmr_size);
}

#[test]
fn reopen_keeps_lines_and_reads_records_back() {
    let (log, artifacts) = memory();
    let log: Log = log;
    let mut ledger = open(&log, &artifacts).unwrap();
    let written = write_sequence(&mut ledger);
    let before = ledger.lines().to_vec();
    assert_eq!(
        before.iter().map(|l| l.offset).collect::<Vec<_>>(),
        vec![1, 2, 3, 4, 5],
        "a store-backed line's offset is its seq"
    );
    drop(ledger);

    let (reopened, report) = Ledger::open_stores(log.clone(), artifacts.clone()).unwrap();
    assert_eq!(report.valid_entries, 5);
    assert_eq!(report.truncated_torn_write_at, None);
    assert_eq!(reopened.lines(), &before[..]);
    for (line, value) in reopened.lines().iter().zip(&written) {
        assert_eq!(&reopened.read_line(line.offset).unwrap(), value);
        assert_eq!(
            reopened.read_statement(&line.capsule_id).unwrap(),
            statement(
                value,
                &key(),
                if line.padding {
                    PADDING_CONTENT_TYPE
                } else {
                    CONTENT_TYPE
                }
            )
        );
    }
    let entry = reopened.lookup(&id(&written[1])).unwrap().unwrap();
    assert_eq!(entry.capsule, written[1]);
    assert_eq!(
        entry.signed_statement,
        statement(&written[1], &key(), CONTENT_TYPE)
    );
    assert!(reopened.lookup(&"0".repeat(64)).unwrap().is_none());
}

// -- chain-head linkage ------------------------------------------------------------

#[test]
fn chain_head_mismatch_is_refused_and_writes_nothing() {
    let (log, artifacts) = memory();
    let log: Log = log;
    let mut ledger = open(&log, &artifacts).unwrap();
    let r0 = capsule(0, None, &key());
    append(&mut ledger, &r0);

    let wrong_parent = capsule(1, Some(&"f".repeat(64)), &key());
    let no_parent = capsule(2, None, &key());
    for bad in [&wrong_parent, &no_parent] {
        let err = ledger
            .append(bad, &statement(bad, &key(), CONTENT_TYPE))
            .unwrap_err();
        assert!(
            matches!(err, LedgerError::AppendChainMismatch { .. }),
            "{err}"
        );
        assert!(matches!(
            artifacts.get(&id(bad)),
            Err(ArtifactError::NotFound)
        ));
    }
    assert_eq!(entries(log.as_ref()), vec![id(&r0)]);
}

#[test]
fn a_line_open_would_refuse_is_refused_before_anything_is_written() {
    let (log, artifacts) = memory();
    let log: Log = log;
    let mut ledger = open(&log, &artifacts).unwrap();
    let r0 = capsule(0, None, &key());

    // A statement over another capsule.
    let other = capsule(9, None, &key());
    let err = ledger
        .append(&r0, &statement(&other, &key(), CONTENT_TYPE))
        .unwrap_err();
    assert!(matches!(err, LedgerError::StatementInvalid { .. }), "{err}");

    // A key_id that is not the envelope's signer, with a statement that
    // key_id's key did sign: only the envelope check refuses it.
    let mut wrong_key = r0.clone();
    wrong_key["key_id"] = json!(hex::encode(other_key().verifying_key().to_bytes()));
    let err = ledger
        .append(
            &wrong_key,
            &statement(&wrong_key, &other_key(), CONTENT_TYPE),
        )
        .unwrap_err();
    assert!(matches!(err, LedgerError::Envelope { .. }), "{err}");

    // An envelope in uppercase hex, which would read back lowercase.
    let mut upper = r0.clone();
    upper["signature"] = json!(r0["signature"].as_str().unwrap().to_uppercase());
    let err = ledger
        .append(&upper, &statement(&upper, &key(), CONTENT_TYPE))
        .unwrap_err();
    assert!(matches!(err, LedgerError::Envelope { .. }), "{err}");

    // No producer envelope at all.
    let mut unsigned = r0.clone();
    unsigned.as_object_mut().unwrap().remove("signature");
    unsigned.as_object_mut().unwrap().remove("key_id");
    let err = ledger
        .append(&unsigned, &statement(&unsigned, &key(), CONTENT_TYPE))
        .unwrap_err();
    assert!(matches!(err, LedgerError::Envelope { .. }), "{err}");

    // A tampered capsule whose stored id no longer recomputes.
    let mut tampered = r0.clone();
    tampered["action_id"] = json!("example/stores/tampered");
    let err = ledger
        .append(&tampered, &statement(&tampered, &key(), CONTENT_TYPE))
        .unwrap_err();
    assert!(
        matches!(err, LedgerError::CapsuleIdMismatch { .. }),
        "{err}"
    );

    assert!(entries(log.as_ref()).is_empty());
    assert!(matches!(
        artifacts.get(&id(&r0)),
        Err(ArtifactError::NotFound)
    ));
    assert!(ledger.lines().is_empty());

    // The corrected line goes through, and the ledger reopens.
    append(&mut ledger, &r0);
    assert_eq!(
        open(&log, &artifacts).unwrap().chain_head(),
        Some(id(&r0).as_str())
    );
}

/// The record a store-backed ledger writes for `line`: what a crash between
/// the record's `put` and the log's `append` leaves behind.
fn orphan_record(line: &Value) -> capsule_emit_artifact::Record {
    let mut object = line.as_object().unwrap().clone();
    let signature = object.remove("signature").unwrap();
    object.remove("key_id");
    capsule_emit_artifact::Record {
        capsule_id: id(line),
        capsule: serde_json::to_vec(&Value::Object(object)).unwrap(),
        producer_envelope: hex::decode(signature.as_str().unwrap()).unwrap(),
        artifacts: vec![capsule_emit_artifact::Artifact {
            name: capsule_emit::ledger::STATEMENT.to_string(),
            binding: String::new(),
            content: Some(statement(line, &key(), CONTENT_TYPE)),
            state: capsule_emit_artifact::RetentionState::Present,
            content_sha256: String::new(),
        }],
    }
}

#[test]
fn a_crash_between_the_two_writes_leaves_a_record_the_retry_reuses() {
    let (log, artifacts) = memory();
    let log: Log = log;
    let mut ledger = open(&log, &artifacts).unwrap();
    let r0 = capsule(0, None, &key());
    append(&mut ledger, &r0);
    let r1 = capsule(1, Some(&id(&r0)), &key());
    artifacts.put(&orphan_record(&r1)).unwrap();
    drop(ledger);

    // The record no entry names is not a line.
    let mut reopened = open(&log, &artifacts).unwrap();
    assert_eq!(reopened.entries(), 1);
    assert_eq!(reopened.chain_head(), Some(id(&r0).as_str()));

    // The retry's identical put is accepted, and the entry commits the line.
    append(&mut reopened, &r1);
    assert_eq!(entries(log.as_ref()), vec![id(&r0), id(&r1)]);
    let again = open(&log, &artifacts).unwrap();
    assert_eq!(again.chain_head(), Some(id(&r1).as_str()));
    assert_eq!(again.lookup(&id(&r1)).unwrap().unwrap().capsule, r1);
}

#[test]
fn a_line_already_in_the_log_is_refused() {
    let (log, artifacts) = memory();
    let log: Log = log;
    let mut ledger = open(&log, &artifacts).unwrap();
    let p = padding(0xcc);
    append_padding(&mut ledger, &p);
    let err = ledger
        .append_padding(&p, &statement(&p, &key(), PADDING_CONTENT_TYPE))
        .unwrap_err();
    assert!(matches!(err, LedgerError::AlreadyInLog { .. }), "{err}");
    assert_eq!(ledger.entries(), 1);
}

// -- torn tail and real corruption -----------------------------------------------

/// The JSONL backends: CLL's journal and the artifact store's file.
fn jsonl(dir: &Path, provision: bool) -> (Arc<cll::log::jsonl::JsonlStore>, Artifacts) {
    let journal = dir.join("log.jsonl");
    if provision {
        cll::log::jsonl::JsonlStore::init(&journal).unwrap();
    }
    let log = cll::log::jsonl::JsonlStore::open(&journal).unwrap();
    let artifacts = capsule_emit_artifact::jsonl::JsonlStore::new(
        dir.join("artifacts.jsonl"),
        NAMESPACE,
        trusted(),
    )
    .unwrap();
    artifacts.init().unwrap();
    (Arc::new(log), Arc::new(artifacts))
}

fn append_bytes(path: &Path, bytes: &[u8]) {
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new().append(true).open(path).unwrap();
    file.write_all(bytes).unwrap();
}

#[test]
fn a_torn_tail_reopens_clean_and_real_corruption_is_a_hard_error() {
    let dir = tempfile::tempdir().unwrap();
    let r0 = capsule(0, None, &key());
    let r1 = capsule(1, Some(&id(&r0)), &key());
    {
        let (log, artifacts) = jsonl(dir.path(), true);
        let log: Log = log;
        let mut ledger = open(&log, &artifacts).unwrap();
        append(&mut ledger, &r0);
        append(&mut ledger, &r1);
    }
    // A crash mid-write in each layer: a last line with no newline.
    append_bytes(
        &dir.path().join("log.jsonl"),
        br#"{"type":"cll.append","seq":3"#,
    );
    append_bytes(
        &dir.path().join("artifacts.jsonl"),
        br#"{"capsule_id":"dead"#,
    );

    // CLL init truncates its torn tail; the artifact store never indexes one.
    let (log, artifacts) = jsonl(dir.path(), true);
    let log: Log = log;
    let mut ledger = open(&log, &artifacts).unwrap();
    assert_eq!(ledger.chain_head(), Some(id(&r1).as_str()));
    let r2 = capsule(2, Some(&id(&r1)), &key());
    append(&mut ledger, &r2);
    drop(ledger);
    drop((log, artifacts));
    let (log, artifacts) = jsonl(dir.path(), false);
    let log: Log = log;
    assert_eq!(open(&log, &artifacts).unwrap().entries(), 3);
    drop((log, artifacts));

    // A complete artifact line rewritten in place: r1's record is gone from
    // the store while the log still names it.
    let path = dir.path().join("artifacts.jsonl");
    let text = std::fs::read_to_string(&path).unwrap();
    std::fs::write(&path, text.replace(&id(&r1), &"e".repeat(64))).unwrap();
    let (log, artifacts) = jsonl(dir.path(), false);
    let log: Log = log;
    let err = open(&log, &artifacts).unwrap_err();
    assert!(
        matches!(err, LedgerError::MissingRecord { line: 2, .. }),
        "{err}"
    );
}

#[test]
fn an_entry_without_a_record_is_a_hard_error() {
    let (log, artifacts) = memory();
    let entry_log: Log = log.clone();
    let mut ledger = open(&entry_log, &artifacts).unwrap();
    append(&mut ledger, &capsule(0, None, &key()));
    log.append(&AppendInput {
        value: vec![0x42; 32],
        appended_at: Timestamp::now(),
    })
    .unwrap();
    let err = open(&entry_log, &artifacts).unwrap_err();
    assert!(
        matches!(err, LedgerError::MissingRecord { line: 2, .. }),
        "{err}"
    );
}

#[test]
fn a_purged_ledger_record_makes_reopen_fail() {
    let (log, artifacts) = memory();
    let log: Log = log;
    let mut ledger = open(&log, &artifacts).unwrap();
    let r0 = capsule(0, None, &key());
    append(&mut ledger, &r0);
    artifacts.purge(&id(&r0)).unwrap();
    let err = open(&log, &artifacts).unwrap_err();
    assert!(
        matches!(err, LedgerError::StatementInvalid { line: 1, .. }),
        "{err}"
    );
}

// -- padding outside the chain -----------------------------------------------------

#[test]
fn padding_is_a_leaf_outside_the_chain() {
    let (log, artifacts) = memory();
    let log: Log = log;
    let mut ledger = open(&log, &artifacts).unwrap();
    let r0 = capsule(0, None, &key());
    append(&mut ledger, &r0);
    assert_eq!(ledger.pad_to_bucket(4, &key(), ISSUER).unwrap(), 4);
    assert_eq!(
        ledger.pad_to_bucket(4, &key(), ISSUER).unwrap(),
        4,
        "never pads twice"
    );

    for l in [&ledger, &open(&log, &artifacts).unwrap()] {
        assert_eq!(l.entries(), 4);
        assert_eq!(l.chain_head(), Some(id(&r0).as_str()));
        assert_eq!(l.len(), 1);
        assert_eq!(
            l.lines().iter().map(|x| x.padding).collect::<Vec<_>>(),
            vec![false, true, true, true]
        );
        for line in &l.lines()[1..] {
            assert!(!l.contains(&line.capsule_id));
            assert!(l.lookup(&line.capsule_id).unwrap().is_none());
            assert_eq!(l.read_line(line.offset).unwrap()["record_type"], "padding");
        }
    }
    // The next record still chains to the last real record.
    let r1 = capsule(1, Some(&id(&r0)), &key());
    append(&mut ledger, &r1);
    assert_eq!(ledger.chain_head(), Some(id(&r1).as_str()));
}

// -- checkpoints kept in the CLL backend ------------------------------------------

#[test]
fn a_reopened_checkpoint_is_the_one_first_cut() {
    let (log, artifacts) = memory();
    let entry_log: Log = log.clone();
    let mut ledger = open(&entry_log, &artifacts).unwrap();
    write_sequence(&mut ledger);
    let mut state = load_log(log.clone(), &[&key()]).unwrap();
    let cut = state.reconnect(&key(), &anchor()).unwrap().unwrap();
    assert!(cut.verify_signature_offline());

    let reopened = load_log(log.clone(), &[&key()]).unwrap();
    let rebuilt = reopened.last_checkpoint().unwrap();
    assert_eq!(rebuilt, &cut);
    assert_eq!(rebuilt.digest(), cut.digest());
    assert_eq!(rebuilt.signature, cut.signature);
}

#[test]
fn an_uncheckpointed_tail_reopens_and_older_coverage_is_reused() {
    let (log, artifacts) = memory();
    let entry_log: Log = log.clone();
    let mut ledger = open(&entry_log, &artifacts).unwrap();
    let written = write_sequence(&mut ledger);
    let mut state = load_log(log.clone(), &[&key()]).unwrap();
    let cut = state.reconnect(&key(), &anchor()).unwrap().unwrap();
    let tail = capsule(3, Some(&id(&written[4])), &key());
    append(&mut ledger, &tail);

    let (mut reopened, report) =
        CheckpointState::load_log(log.clone(), LOG_ID, cfg(), &[&key()]).unwrap();
    assert_eq!(report.leaves_indexed_this_load, 1);
    assert_eq!(reopened.leaf_count(), 6);
    let old = reopened
        .existing_coverage(&id(&written[0]))
        .unwrap()
        .unwrap();
    assert_eq!(old.checkpoint, cut);
    let root: [u8; 32] = hex::decode(&old.checkpoint.root)
        .unwrap()
        .try_into()
        .unwrap();
    let leaf: [u8; 32] = hex::decode(id(&written[0])).unwrap().try_into().unwrap();
    assert!(verify_inclusion(
        &root,
        old.checkpoint.mmr_size,
        old.leaf_index,
        &leaf,
        &old.proof
    ));
    assert!(reopened.existing_coverage(&id(&tail)).unwrap().is_none());

    // A push of the tail cuts a new checkpoint, committed to the backend.
    let coverage = reopened
        .checkpoint_covering(&id(&tail), &key(), &anchor())
        .unwrap();
    assert!(coverage.cut_new);
    assert_eq!(coverage.checkpoint.prev_size, cut.mmr_size);
    assert_eq!(log.load_cll().unwrap().checkpoint.unwrap().indexed_seq, 6);
    let again = load_log(log.clone(), &[&key()]).unwrap();
    assert_eq!(again.last_checkpoint(), Some(&coverage.checkpoint));
}

#[test]
fn a_record_appended_after_load_is_folded_by_seq() {
    let (log, artifacts) = memory();
    let entry_log: Log = log.clone();
    let mut ledger = open(&entry_log, &artifacts).unwrap();
    let r0 = capsule(0, None, &key());
    append(&mut ledger, &r0);
    let mut state = load_log(log.clone(), &[&key()]).unwrap();
    let r1 = capsule(1, Some(&id(&r0)), &key());
    append(&mut ledger, &r1);
    let cut = state.reconnect(&key(), &anchor()).unwrap().unwrap();
    assert_eq!(state.leaf_count(), 2);
    assert_eq!(cut.leaf_count().unwrap(), 2);
}

#[test]
fn a_rotated_key_needs_the_archived_signer() {
    let (log, artifacts) = memory();
    let entry_log: Log = log.clone();
    let mut ledger = open(&entry_log, &artifacts).unwrap();
    let written = write_sequence(&mut ledger);
    let mut state = load_log(log.clone(), &[&key()]).unwrap();
    let cut = state.reconnect(&key(), &anchor()).unwrap().unwrap();

    let new_key = other_key();
    let err = load_log(log.clone(), &[&new_key]).err().unwrap();
    assert!(err.to_string().contains(&cut.key_id), "{err}");

    let mut rotated = load_log(log.clone(), &[&new_key, &key()]).unwrap();
    assert_eq!(rotated.last_checkpoint(), Some(&cut));
    let next = capsule(3, Some(&id(&written[4])), &new_key);
    ledger
        .append(&next, &statement(&next, &new_key, CONTENT_TYPE))
        .unwrap();
    let second = rotated.reconnect(&new_key, &anchor()).unwrap().unwrap();
    assert_eq!(
        second.key_id,
        hex::encode(new_key.verifying_key().to_bytes())
    );
    assert_eq!(second.prev_root, cut.root);
}

#[test]
fn witness_urls_are_refused() {
    let (log, _) = memory();
    let cfg = CheckpointCadenceConfig {
        witness_urls: vec!["http://127.0.0.1:1".into()],
        ..CheckpointCadenceConfig::default()
    };
    let err = CheckpointState::load_log(log, LOG_ID, cfg, &[&key()])
        .err()
        .unwrap();
    assert!(err.to_string().contains("witness"), "{err}");
}

/// A fresh memory log holding `source`'s entries, with `state` committed as
/// its CLL state.
fn copy_with_state(
    source: &cll::log::memory::MemoryStore,
    state: &cll::log::State,
) -> Arc<cll::log::memory::MemoryStore> {
    let copy = cll::log::memory::MemoryStore::new();
    for entry in source.scan_entries(0, MAX_SCAN_LIMIT).unwrap() {
        copy.append(&AppendInput {
            value: entry.value,
            appended_at: entry.appended_at,
        })
        .unwrap();
    }
    copy.commit_cll(0, None, state).unwrap();
    Arc::new(copy)
}

#[test]
fn a_cll_state_that_does_not_match_the_log_is_refused() {
    let (log, artifacts) = memory();
    let entry_log: Log = log.clone();
    let mut ledger = open(&entry_log, &artifacts).unwrap();
    write_sequence(&mut ledger);
    let mut state = load_log(log.clone(), &[&key()]).unwrap();
    state.reconnect(&key(), &anchor()).unwrap().unwrap();
    let good = log.load_cll().unwrap();
    assert!(load_log(copy_with_state(&log, &good), &[&key()]).is_ok());

    // The checkpoint's COSE no longer verifies.
    let mut bad_cose = good.clone();
    let checkpoint = bad_cose.checkpoint.as_mut().unwrap();
    let last = checkpoint.bytes.len() - 1;
    checkpoint.bytes[last] ^= 1;
    let err = load_log(copy_with_state(&log, &bad_cose), &[&key()])
        .err()
        .unwrap();
    assert!(err.to_string().contains("COSE"), "{err}");

    // An internal MMR node (position 2, the parent of leaves 0 and 1).
    let mut bad_node = good.clone();
    bad_node.nodes[2][0] ^= 1;
    let err = load_log(copy_with_state(&log, &bad_node), &[&key()])
        .err()
        .unwrap();
    assert!(err.to_string().contains("position 2"), "{err}");

    // A leaf: the log's entry at that index no longer matches.
    let mut bad_leaf = good.clone();
    bad_leaf.nodes[0][0] ^= 1;
    let err = load_log(copy_with_state(&log, &bad_leaf), &[&key()])
        .err()
        .unwrap();
    assert!(
        matches!(
            err,
            CheckpointStateError::NodeStoreDivergedFromLedger { leaf_index: 0, .. }
        ),
        "{err}"
    );

    // Stored peaks that are not the MMR's.
    let mut bad_peaks = good.clone();
    bad_peaks.checkpoint.as_mut().unwrap().peaks[0][0] ^= 1;
    let err = load_log(copy_with_state(&log, &bad_peaks), &[&key()])
        .err()
        .unwrap();
    assert!(err.to_string().contains("does not state"), "{err}");

    // The checkpoint of another log.
    let err = CheckpointState::load_log(log.clone(), "another-log", cfg(), &[&key()])
        .err()
        .unwrap();
    assert!(err.to_string().contains("does not state"), "{err}");

    // A state that indexes more leaves than the log holds.
    let short = cll::log::memory::MemoryStore::new();
    short.commit_cll(0, None, &good).unwrap();
    let err = load_log(Arc::new(short), &[&key()]).err().unwrap();
    assert!(
        matches!(err, CheckpointStateError::NodeStoreAheadOfLedger { .. }),
        "{err}"
    );
}

/// A padding sink that, like a writer racing the cut, appends one more record
/// right after the padding: the cut stays on the bucket boundary and the
/// record is folded past it.
struct RacingPadder {
    ledger: Arc<std::sync::Mutex<Ledger>>,
    racing: std::sync::Mutex<Option<Value>>,
}

impl capsule_emit::checkpoint::PaddingSink for RacingPadder {
    fn pad_to_bucket(&self, bucket: u64) -> Result<u64, String> {
        let mut ledger = self.ledger.lock().unwrap();
        let padded = ledger
            .pad_to_bucket(bucket, &key(), ISSUER)
            .map_err(|e| e.to_string())?;
        if let Some(record) = self.racing.lock().unwrap().take() {
            append(&mut ledger, &record);
        }
        Ok(padded)
    }
}

#[test]
fn a_checkpoint_shorter_than_the_persisted_state_reopens() {
    let (log, artifacts) = memory();
    let entry_log: Log = log.clone();
    let ledger = Arc::new(std::sync::Mutex::new(open(&entry_log, &artifacts).unwrap()));
    let r0 = capsule(0, None, &key());
    append(&mut ledger.lock().unwrap(), &r0);
    let r1 = capsule(1, Some(&id(&r0)), &key());

    let mut state = CheckpointState::load_log(
        log.clone(),
        LOG_ID,
        CheckpointCadenceConfig {
            pad_bucket: 4,
            ..cfg()
        },
        &[&key()],
    )
    .unwrap()
    .0;
    state.set_padding_sink(Box::new(RacingPadder {
        ledger: ledger.clone(),
        racing: std::sync::Mutex::new(Some(r1.clone())),
    }));
    let cut = state.reconnect(&key(), &anchor()).unwrap().unwrap();
    assert_eq!(cut.leaf_count().unwrap(), 4);
    let stored = log.load_cll().unwrap();
    assert_eq!(stored.indexed_seq, 5);
    assert_eq!(stored.checkpoint.as_ref().unwrap().indexed_seq, 4);

    let mut reopened = load_log(log.clone(), &[&key()]).unwrap();
    assert_eq!(reopened.last_checkpoint(), Some(&cut));
    assert!(reopened.existing_coverage(&id(&r0)).unwrap().is_some());
    assert!(reopened.existing_coverage(&id(&r1)).unwrap().is_none());
    let next = reopened.reconnect(&key(), &anchor()).unwrap().unwrap();
    assert_eq!(next.leaf_count().unwrap(), 5);
    assert_eq!(next.prev_size, cut.mmr_size);

    // The same five leaves on the flat layout give the same root.
    let dir = tempfile::tempdir().unwrap();
    let (mut flat, _) = Ledger::open(dir.path()).unwrap();
    let stored_ledger = ledger.lock().unwrap();
    for line in stored_ledger.lines() {
        let value = stored_ledger.read_line(line.offset).unwrap();
        let content_type = if line.padding {
            PADDING_CONTENT_TYPE
        } else {
            CONTENT_TYPE
        };
        let signed = statement(&value, &key(), content_type);
        if line.padding {
            flat.append_padding(&value, &signed).unwrap();
        } else {
            flat.append(&value, &signed).unwrap();
        }
    }
    drop(stored_ledger);
    let (mut flat_cp, _) = CheckpointState::load(dir.path(), LOG_ID, cfg()).unwrap();
    assert_eq!(
        flat_cp.reconnect(&key(), &anchor()).unwrap().unwrap().root,
        next.root
    );
}

/// A CLL backend whose `scan_entries` fails once, on its `fail_on`-th call.
struct FlakyScan {
    inner: Arc<cll::log::memory::MemoryStore>,
    scans: std::sync::atomic::AtomicUsize,
    fail_on: usize,
}

impl EntrySource for FlakyScan {
    fn scan_entries(&self, after_seq: u64, limit: usize) -> cll::log::Result<Vec<cll::log::Entry>> {
        let n = self.scans.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        if n == self.fail_on {
            return Err(cll::log::Error::new(
                cll::log::ErrorCode::Storage,
                "injected scan failure",
            ));
        }
        self.inner.scan_entries(after_seq, limit)
    }
}

impl CheckpointStateStore for FlakyScan {
    fn load_cll(&self) -> cll::log::Result<cll::log::State> {
        self.inner.load_cll()
    }

    fn commit_cll(
        &self,
        expected_size: u64,
        expected_checkpoint: Option<&[u8]>,
        next: &cll::log::State,
    ) -> cll::log::Result<()> {
        self.inner
            .commit_cll(expected_size, expected_checkpoint, next)
    }
}

#[test]
fn a_scan_that_fails_partway_resumes_without_folding_twice() {
    let inner = Arc::new(cll::log::memory::MemoryStore::new());
    // load_log's scan of the empty log is call 0; the first sync's pages are
    // calls 1 (entries 1..=1000) and 2, which fails.
    let flaky = Arc::new(FlakyScan {
        inner: inner.clone(),
        scans: std::sync::atomic::AtomicUsize::new(0),
        fail_on: 2,
    });
    let mut state = load_log(flaky, &[&key()]).unwrap();
    for i in 0..1500u32 {
        inner
            .append(&AppendInput {
                value: sha2_digest(&i.to_be_bytes()),
                appended_at: Timestamp::now(),
            })
            .unwrap();
    }
    let err = state.reconnect(&key(), &anchor()).err().unwrap();
    assert!(matches!(err, CheckpointStateError::Log(_)), "{err}");
    assert_eq!(state.leaf_count(), 1000);

    let cut = state.reconnect(&key(), &anchor()).unwrap().unwrap();
    assert_eq!(state.leaf_count(), 1500);
    assert_eq!(cut.leaf_count().unwrap(), 1500);
    // A reload checks every stored node against the entries.
    let reloaded = load_log(inner, &[&key()]).unwrap();
    assert_eq!(reloaded.last_checkpoint(), Some(&cut));
}

fn sha2_digest(data: &[u8]) -> Vec<u8> {
    use sha2::Digest;
    sha2::Sha256::digest(data).to_vec()
}

// -- an Evidence Bundle producer reads the SQLite log -----------------------------

/// The artifact store as the bundle producer's record source: capsules only.
struct Records(Artifacts);

impl RecordSource for Records {
    type Error = ArtifactError;

    fn get_record(&self, id: &str) -> Result<Option<StoredRecord>, ArtifactError> {
        match self.0.get(id) {
            Ok(record) => {
                let capsule: Value = serde_json::from_slice(&record.capsule).unwrap();
                Ok(
                    (capsule["record_type"] != "padding").then_some(StoredRecord {
                        capsule: record.capsule,
                        producer_envelope: record.producer_envelope,
                    }),
                )
            }
            Err(ArtifactError::NotFound) => Ok(None),
            Err(other) => Err(other),
        }
    }
}

#[test]
fn proof_source_from_cll_reads_the_sqlite_backed_log() {
    let dir = tempfile::tempdir().unwrap();
    let (log, artifacts) = sqlite(dir.path());
    let entry_log: Log = log.clone();
    let mut ledger = open(&entry_log, &artifacts).unwrap();
    let r0 = capsule(0, None, &key());
    let r1 = capsule(1, Some(&id(&r0)), &key());
    let r2 = capsule(2, Some(&id(&r1)), &key());
    for r in [&r0, &r1, &r2] {
        append(&mut ledger, r);
    }
    let mut state = load_log(log.clone(), &[&key()]).unwrap();
    state.reconnect(&key(), &anchor()).unwrap().unwrap();

    let source = proof_source_from_cll(log.as_ref(), Records(artifacts.clone())).unwrap();
    let bundle =
        build_evidence_bundle(&source, &BuildOptions::new(id(&r2), Interval::From(1))).unwrap();
    // build_evidence_bundle checks its result with verify_bundle_strict and
    // refuses one that reads INVALID; a whole-log range over records reads
    // complete.
    assert_eq!(bundle["bundle_kind"], "evidence-bundle/v2");
    assert_eq!(bundle["root"], id(&r2));
    assert_eq!(bundle["records"].as_array().unwrap().len(), 3);
}
