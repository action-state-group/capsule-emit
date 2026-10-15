//! capsule-emit's legacy ledger as an artifact store and a CLL entry log
//! (capsule-emit#308 item 5): the declared conformance subsets and the legacy
//! cases.

use std::path::{Path, PathBuf};

use capsule_emit::anchor::AnchorClient;
use capsule_emit::capsule::{
    attach_producer_envelope, payload_bytes, seal, CapsuleInput, ChainLink,
};
use capsule_emit::checkpoint::{CheckpointCadenceConfig, CheckpointState};
use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
use capsule_emit::ledger::Ledger;
use capsule_emit_artifact::conformance::{self, Harness, Limits, Status};
use capsule_emit_artifact::legacy::{LegacyLedger, CAPABILITIES, STATEMENT};
use capsule_emit_artifact::{Artifact, ArtifactError, Record, RetentionState, Store};
use cll::log::{AppendInput, AppendOutcome, EntrySource, EntryStore, ErrorCode, Timestamp};
use ed25519_dalek::SigningKey;
use serde_json::{json, Map, Value};

const ISSUER: &str = "legacy-test";
const CONTENT_TYPE: &str = "application/vnd.agent-action-capsule+json";

fn key() -> SigningKey {
    SigningKey::from_bytes(&[7u8; 32])
}

fn trusted() -> Vec<Vec<u8>> {
    vec![key().verifying_key().to_bytes().to_vec()]
}

fn statement(line: &Value, signer: &SigningKey) -> Vec<u8> {
    build_signed_statement(
        &SignedStatementInput {
            payload: &payload_bytes(line),
            issuer: ISSUER,
            subject: line["capsule_id"].as_str().unwrap(),
            content_type: CONTENT_TYPE,
        },
        signer,
    )
}

