//! Settlement-records conformance: legs sealed by THIS crate's
//! [`capsule_emit::settlement`] module must derive the expected states under
//! the Python reference verifier (`capsule_emit.settlement.verify_settlements`).
//!
//! The legs carry the numbers of a real two-node Lightning run: four invoices,
//! each 1,000 msat sent by the payer with a routing fee of 0 and 995 msat
//! credited to the payee with a receive fee of 5; the provider seals one terms
//! leg per invoice, and both sides seal the delivery of one exchange. Expected:
//! every invoice `agreed` (`settled`, terms amount `equal`), delivery `matched`
//! on the invoice the delivered legs cite, no failures.
//!
//! `#[ignore]`d and gated on env vars, like `chain_ledger_conformance.rs`:
//!
//!   AAC_PYTHON=python3 \
//!   AAC_SETTLEMENT_VERIFY_SCRIPT=$PWD/tests/scripts/verify_rust_settlement.py \
//!     cargo test --test settlement_conformance -- --ignored --nocapture

use capsule_emit::capsule::LocalRecordHeader;
use capsule_emit::settlement::{amount, build_leg, counterparty_reference, seal_leg};
use ed25519_dalek::SigningKey;
use serde_json::{json, Map, Value};
use std::process::Command;

const HASHES: [&str; 4] = [
    "f9389cf7848df920cd37d1b2ddcae5ac05df5156f6e1fc7c5e9a8b72455f7b8f",
    "d5e349b5e4369a11ae46ab2cdba6f68cac39c024c0d9679c84ad6b02ff48533b",
    "8437d5a5e0c5aa3304c0e0d17288cd504593ca7e560e3691a30739f1725ee7c5",
    "59c0c4d2859c994004ad65ce13831b9cdaf688828d6c9f08cdb06f1bda9f15dd",
];
const RESPONSE_DIGEST: &str = "33e3aff1f39db84ca6bc2e4a8d19703c57566b215e2fa5282dcfefc983bf4ebf";
const AT: &str = "2026-10-02T02:41:16Z";

fn m(pairs: Vec<(&str, Value)>) -> Map<String, Value> {
    pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

fn msat(v: u128) -> Value {
    amount(v, "BTC", 11)
}

fn legs() -> Vec<Value> {
    let payer = SigningKey::from_bytes(&[0x11; 32]);
    let payee = SigningKey::from_bytes(&[0x22; 32]);
    let header = |operator| LocalRecordHeader {
        operator,
        developer: "capsule-emit/settlement-conformance",
        provider: "mesh-llm",
        model_id: "local-gguf",
    };
    let (payer_h, payee_h) = (header("payer"), header("payee"));
    let mut out = Vec::new();
    let mut output_terms = String::new();
    for hash in HASHES {
        let ln = json!({"type": "ln.payment_hash", "value": hash});
        let terms = seal_leg(
            &payee_h,
            format!("settlement/terms/{hash}"),
            build_leg(
                "terms",
                "payee",
                m(vec![("amount", msat(1000)), ("payment_ref", ln.clone())]),
            )
            .unwrap(),
            None,
            None,
            &payee,
        )
        .unwrap();
        let terms_ref = terms["capsule_id"].as_str().unwrap().to_string();
        let payer_leg = seal_leg(
            &payer_h,
            format!("settlement/payer_observed/{hash}"),
            build_leg(
                "payer_observed",
                "payer",
                m(vec![
                    ("terms_ref", json!(terms_ref)),
                    ("amount", msat(1000)),
                    ("routing_fee", msat(0)),
                    ("payment_ref", ln.clone()),
                    ("status", json!("settled")),
                    ("observed_at", json!(AT)),
                ]),
            )
            .unwrap(),
            None,
            None,
            &payer,
        )
        .unwrap();
        let payee_leg = seal_leg(
            &payee_h,
            format!("settlement/payee_observed/{hash}"),
            build_leg(
                "payee_observed",
                "payee",
                m(vec![
                    ("terms_ref", json!(terms_ref)),
                    ("received", msat(995)),
                    ("receive_fee", msat(5)),
                    ("payment_ref", ln),
                    ("status", json!("settled")),
                    ("observed_at", json!(AT)),
                ]),
            )
            .unwrap(),
            Some(json!([counterparty_reference(
                payer_leg["capsule_id"].as_str().unwrap()
            )])),
            None,
            &payee,
        )
        .unwrap();
        output_terms = terms_ref;
        out.extend([terms, payer_leg, payee_leg]);
    }
    for (role, direction, key, h) in [
        ("payee", "sent", &payee, &payee_h),
        ("payer", "received", &payer, &payer_h),
    ] {
        out.push(
            seal_leg(
                h,
                format!("settlement/delivered/{direction}"),
                build_leg(
                    "delivered",
                    role,
                    m(vec![
                        ("terms_ref", json!(output_terms)),
                        ("observed_at", json!(AT)),
                        (
                            "delivery",
                            json!({"direction": direction, "content_digest": RESPONSE_DIGEST}),
                        ),
                    ]),
                )
                .unwrap(),
                None,
                None,
                key,
            )
            .unwrap(),
        );
    }
    out
}

#[test]
#[ignore]
fn rust_sealed_legs_derive_agreed_under_the_python_reference() {
    let (Ok(python), Ok(script)) = (
        std::env::var("AAC_PYTHON"),
        std::env::var("AAC_SETTLEMENT_VERIFY_SCRIPT"),
    ) else {
        eprintln!("AAC_PYTHON / AAC_SETTLEMENT_VERIFY_SCRIPT not set; skipping (see module docs)");
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("legs.json");
    std::fs::write(&path, serde_json::to_vec(&legs()).unwrap()).unwrap();
    let out = Command::new(python)
        .arg(script)
        .arg(&path)
        .output()
        .expect("run the reference");
    let stdout = String::from_utf8_lossy(&out.stdout);
    eprintln!("{stdout}{}", String::from_utf8_lossy(&out.stderr));
    let report: Value = serde_json::from_str(stdout.trim()).expect("one JSON line");
    assert_eq!(report["conforming"], json!(true));
    assert_eq!(report["failures"], json!([]));
    let states: Vec<&str> = report["settlements"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["payment_state"].as_str().unwrap())
        .collect();
    assert_eq!(states, vec!["agreed"; 4]);
    let matched = report["settlements"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|s| s["delivery_state"] == json!("matched"))
        .count();
    assert_eq!(matched, 1);
    assert!(out.status.success());
}

#[test]
fn the_legs_pass_this_crates_own_structure_checks() {
    for capsule in legs() {
        assert!(capsule_emit::settlement::structure_failures(&capsule["settlement"]).is_empty());
    }
}
