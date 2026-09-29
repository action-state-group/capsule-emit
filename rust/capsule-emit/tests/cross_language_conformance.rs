//! Cross-language conformance: a capsule + COSE_Sign1 statement produced by this
//! crate must (a) verify GREEN against the Python reference
//! (`agent_action_capsule` + `scitt_cose`) and (b) verify GREEN against the
//! independent Go COSE verifier (`scitt-cose-go-verify`, using
//! `veraison/go-cose` — a clean-room second opinion so a `coset`-specific bug
//! can't be masked by testing against itself); and a mutated statement must be
//! REJECTED by both.
//!
//! `#[ignore]`d and gated on env vars: it needs the Python reference
//! packages and a checkout of the Go verifier. Run it with:
//!
//!   AAC_PYTHON=python3 \
//!   AAC_VERIFY_SCRIPT=/path/to/tests/scripts/verify_rust_capsule.py \
//!   AAC_GO_VERIFY_DIR=/path/to/scitt-cose/scitt-cose-go-verify \
//!   AAC_PRODUCE_SCRIPT=/path/to/tests/scripts/produce_python_envelope.py \
//!     cargo test --test cross_language_conformance -- --ignored --nocapture
//!
//! (`AAC_PRODUCE_SCRIPT` is only needed by the reverse-direction test
//! `rust_verifies_a_python_produced_noncanonical_envelope`, which is gated on it
//! independently — the other tests run without it.)

mod common;

use capsule_emit::capsule::{seal, CapsuleInput};
use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
use capsule_emit::keys::KeyPair;
use coset::TaggedCborSerializable;
use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;

struct Env {
    python: String,
    verify_script: PathBuf,
    go_verify_dir: PathBuf,
}

fn env() -> Option<Env> {
    let python = std::env::var("AAC_PYTHON").ok()?;
    let verify_script = std::env::var("AAC_VERIFY_SCRIPT").ok()?.into();
    let go_verify_dir = std::env::var("AAC_GO_VERIFY_DIR").ok()?.into();
    Some(Env {
        python,
        verify_script,
        go_verify_dir,
    })
}

/// One representative capsule: a confirmed inference_completion effect with
/// model identity and compute I/O digests. Standalone (no chain);
/// `chain_ledger_conformance.rs` covers chains.
fn sample_capsule_input() -> CapsuleInput {
    common::capsule_input("cross-language", 0, None)
}

struct Produced {
    dir: tempfile::TempDir,
    capsule_path: PathBuf,
    statement_path: PathBuf,
    pubkey_path: PathBuf,
    capsule_id: String,
}

fn produce() -> Produced {
    let input = sample_capsule_input();
    let mut capsule = seal(&input).expect("seal capsule");
    let capsule_id = capsule["capsule_id"].as_str().unwrap().to_string();

    let keys = KeyPair::generate();
    // verifier parity for the inline signature envelope: attach the
    // inline producer-signature envelope BEFORE serializing, so the parity
    // oracle checks the SIGNED capsule (signature/key_id present), not just the
    // unsigned body -- and the detached `.cose` payload IS the pushed-half
    // bytes (one shape everywhere). Attaching does not move capsule_id.
    //
    // Renamed from "full-output parity" -- byte-identity is unachievable and not
    // the goal: RFC 9052 §9 requires canonical protected-header key order
    // (Rust/coset conforms, the Python reference emits insertion order, a §9
    // violation). The criterion is verifier-parity -- every conforming verifier
    // accepts the envelope because the signature is over the protected bstr as
    // received -- plus identical capsule_id (over the JCS body).
    capsule_emit::capsule::attach_producer_envelope(&mut capsule, &keys.signing_key)
        .expect("attach inline producer envelope");
    assert_eq!(
        capsule["capsule_id"].as_str().unwrap(),
        capsule_id,
        "attaching the envelope must not change capsule_id"
    );
    let payload = capsule_emit::capsule::payload_bytes(&capsule);

    let statement = build_signed_statement(
        &SignedStatementInput {
            payload: &payload,
            issuer: "capsule-emit-conformance",
            subject: &capsule_id,
            content_type:
                "application/vnd.agent-action-capsule+json; profile=draft-mih-scitt-agent-action-capsule-02",
        },
        &keys.signing_key,
    );

    let dir = tempfile::tempdir().expect("tempdir");
    let capsule_path = dir.path().join("capsule.json");
    let statement_path = dir.path().join("statement.cose");
    let pubkey_path = dir.path().join("node-key.pub.pem");
    std::fs::write(&capsule_path, &payload).unwrap();
    std::fs::write(&statement_path, &statement).unwrap();
    std::fs::write(&pubkey_path, keys.public_key_pem()).unwrap();

    Produced {
        dir,
        capsule_path,
        statement_path,
        pubkey_path,
        capsule_id,
    }
}