fn input(n: usize, chain: Option<ChainLink>) -> CapsuleInput {
    CapsuleInput {
        action_id: format!("example/legacy/{n:08}"),
        action_type: "decide".to_string(),
        operator: "example-operator".to_string(),
        developer: "capsule-emit/legacy-test".to_string(),
        timestamp: format!("2026-09-28T00:{n:02}:00Z"),
        domain: Some("action".to_string()),
        provenance: Some("collector".to_string()),
        model_id: "example-model".to_string(),
        provider: "example-provider".to_string(),
        agent_input_digest: format!("{n:064x}"),
        agent_output_digest: Some(format!("{:064x}", n + 1)),
        tool_calls_digest: None,
        reasoning_digest: None,
        host_binding: None,
        runtime: json!({"name": "legacy-runtime"}),
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

/// A capsule line chained to `parent`, with the producer envelope unless
/// `unsigned` (a capsule sealed before producer envelopes).
fn line(n: usize, parent: Option<&str>, unsigned: bool) -> Value {
    let chain = parent.map(|p| ChainLink {
        parent_capsule_id: p.to_string(),
        relation: "follows".to_string(),
    });
    let mut capsule = seal(&input(n, chain)).unwrap();
    if !unsigned {
        attach_producer_envelope(&mut capsule, &key()).unwrap();
    }
    capsule
}

fn id(line: &Value) -> &str {
    line["capsule_id"].as_str().unwrap()
}

/// A historical ledger written by `Ledger` directly: two signed capsules, a
/// pre-envelope capsule, padding to 8 lines and one more capsule.
fn historical(dir: &Path) -> Vec<Value> {
    let (mut ledger, _) = Ledger::open(dir).unwrap();
    let mut lines = Vec::new();
    for n in 0..3 {
        let parent = lines.last().map(|l: &Value| id(l).to_string());
        let l = line(n, parent.as_deref(), n == 2);
        ledger.append(&l, &statement(&l, &key())).unwrap();
        lines.push(l);
    }
    ledger.pad_to_bucket(8, &key(), ISSUER).unwrap();
    let l = line(3, Some(id(&lines[2])), false);
    ledger.append(&l, &statement(&l, &key())).unwrap();
    lines.push(l);
    lines
}

fn file_lines(dir: &Path) -> Vec<Value> {
    std::fs::read_to_string(dir.join("capsules.jsonl"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn store(dir: &Path) -> LegacyLedger {
    let store = LegacyLedger::new(dir, "ledger", trusted()).unwrap();
    store.init().unwrap();
    store
}

fn without_envelope(line: &Value) -> Vec<u8> {
    let mut object = line.as_object().unwrap().clone();
    object.remove("signature");
    object.remove("key_id");
    serde_json::to_vec(&Value::Object(object)).unwrap()
}

// -- declared conformance subsets ---------------------------------------------

#[test]
fn cll_conformance_subset() {
    // One directory per case, all removed with `root` when the test ends.
    let root = tempfile::tempdir().unwrap();
    let mut next = 0;
    let report = cll::log::conformance::run(
        cll::log::conformance::Config::new(|| {
            next += 1;
            let dir: PathBuf = root.path().join(next.to_string());
            LegacyLedger::new(dir, "ledger", trusted()).unwrap()
        })
        .with_reopen(|current: LegacyLedger| {
            current.close();
            LegacyLedger::new(current.dir(), "ledger", trusted()).unwrap()
        })
        .without_digest_append(),
    );
    report.assert_ok();
    let ran: Vec<&str> = report
        .cases
        .iter()
        .filter(|c| c.status == cll::log::conformance::Status::Passed)
        .map(|c| c.name.as_str())
        .collect();
    for name in [
        "entries/empty-log",
        "entries/invalid-values",
        "entries/digest-append-unsupported",
    ] {
        assert!(ran.contains(&name), "{name} did not run:\n{report}");
    }
}

/// The vector key whose public key signed `record`'s envelope.
fn vector_signer(record: &Record) -> SigningKey {
    let envelope =
        agent_action_capsule::envelope::verify(&record.capsule_id, &record.producer_envelope);
    (1u8..=3)
        .map(|seed| SigningKey::from_bytes(&[seed; 32]))
        .find(|k| k.verifying_key().to_bytes()[..] == envelope.public_key[..])
        .unwrap_or_else(|| SigningKey::from_bytes(&[1u8; 32]))
}

#[test]
fn artifact_conformance_subset() {
    let harness = Harness {
        new_backing: Box::new(|| tempfile::tempdir().unwrap()),
        open: Box::new(|dir: &tempfile::TempDir, ns: &str, keys: &[Vec<u8>]| {
            Ok(Box::new(LegacyLedger::new(dir.path(), ns, keys)?) as Box<dyn Store>)
        }),
        durable: true,
        shared_backing: false,
    };
    // The vectors carry no signed statement: add one signed by the record's
    // own producer key (the vector keys are seeds 1, 2, 3), as seal() does.
    let limits = Limits {
        originals: false,
        purge: false,
        before_put: Some(Box::new(|mut record: Record| {
            let mut line: Value = serde_json::from_slice(&record.capsule).unwrap_or(json!({}));
            let signer = vector_signer(&record);
            if let Some(object) = line.as_object_mut() {
                object.insert(
                    "signature".into(),
                    json!(hex::encode(&record.producer_envelope)),
                );
                object.insert(
                    "key_id".into(),
                    json!(hex::encode(signer.verifying_key().to_bytes())),
                );
                object.insert("capsule_id".into(), json!(record.capsule_id));
            }
            let statement = statement(&line, &signer);
            record.artifacts.push(Artifact {
                name: STATEMENT.into(),
                binding: String::new(),
                content: Some(statement),
                state: RetentionState::Present,
                content_sha256: String::new(),
            });
            record
        })),
        after_get: Some(Box::new(|mut record: Record| {
            let before = record.artifacts.len();
            record.artifacts.retain(|a| a.name != STATEMENT);
            assert_eq!(
                before,
                record.artifacts.len() + 1,
                "get must report the statement"
            );
            record
        })),
    };
    let outcomes = conformance::run_limited(&harness, &limits);
    let failures: Vec<_> = outcomes
        .iter()
        .filter(|o| o.status == Status::Fail)
        .collect();
    assert!(failures.is_empty(), "{failures:#?}");
    let passed: Vec<&str> = outcomes
        .iter()
        .filter(|o| o.status == Status::Pass)
        .map(|o| o.name.as_str())
        .collect();
    for name in [
        "bare_record",
        "bare_rejections",
        "bare_untrusted_on_read",
        "malformed_ids",
    ] {
        assert!(passed.contains(&name), "{name} did not run: {outcomes:#?}");
    }
    assert!(!passed.contains(&"purge_lifecycle"));
}

// -- historical ledger ---------------------------------------------------------

#[test]
fn every_line_is_an_entry_at_its_existing_position() {
    let dir = tempfile::tempdir().unwrap();
    historical(dir.path());
    let raw = file_lines(dir.path());
    assert_eq!(raw.len(), 9);
    let store = store(dir.path());
    let entries = store.scan_entries(0, 100).unwrap();
    let values: Vec<String> = entries.iter().map(|e| hex::encode(&e.value)).collect();
    let ids: Vec<&str> = raw.iter().map(id).collect();
    assert_eq!(values, ids);
    assert_eq!(
        entries.iter().map(|e| e.seq).collect::<Vec<_>>(),
        (1..=9).collect::<Vec<u64>>()
    );
    // A padding line has no timestamp and inherits the previous entry's.
    assert_eq!(entries[3].appended_at, entries[2].appended_at);
    assert_eq!(
        entries[0].appended_at,
        Timestamp::parse("2026-09-28T00:00:00.000Z").unwrap()
    );
    assert_eq!(store.scan_entries(7, 5).unwrap(), entries[7..].to_vec());
    let first = store.get_entry(&entries[0].value).unwrap();
    assert_eq!(first.seq, 1);
    let again = store
        .append(&AppendInput {
            value: entries[0].value.clone(),
            appended_at: Timestamp::now(),
        })
        .unwrap();
    assert_eq!(
        (again.outcome, again.entry.seq),
        (AppendOutcome::Idempotent, 1)
    );
    let err = store
        .append(&AppendInput {
            value: vec![9; 32],
            appended_at: Timestamp::now(),
        })
        .unwrap_err();
    assert_eq!(err.code(), ErrorCode::Unsupported);
}

#[test]
fn get_returns_the_capsule_envelope_and_statement() {
    let dir = tempfile::tempdir().unwrap();
    let lines = historical(dir.path());
    let store = store(dir.path());
    for l in [&lines[0], &lines[1], &lines[3]] {
        let record = store.get(id(l)).unwrap();
        assert_eq!(record.capsule, without_envelope(l));
        assert_eq!(
            record.producer_envelope,
            hex::decode(l["signature"].as_str().unwrap()).unwrap()
        );
        let [statement] = &record.artifacts[..] else {
            panic!("one attachment")
        };
        assert_eq!(statement.name, STATEMENT);
        assert!(statement.binding.is_empty());
        assert_eq!(
            statement.content.as_deref(),
            Some(
                &std::fs::read(dir.path().join(format!("signed-statements/{}.cose", id(l))))
                    .unwrap()[..]
            )
        );
    }
    let unsigned = store.get(id(&lines[2])).unwrap();
    assert!(unsigned.producer_envelope.is_empty());
    let padding = file_lines(dir.path())[3].clone();
    assert!(matches!(
        store.get(id(&padding)),
        Err(ArtifactError::NotFound)
    ));
    let other = LegacyLedger::new(dir.path(), "ledger", [[1u8; 32]]).unwrap();
    assert!(matches!(
        other.get(id(&lines[0])),
        Err(ArtifactError::UntrustedSigner)
    ));
}

#[test]
fn put_of_historical_records_reproduces_the_ledger() {
    let source_dir = tempfile::tempdir().unwrap();
    let lines = historical(source_dir.path());
    let source = store(source_dir.path());
    let target_dir = tempfile::tempdir().unwrap();
    let target = store(target_dir.path());
    for l in &lines[..3] {
        let record = source.get(id(l)).unwrap();
        target.put(&record).unwrap();
        target.put(&record).unwrap(); // idempotent
    }
    let raw = |dir: &Path| std::fs::read_to_string(dir.join("capsules.jsonl")).unwrap();
    let source_lines: Vec<String> = raw(source_dir.path())
        .lines()
        .take(3)
        .map(String::from)
        .collect();
    let target_lines: Vec<String> = raw(target_dir.path()).lines().map(String::from).collect();
    assert_eq!(source_lines, target_lines);
    let (reopened, _) = Ledger::open(target_dir.path()).unwrap();
    assert_eq!(reopened.entries(), 3);
}

#[test]
fn capability_limits_are_unsupported() {
    let dir = tempfile::tempdir().unwrap();
    let lines = historical(dir.path());
    let store = store(dir.path());
    let mut record = store.get(id(&lines[0])).unwrap();
    assert!(matches!(
        store.purge(id(&lines[0])),
        Err(ArtifactError::Unsupported(_))
    ));
    assert!(matches!(
        store.purge("NOT-AN-ID"),
        Err(ArtifactError::Invalid(_))
    ));
    record.artifacts.push(Artifact {
        name: "payload".into(),
        binding: String::new(),
        content: None,
        state: RetentionState::NeverRetained,
        content_sha256: String::new(),
    });
    assert!(matches!(
        store.put(&record),
        Err(ArtifactError::Unsupported(_))
    ));
    record.artifacts.truncate(1);
    record.artifacts.push(record.artifacts[0].clone());
    assert!(
        matches!(store.put(&record), Err(ArtifactError::Invalid(ref m)) if m.contains("duplicate"))
    );
    record.artifacts.clear();
    assert!(matches!(store.put(&record), Err(ArtifactError::Invalid(_))));
    assert!(CAPABILITIES.iter().all(|(_, offered)| !offered));
}

#[test]
fn a_different_record_under_a_held_id_conflicts_and_chain_breaks_are_invalid() {
    let dir = tempfile::tempdir().unwrap();
    let lines = historical(dir.path());
    let store = store(dir.path());
    let mut record = store.get(id(&lines[0])).unwrap();
    let other = SigningKey::from_bytes(&[8u8; 32]);
    record.artifacts[0].content = Some(statement(&lines[0], &other));
    record.artifacts[0].content_sha256.clear();
    // A statement by another key no longer verifies against key_id.
    assert!(matches!(store.put(&record), Err(ArtifactError::Invalid(_))));

    let unchained = line(9, None, false);
    let mut fresh = Record {
        capsule_id: id(&unchained).to_string(),
        capsule: without_envelope(&unchained),
        producer_envelope: hex::decode(unchained["signature"].as_str().unwrap()).unwrap(),
        artifacts: Vec::new(),
    };
    fresh.artifacts.push(Artifact {
        name: STATEMENT.into(),
        binding: String::new(),
        content: Some(statement(&unchained, &key())),
        state: RetentionState::Present,
        content_sha256: String::new(),
    });
    let err = store.put(&fresh).unwrap_err();
    assert!(
        matches!(err, ArtifactError::Invalid(ref m) if m.contains("head")),
        "{err}"
    );
}

#[test]
fn new_padding_is_covered_by_the_next_checkpoint() {
    let dir = tempfile::tempdir().unwrap();
    historical(dir.path());
    let store = store(dir.path());
    let real = line(4, Some(id(file_lines(dir.path()).last().unwrap())), false);
    let record = Record {
        capsule_id: id(&real).to_string(),
        capsule: without_envelope(&real),
        producer_envelope: hex::decode(real["signature"].as_str().unwrap()).unwrap(),
        artifacts: vec![Artifact {
            name: STATEMENT.into(),
            binding: String::new(),
            content: Some(statement(&real, &key())),
            state: RetentionState::Present,
            content_sha256: String::new(),
        }],
    };
    store.put(&record).unwrap();
    assert_eq!(store.pad_to_bucket(16, &key(), ISSUER).unwrap(), 16);
    let entries = store.scan_entries(0, 100).unwrap();
    assert_eq!(entries.len(), 16);
    let padding = hex::encode(&entries[15].value);
    store.close();

    let (mut state, _) = CheckpointState::load(
        dir.path(),
        "legacy-test-log",
        CheckpointCadenceConfig::default(),
    )
    .unwrap();
    let coverage = state
        .checkpoint_covering(&padding, &key(), &AnchorClient::new("http://127.0.0.1:1"))
        .unwrap();
    assert_eq!(coverage.leaf_index, 15);
    assert_eq!(coverage.checkpoint.mmr_size, cll::mmr::node_count(16));
}

#[test]
fn closed_and_uninitialized_stores() {
    let dir = tempfile::tempdir().unwrap();
    let store = LegacyLedger::new(dir.path().join("none"), "ledger", trusted()).unwrap();
    assert!(
        !dir.path().join("none").exists(),
        "the constructor writes nothing"
    );
    assert_eq!(store.scan_entries(0, 10).unwrap(), vec![]);
    assert!(matches!(
        store.get(&"a".repeat(64)),
        Err(ArtifactError::NotFound)
    ));
    store.init().unwrap();
    store.init().unwrap();
    assert!(dir.path().join("none/capsules.jsonl").exists());
    store.close();
    assert_eq!(
        store.scan_entries(0, 1).unwrap_err().code(),
        ErrorCode::Closed
    );
    assert!(store.get(&"a".repeat(64)).is_err());
    assert!(LegacyLedger::new(dir.path(), "bad/ns", trusted()).is_err());
}

/// The Rust CLI, `verify_capsule <capsule.json> <statement.cose> <pubkey.pem>
/// [ledger_dir]`, is a wrapper over `verify_offline` with the ledger's known
/// ids: every record written through the backend verifies that way.
#[test]
fn records_written_through_the_backend_pass_the_offline_verifier() {
    let source_dir = tempfile::tempdir().unwrap();
    let lines = historical(source_dir.path());
    let source = store(source_dir.path());
    let dir = tempfile::tempdir().unwrap();
    let target = store(dir.path());
    for l in &lines[..3] {
        target.put(&source.get(id(l)).unwrap()).unwrap();
    }
    target.close();
    let (ledger, _) = Ledger::open(dir.path()).unwrap();
    let known = ledger.known_capsule_ids();
    for l in &lines[..3] {
        let entry = ledger.lookup(id(l)).unwrap().unwrap();
        let report = capsule_emit::verify::verify_offline(
            &entry.capsule,
            &entry.signed_statement,
            &key().verifying_key(),
            Some(&known),
        );
        assert!(report.ok(), "{:?}", report.findings);
    }
}

#[test]
fn rejected_padding_leaves_the_ledger_reopenable() {
    let dir = tempfile::tempdir().unwrap();
    historical(dir.path());
    let store = store(dir.path());
    let mut padding = capsule_emit::padding::build_padding_record().unwrap();
    attach_producer_envelope(&mut padding, &key()).unwrap();
    let good = statement(&padding, &key());
    assert!(matches!(
        store.append_padding(&padding, b""),
        Err(ArtifactError::Invalid(_))
    ));
    let mut wrong_id = padding.clone();
    wrong_id["capsule_id"] = json!("ab".repeat(32));
    assert!(matches!(
        store.append_padding(&wrong_id, &good),
        Err(ArtifactError::Invalid(_))
    ));
    store.append_padding(&padding, &good).unwrap();
    assert!(matches!(
        store.append_padding(&padding, &good),
        Err(ArtifactError::Conflict)
    ));
    store.close();
    let (reopened, _) = Ledger::open(dir.path()).unwrap();
    assert_eq!(reopened.entries(), 10);
}

#[test]
fn arguments_are_checked_before_durable_state() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("capsules.jsonl"), "not json\n").unwrap();
    let store = LegacyLedger::new(dir.path(), "ledger", trusted()).unwrap();
    assert_eq!(store.get_entry(&[]).unwrap_err().code(), ErrorCode::Invalid);
    assert_eq!(
        store.scan_entries(0, 0).unwrap_err().code(),
        ErrorCode::Invalid
    );
    let input = AppendInput {
        value: vec![1; 31],
        appended_at: Timestamp::now(),
    };
    assert_eq!(store.append(&input).unwrap_err().code(), ErrorCode::Invalid);
    assert_eq!(
        store.scan_entries(0, 10).unwrap_err().code(),
        ErrorCode::Corrupt
    );
    // Nothing was truncated or recovered by the invalid calls.
    assert_eq!(
        std::fs::read_to_string(dir.path().join("capsules.jsonl")).unwrap(),
        "not json\n"
    );
}

#[test]
fn a_concurrent_close_reports_closed() {
    let dir = tempfile::tempdir().unwrap();
    historical(dir.path());
    let store = std::sync::Arc::new(store(dir.path()));
    let readers: Vec<_> = (0..4)
        .map(|_| {
            let store = store.clone();
            std::thread::spawn(move || {
                for _ in 0..200 {
                    if let Err(e) = store.scan_entries(0, 10) {
                        assert_eq!(e.code(), ErrorCode::Closed, "{e}");
                    }
                }
            })
        })
        .collect();
    store.close();
    for reader in readers {
        reader.join().unwrap();
    }
    // Closed comes before argument validation on every CLL operation.
    let input = AppendInput {
        value: vec![1; 31],
        appended_at: Timestamp::now(),
    };
    assert_eq!(store.append(&input).unwrap_err().code(), ErrorCode::Closed);
    assert_eq!(store.get_entry(&[]).unwrap_err().code(), ErrorCode::Closed);
    assert_eq!(
        store.scan_entries(0, 0).unwrap_err().code(),
        ErrorCode::Closed
    );
}
