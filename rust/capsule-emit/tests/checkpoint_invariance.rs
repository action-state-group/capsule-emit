//! Checkpoint conformance: the checkpoint this crate's
//! `checkpoint::CheckpointState` produces over a `capsules.jsonl` must carry
//! the SAME MMR root an independent Python recomputation gets over the
//! identical leaves, and the COSE-wire statement must verify under the
//! Python reference offline verifier (`capsule_emit.checkpoint.
//! verify_checkpoint_cose_offline`) -- cross-language verification, the same
//! discipline `cross_language_conformance.rs`/`chain_ledger_conformance.rs`
//! already hold for Layer 0. This test is checkpoint-layer only: Layer 0
//! (capsule_id / COSE_Sign1 capsule signature) byte-identity is already
//! covered by those two tests, not re-derived here.
//!
//! `#[ignore]`d and gated on env vars (same shape as this crate's other
//! cross-language tests): it needs the Python `capsule_emit` and
//! `checkpointed-local-log` packages. Run it with:
//!
//!   AAC_PYTHON=python3 \
//!   AAC_CHECKPOINT_VERIFY_SCRIPT=/path/to/tests/scripts/checkpoint_invariance_check.py \
//!     cargo test --test checkpoint_invariance -- --ignored --nocapture

mod common;

use capsule_emit::anchor::AnchorClient;
use capsule_emit::capsule::{seal, CapsuleInput};
use capsule_emit::checkpoint::{CheckpointCadenceConfig, CheckpointState};
use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
use capsule_emit::keys::KeyPair;
use capsule_emit::ledger::Ledger;
use ed25519_dalek::SigningKey;
use std::path::PathBuf;
use std::process::Command;

struct Env {
    python: String,
    verify_script: PathBuf,
}

fn env() -> Option<Env> {
    let python = std::env::var("AAC_PYTHON").ok()?;
    let verify_script = std::env::var("AAC_CHECKPOINT_VERIFY_SCRIPT").ok()?.into();
    Some(Env {
        python,
        verify_script,
    })
}

/// One minimal, valid capsule input for leaf `n` -- content doesn't matter
/// for the checkpoint layer, only that `seal()` produces a real, distinct
/// `capsule_id` each time.
fn capsule_input(n: usize, parent: Option<&str>) -> CapsuleInput {
    common::capsule_input("checkpoint-invariance", n, parent.map(common::follows))
}

fn build_ledger(dir: &std::path::Path, keys: &SigningKey, n: usize) -> Ledger {
    let (mut ledger, _report) = Ledger::open(dir).expect("open ledger");
    let mut parent: Option<String> = None;
    for i in 0..n {
        let input = capsule_input(i, parent.as_deref());
        let capsule = seal(&input).expect("seal capsule");
        let capsule_id = capsule["capsule_id"].as_str().unwrap().to_string();
        let payload = capsule_emit::capsule::payload_bytes(&capsule);
        let statement = build_signed_statement(
            &SignedStatementInput {
                payload: &payload,
                issuer: "checkpoint-invariance-node",
                subject: &capsule_id,
                content_type:
                    "application/vnd.agent-action-capsule+json; profile=draft-mih-scitt-agent-action-capsule-02",
            },
            keys,
        );
        ledger
            .append(&capsule, &statement)
            .expect("append to ledger");
        parent = Some(capsule_id);
    }
    ledger
}

fn run_python_check(
    env: &Env,
    ledger_dir: &std::path::Path,
    root_hex: &str,
    mmr_size: u64,
    cose_hex_path: &str,
) -> (bool, String) {
    let output = Command::new(&env.python)
        .arg(&env.verify_script)
        .arg(ledger_dir)
        .arg(root_hex)
        .arg(mmr_size.to_string())
        .arg(cose_hex_path)
        .output()
        .expect("run python checkpoint invariance checker");
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    (output.status.success(), stdout)
}

#[test]
#[ignore]
fn rust_checkpoint_root_and_cose_match_independent_python_recomputation() {
    let Some(env) = env() else {
        eprintln!("AAC_PYTHON / AAC_CHECKPOINT_VERIFY_SCRIPT not set; skipping (see module docs)");
        return;
    };

    let dir = tempfile::tempdir().expect("tempdir");
    let keys = KeyPair::generate();
    let ledger = build_ledger(dir.path(), &keys.signing_key, 5);
    drop(ledger); // release the append handle before CheckpointState opens the same dir

    let (mut state, _report) = CheckpointState::load(
        dir.path(),
        "checkpoint-invariance-log",
        CheckpointCadenceConfig::default(),
    )
    .expect("load checkpoint state");
    let anchor = AnchorClient::new("http://127.0.0.1:1"); // no witness_urls configured -- never dialled
    let cp = state
        .reconnect(&keys.signing_key, &anchor)
        .expect("reconnect")
        .expect("a checkpoint must be produced over 5 sealed capsules");

    assert_eq!(cp.log_id, "checkpoint-invariance-log");
    assert!(
        cp.verify_signature_offline(),
        "Rust's own offline check must accept its own checkpoint"
    );

    let cose_hex_path = dir.path().join("checkpoint.cose.hex");
    // `state`'s own last-built COSE bytes aren't exposed publicly (only
    // persisted to checkpoints.jsonl) -- read them back off disk the same
    // way any stranger consuming this ledger would.
    let lines = cll::store::read_checkpoints(dir.path().join("checkpoints.jsonl"))
        .expect("read checkpoints.jsonl");
    let last = lines.last().expect("at least one checkpoint line");
    let cose_arg = match &last.checkpoint_cose_hex {
        Some(hex_str) => {
            std::fs::write(&cose_hex_path, hex_str).unwrap();
            cose_hex_path.display().to_string()
        }
        None => "-".to_string(),
    };

    let (ok, stdout) = run_python_check(&env, dir.path(), &cp.root, cp.mmr_size, &cose_arg);
    eprintln!("python checkpoint invariance check: {stdout}");
    assert!(ok, "python checkpoint invariance check failed: {stdout}");

    let result: serde_json::Value =
        serde_json::from_str(stdout.trim()).expect("parse checker JSON");
    assert_eq!(result["root_match"], true, "{stdout}");
    assert_eq!(result["computed_root"], cp.root, "{stdout}");
    if cose_arg != "-" {
        assert_eq!(result["cose_ok"], true, "{stdout}");
    }
}