fn run_python_verifier(env: &Env, produced: &Produced) -> (bool, String) {
    let output = Command::new(&env.python)
        .arg(&env.verify_script)
        .arg(&produced.capsule_path)
        .arg(&produced.statement_path)
        .arg(&produced.pubkey_path)
        .output()
        .expect("run python verifier");
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    (output.status.success(), stdout)
}

fn run_go_verifier(env: &Env, produced: &Produced) -> (bool, String) {
    let output = Command::new("go")
        .arg("run")
        .arg(".")
        .arg("-statement")
        .arg(&produced.statement_path)
        .arg("-pubkey")
        .arg(&produced.pubkey_path)
        .arg("-alg")
        .arg("EdDSA")
        .current_dir(&env.go_verify_dir)
        .output()
        .expect("run go verifier");
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    (output.status.success(), format!("{stdout}\n{stderr}"))
}

#[test]
#[ignore]
fn rust_capsule_verifies_green_python_and_go_and_rejects_mutation() {
    let Some(env) = env() else {
        eprintln!("AAC_PYTHON / AAC_VERIFY_SCRIPT / AAC_GO_VERIFY_DIR not set; skipping (see module docs)");
        return;
    };

    let produced = produce();
    assert_eq!(
        produced.capsule_id.len(),
        64,
        "capsule_id must be 64 hex chars"
    );

    // --- (a) Python reference: COSE signature + Class-1 capsule_id/JCS check.
    let (py_ok, py_out) = run_python_verifier(&env, &produced);
    eprintln!("python verifier: {py_out}");
    assert!(
        py_ok,
        "Python reference rejected a valid Rust-produced capsule: {py_out}"
    );
    let py_result: Value = serde_json::from_str(py_out.trim()).expect("parse python verifier JSON");
    assert_eq!(py_result["cose_ok"], true, "{py_out}");
    assert_eq!(py_result["capsule_ok"], true, "{py_out}");
    assert_eq!(py_result["capsule_id"], produced.capsule_id, "{py_out}");
    // verifier parity for the inline signature envelope: the INLINE
    // producer-signature envelope on the capsule must grade AUTHORED at the
    // Python reference (its tristate signature check) -- not just the body.
    // This is the assertion that would have caught the silent regression: a
    // body-only parity check passed while the envelope was absent.
    assert_eq!(
        py_result["envelope_verdict"], "authored",
        "inline producer envelope must verify AUTHORED in the Python reference: {py_out}"
    );
    assert_eq!(py_result["envelope_ok"], true, "{py_out}");

    // --- (b) Independent Go COSE verifier (veraison/go-cose, clean-room vs `coset`).
    let (go_ok, go_out) = run_go_verifier(&env, &produced);
    eprintln!("go verifier: {go_out}");
    assert!(
        go_ok,
        "Go verifier rejected a valid Rust-produced statement: {go_out}"
    );

    // --- (c) Rust's own independent re-verification (belt + suspenders: proves
    //     the signer and verifier in this crate aren't self-consistently wrong
    //     in the same way).
    let keys_pem = std::fs::read_to_string(&produced.pubkey_path).unwrap();
    let vk = capsule_emit::keys::load_verifying_key_pem(&keys_pem).unwrap();
    let statement_bytes = std::fs::read(&produced.statement_path).unwrap();
    let verified = capsule_emit::cose::verify_signed_statement(&statement_bytes, &vk)
        .expect("Rust's own verifier should accept its own output");
    assert_eq!(
        verified.subject.as_deref(),
        Some(produced.capsule_id.as_str())
    );

    // --- (d) Mutation MUST be rejected by every verifier: flip one payload
    //     byte inside the signed COSE message (not the standalone capsule.json
    //     copy), so the signature no longer covers the bytes the message
    //     claims to carry.
    let mut mutated = statement_bytes.clone();
    let flip_at = mutated.len() - 5; // near the tail: inside the signature bytes
    mutated[flip_at] ^= 0x01;
    let mutated_path = produced.dir.path().join("statement-mutated.cose");
    std::fs::write(&mutated_path, &mutated).unwrap();

    let mutated_produced = Produced {
        dir: tempfile::tempdir().unwrap(), // unused, keeps struct shape
        capsule_path: produced.capsule_path.clone(),
        statement_path: mutated_path.clone(),
        pubkey_path: produced.pubkey_path.clone(),
        capsule_id: produced.capsule_id.clone(),
    };

    let (py_mut_ok, py_mut_out) = run_python_verifier(&env, &mutated_produced);
    eprintln!("python verifier (mutated): {py_mut_out}");
    assert!(
        !py_mut_ok,
        "Python reference ACCEPTED a mutated statement: {py_mut_out}"
    );

    let (go_mut_ok, go_mut_out) = run_go_verifier(&env, &mutated_produced);
    eprintln!("go verifier (mutated): {go_mut_out}");
    assert!(
        !go_mut_ok,
        "Go verifier ACCEPTED a mutated statement: {go_mut_out}"
    );

    assert!(
        capsule_emit::cose::verify_signed_statement(&mutated, &vk).is_err(),
        "Rust's own verifier ACCEPTED a mutated statement"
    );
}

