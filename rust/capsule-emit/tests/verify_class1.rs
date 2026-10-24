//! `verify::verify_offline` takes its Class 1 verdict from the reference
//! verifier (`agent_action_capsule::verify::verify`): records that are
//! correctly signed, as a producer envelope and as a COSE statement, with a
//! `capsule_id` that recomputes, still fail it, and the finding says which
//! check refused them. The checks themselves are tested against the
//! conformance vectors in agent-action-capsule.

use std::collections::HashSet;

use capsule_emit::cose::{build_signed_statement, verify_producer_envelope, SignedStatementInput};
use capsule_emit::verify::{verify_offline, VerifyReport};
use serde_json::{json, Value};

const KEY: [u8; 32] = [7; 32];

/// `verify_offline` over `sealed`, signed as a statement whose subject is its
/// carried `capsule_id`, with an optional store of known ids.
fn offline_with(sealed: &Value, store: Option<&HashSet<String>>) -> VerifyReport {
    let key = ed25519_dalek::SigningKey::from_bytes(&KEY);
    let id = sealed["capsule_id"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let statement = build_signed_statement(
        &SignedStatementInput {
            payload: &capsule_emit::capsule::payload_bytes(sealed),
            issuer: "example-issuer",
            subject: &id,
            content_type: "application/json",
        },
        &key,
    );
    verify_offline(sealed, &statement, &key.verifying_key(), store)
}

fn offline(sealed: &Value) -> VerifyReport {
    offline_with(sealed, None)
}

/// A well-formed format-4 record: passes every check.
fn record() -> Value {
    json!({
        "spec_version": "draft-mih-scitt-agent-action-capsule-05",
        "format_version": "4",
        "canonicalization_id": "jcs",
        "action_id": "example/1",
        "action_type": "decide",
        "operator": "op",
        "developer": "dev@v1",
        "timestamp": "2026-09-29T00:00:00Z",
        "effect": {
            "status": "confirmed",
            "type": "inference_completion",
            "request_digest": "b".repeat(64),
            "response_digest": "c".repeat(64),
            "effect_attestation": "runtime_claimed"
        },
        "disposition": {"decision": "accept", "approver": "policy", "human_disposed": false, "verdict_class": "confirmed"}
    })
}

/// `capsule` with its id recomputed and its producer envelope attached, as a
/// producer that signs whatever it is handed would.
fn seal(mut capsule: Value) -> Value {
    let id = capsule_emit::jcs::compute_capsule_id(&capsule).expect("id");
    capsule["capsule_id"] = json!(id);
    let key = ed25519_dalek::SigningKey::from_bytes(&[7; 32]);
    capsule_emit::capsule::attach_producer_envelope(&mut capsule, &key).expect("envelope");
    capsule
}

#[test]
fn a_well_formed_signed_record_passes() {
    let sealed = seal(record());
    assert!(verify_producer_envelope(&sealed).is_ok());
    let report = offline(&sealed);
    assert!(report.ok(), "{:?}", report.findings);
}

#[test]
fn signed_records_that_fail_the_checks_are_refused() {
    type Change = fn(&mut Value);
    let cases: &[(&str, Change, u8, &str)] = &[
        (
            "action_type",
            |c| c["action_type"] = json!("act"),
            1,
            "action_type_invalid",
        ),
        (
            "no operator",
            |c| drop(c.as_object_mut().unwrap().remove("operator")),
            1,
            "missing_required_field",
        ),
        (
            "operator not a string",
            |c| c["operator"] = json!(7),
            1,
            "field_not_string",
        ),
        (
            "effect not an object",
            |c| c["effect"] = json!("done"),
            1,
            "block_not_object",
        ),
        (
            "constraints not an array",
            |c| c["constraints"] = json!({}),
            1,
            "constraints_not_array",
        ),
        (
            "approver",
            |c| c["disposition"]["approver"] = json!("robot"),
            1,
            "approver_invalid",
        ),
        (
            "human_disposed",
            |c| c["disposition"]["human_disposed"] = json!("no"),
            1,
            "field_not_bool",
        ),
        (
            "no decision",
            |c| drop(c["disposition"].as_object_mut().unwrap().remove("decision")),
            1,
            "missing_required_field",
        ),
        (
            "reference not an object",
            |c| c["references"] = json!([7]),
            1,
            "reference_malformed",
        ),
        (
            "canonicalization",
            |c| c["canonicalization_id"] = json!("jcs-n"),
            1,
            "canonicalization_profile_mismatch",
        ),
        (
            "confirmed without response",
            |c| {
                drop(
                    c["effect"]
                        .as_object_mut()
                        .unwrap()
                        .remove("response_digest"),
                )
            },
            3,
            "confirmed_without_response",
        ),
        (
            "never-dispatch with an effect",
            |c| c["disposition"]["verdict_class"] = json!("blocked"),
            4,
            "verdict_effect_conflict",
        ),
        (
            "no effect_attestation",
            |c| {
                drop(
                    c["effect"]
                        .as_object_mut()
                        .unwrap()
                        .remove("effect_attestation"),
                )
            },
            5,
            "effect_attestation_missing",
        ),
        (
            "chain parent",
            |c| c["chain"] = json!({"parent_capsule_id": "x", "relation": "follows"}),
            6,
            "chain_parent_malformed",
        ),
        (
            "chain relation",
            |c| c["chain"] = json!({"parent_capsule_id": "a".repeat(64)}),
            6,
            "missing_required_field",
        ),
        (
            "effect_mode overclaim",
            |c| {
                c["effect"]["status"] = json!("dispatched");
                c["assurance"] = json!({"effect_mode": "confirmed"});
            },
            7,
            "assurance_overclaim",
        ),
        (
            "time laundering",
            |c| {
                c["provenance_mode"] = json!({
                    "mode": "backfilled", "source_ref": {"type": "t", "digest_alg": "SHA-256", "digest": "d"},
                    "source_asserted_at": "2026-01-01T00:00:00Z", "import_batch": "b", "imported_at": "2026-01-01T00:00:00Z"
                })
            },
            9,
            "provenance_time_laundering_shape",
        ),
    ];
    for (name, change, check, code) in cases {
        let mut capsule = record();
        change(&mut capsule);
        let sealed = seal(capsule);
        assert!(
            verify_producer_envelope(&sealed).is_ok(),
            "{name}: the id recomputes and the signature verifies"
        );
        let report = offline(&sealed);
        assert!(!report.ok(), "{name}: must be refused");
        assert!(
            !report.structure_ok,
            "{name}: refused by the Class 1 checks"
        );
        assert!(report.cose_ok && report.payload_matches_capsule, "{name}");
        let want = format!("check {check}: {code}: ");
        assert!(
            report.findings.iter().any(|f| f.starts_with(&want)),
            "{name}: expected check {check} {code}, got {:?}",
            report.findings
        );
    }
}

/// A carried `capsule_id` that does not recompute fails check 2, and no
/// subject can match it.
#[test]
fn a_tampered_record_fails_the_capsule_id_check() {
    let mut sealed = seal(record());
    sealed["operator"] = json!("someone-else");
    let report = offline(&sealed);
    assert!(!report.ok());
    assert!(!report.capsule_id_ok);
    assert!(!report.subject_matches_capsule_id);
    assert_eq!(report.capsule_id, None);
    assert!(
        report
            .findings
            .iter()
            .any(|f| f.starts_with("check 2: capsule_id_mismatch: ")),
        "{:?}",
        report.findings
    );
}

/// Chain-parent membership is check 6 against the supplied ids: a note
/// without a store, a failure when the store lacks the parent.
#[test]
fn chain_parent_membership_uses_the_supplied_store() {
    let parent = "a".repeat(64);
    let mut capsule = record();
    capsule["chain"] = json!({"parent_capsule_id": parent, "relation": "follows"});
    let sealed = seal(capsule);

    let alone = offline(&sealed);
    assert!(alone.ok(), "{:?}", alone.findings);
    assert!(
        alone.findings.iter().any(|f| f.starts_with("check 6: ")),
        "{:?}",
        alone.findings
    );

    let with_parent = HashSet::from([parent.clone()]);
    let found = offline_with(&sealed, Some(&with_parent));
    assert!(found.ok(), "{:?}", found.findings);
    assert!(found.chain_ok);

    let without_parent = HashSet::from(["b".repeat(64)]);
    let missing = offline_with(&sealed, Some(&without_parent));
    assert!(!missing.ok());
    assert!(!missing.chain_ok);
    assert!(!missing.structure_ok);
}