/// REVERSE DIRECTION (inline signature envelope): the Rust verifier
/// must ACCEPT a COSE_Sign1 the PYTHON reference produced with its NON-CANONICAL
/// protected-header key order (3, 4, 1) — content_type (3), kid (4), alg (1) —
/// which RFC 9052 §9 forbids but which the signature check must nonetheless honor
/// because the `Sig_structure` is built over the protected bstr AS RECEIVED.
///
/// This is NOT `#[ignore]`d and needs NO env vars / no Python at test time: it
/// loads a COMMITTED golden fixture
/// (`tests/fixtures/python_produced_noncanonical_envelope.hex`, produced ONCE by
/// `tests/scripts/produce_python_envelope.py` — the documented regenerator, not
/// called from here). A stored insertion-order envelope that Rust verifies is a
/// golden vector for as-received behavior; it stays valid even if the Python
/// reference later moves to canonical order.
#[test]
fn rust_verifies_a_python_produced_noncanonical_envelope() {
    // Known fixture inputs (see tests/fixtures/README.md): Ed25519 seed = 0x11*32,
    // signed digest = 0x22*32. The verifying key is derived from the seed; the
    // envelope's kid equals this raw public key.
    let signing_key = ed25519_dalek::SigningKey::from_bytes(&[0x11u8; 32]);
    let vk = signing_key.verifying_key();

    let hex = include_str!("fixtures/python_produced_noncanonical_envelope.hex");
    let envelope: Vec<u8> = hex
        .split_whitespace()
        .collect::<String>()
        .as_bytes()
        .chunks(2)
        .map(|pair| {
            u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
                .expect("fixture is valid hex")
        })
        .collect();

    // Prove the fixture really carries the NON-canonical (3, 4, 1) order, so a
    // regeneration that accidentally emitted canonical order would fail here and
    // not silently weaken the guarantee. Read the label order straight off the
    // PRESERVED original protected bytes (the exact bytes the signature covers),
    // not off `coset`'s re-serialized view.
    let sign1 = coset::CoseSign1::from_tagged_slice(&envelope)
        .expect("fixture is a valid COSE_Sign1 (tag 18)");
    let protected_bstr = &sign1.protected.original_data.expect(
        "coset preserves the original protected-header bytes on parse (the as-received bstr)",
    );
    let protected_map: coset::cbor::value::Value =
        coset::cbor::from_reader(&protected_bstr[..]).expect("protected bstr is a CBOR map");
    let order: Vec<i64> = match &protected_map {
        coset::cbor::value::Value::Map(entries) => entries
            .iter()
            .map(|(k, _)| match k {
                coset::cbor::value::Value::Integer(i) => i64::try_from(*i).unwrap(),
                other => panic!("non-integer protected-header label: {other:?}"),
            })
            .collect(),
        other => panic!("protected header is not a CBOR map: {other:?}"),
    };
    assert_eq!(
        order,
        vec![3, 4, 1],
        "fixture must carry the NON-canonical Python insertion order (3, 4, 1); \
         regenerate if the reference changed"
    );

    // The Rust verifier MUST ACCEPT the Python-produced, non-canonical envelope: the
    // signature is byte-exact over the protected bstr as received, so key order
    // cannot matter.
    let verified = capsule_emit::cose::verify_signed_statement(&envelope, &vk)
        .expect("Rust must ACCEPT a Python-produced non-canonical envelope");
    assert_eq!(
        verified.payload,
        vec![0x22u8; 32],
        "verified payload must be the fixture's signed digest (0x22*32)"
    );
}
