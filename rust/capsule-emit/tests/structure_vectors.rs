//! `structure::check_structure` against the reference verifier's frozen
//! Class 1 vectors (agent-action-capsule, pinned in `tests/vectors/SOURCES.md`; vendored under
//! `tests/vectors/aac-capsule/` and `tests/vectors/aac-provenance-mode/`,
//! checksum-gated in CI): for every case, the same `ok`, and the same error
//! and warning findings, by check and code, in the same order.
//!
//! A store-level case (`{"ledger": [...]}`) is checked record by record
//! against its per-record expected result, minus the one finding that needs
//! the store (`chain_parent_missing`); informational findings are not
//! compared (the module does not report them).
//!
//! Then: records that are correctly signed, with a `capsule_id` that
//! recomputes, and still fail these checks.

use std::path::{Path, PathBuf};

use capsule_emit::cose::verify_producer_envelope;
use capsule_emit::structure::{check_structure, Severity, StructureReport};
use serde_json::{json, Value};

const STORE_ONLY: &[&str] = &["chain_parent_missing"];

fn vectors_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/vectors")
}

fn read(path: &Path) -> Value {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

/// The (check, code) of each finding of one severity, in order.
fn expected_findings(expected: &Value, severity: &str) -> Vec<(Option<u64>, String)> {
    expected["findings"]
        .as_array()
        .map(|findings| {
            findings
                .iter()
                .filter(|f| f["severity"] == severity)
                .filter(|f| !STORE_ONLY.contains(&f["code"].as_str().unwrap_or_default()))
                .map(|f| {
                    (
                        f["check"].as_u64(),
                        f["code"].as_str().unwrap_or_default().to_string(),
                    )
                })
                .collect()
        })
        .unwrap_or_default()
}

fn got_findings(report: &StructureReport, severity: Severity) -> Vec<(Option<u64>, String)> {
    report
        .findings
        .iter()
        .filter(|f| f.severity == severity)
        .map(|f| (f.check.map(u64::from), f.code.to_string()))
        .collect()
}

/// Compare one record with its expected result; `Err` describes a difference.
fn compare(name: &str, input: &Value, expected: &Value) -> Result<(), String> {
    let report = check_structure(input);
    let want_errors = expected_findings(expected, "error");
    let got_errors = got_findings(&report, Severity::Error);
    let want_warnings = expected_findings(expected, "warning");
    let got_warnings = got_findings(&report, Severity::Warning);
    let store_only = expected["findings"].as_array().is_some_and(|fs| {
        fs.iter()
            .any(|f| STORE_ONLY.contains(&f["code"].as_str().unwrap_or_default()))
    });
    let want_ok =
        expected["ok"].as_bool().unwrap_or(false) || (store_only && want_errors.is_empty());
    if want_errors != got_errors || want_warnings != got_warnings || want_ok != report.ok() {
        return Err(format!(
            "{name}: expected ok={want_ok} errors={want_errors:?} warnings={want_warnings:?}; \
             got ok={} errors={got_errors:?} warnings={got_warnings:?}",
            report.ok()
        ));
    }
    Ok(())
}

fn run_corpus(dir: &str) -> (usize, Vec<String>) {
    let root = vectors_dir().join(dir);
    let mut cases: Vec<PathBuf> = std::fs::read_dir(&root)
        .expect("vector directory")
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.join("input.json").is_file())
        .collect();
    cases.sort();
    let mut checked = 0;
    let mut differences = Vec::new();
    for case in cases {
        let name = case.file_name().unwrap().to_string_lossy().into_owned();
        let input = read(&case.join("input.json"));
        let expected = read(&case.join("expected.json"));
        match (input.get("ledger"), expected.get("results")) {
            (Some(Value::Array(records)), Some(Value::Array(results))) => {
                assert_eq!(
                    records.len(),
                    results.len(),
                    "{name}: one result per record"
                );
                for (i, (record, result)) in records.iter().zip(results).enumerate() {
                    if let Err(d) = compare(&format!("{name}[{i}]"), record, result) {
                        differences.push(d);
                    }
                    checked += 1;
                }
            }
            _ => {
                if let Err(d) = compare(&name, &input, &expected) {
                    differences.push(d);
                }
                checked += 1;
            }
        }
    }
    (checked, differences)
}

#[test]
fn every_capsule_vector_gets_the_reference_verdict_and_findings() {
    let (checked, differences) = run_corpus("aac-capsule");
    assert!(checked >= 94, "checked {checked} records");
    assert!(differences.is_empty(), "{}", differences.join("\n"));
}

#[test]
fn every_provenance_mode_vector_gets_the_reference_verdict_and_findings() {
    let (checked, differences) = run_corpus("aac-provenance-mode");
    assert!(checked >= 24, "checked {checked} records");
    assert!(differences.is_empty(), "{}", differences.join("\n"));
}

// ---------------------------------------------------------------------------
// Signed, identified, and still malformed
// ---------------------------------------------------------------------------

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
    let report = check_structure(&sealed);
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
        let report = check_structure(&sealed);
        assert!(!report.ok(), "{name}: must be refused");
        assert!(
            report
                .errors()
                .any(|f| f.check == Some(*check) && f.code == *code),
            "{name}: expected check {check} {code}, got {:?}",
            report.findings
        );
    }
}

/// `verify_offline` gates on the checks too: a record that is signed as a
/// statement and whose id recomputes, but that fails them, is not ok.
#[test]
fn verify_offline_refuses_a_signed_record_that_fails_the_checks() {
    use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
    let key = ed25519_dalek::SigningKey::from_bytes(&[7; 32]);
    for (capsule, want_ok) in [
        (record(), true),
        (
            {
                let mut c = record();
                c["action_type"] = json!("act");
                c
            },
            false,
        ),
    ] {
        let sealed = seal(capsule);
        let payload = capsule_emit::capsule::payload_bytes(&sealed);
        let id = sealed["capsule_id"].as_str().unwrap().to_string();
        let statement = build_signed_statement(
            &SignedStatementInput {
                payload: &payload,
                issuer: "example-issuer",
                subject: &id,
                content_type: "application/json",
            },
            &key,
        );
        let report =
            capsule_emit::verify::verify_offline(&sealed, &statement, &key.verifying_key(), None);
        assert_eq!(report.ok(), want_ok, "{:?}", report.findings);
        assert_eq!(report.structure_ok, want_ok);
    }
}

// ---------------------------------------------------------------------------
// Known differences from the reference verifier (README, "Differences from
// the reference verifier"): pinned, so a change in either is noticed.
// ---------------------------------------------------------------------------

fn parsed(text: &str) -> Value {
    serde_json::from_str(text).expect("JSON")
}

/// `-0` parses as a float in Rust (an integer zero in Python), so it is
/// refused as a float in a digest field.
#[test]
fn negative_zero_is_refused_as_a_float() {
    let mut text = record().to_string();
    text.insert_str(text.len() - 1, r#","n":-0"#);
    let report = check_structure(&parsed(&text));
    assert!(report.errors().any(|f| f.code == "float_in_digest_field"));
}

/// An integer above u64::MAX parses as a float in Rust: refused as
/// `float_in_digest_field` (Python: `unsafe_integer_in_digest_field`).
#[test]
fn an_integer_past_u64_is_refused_as_a_float() {
    let mut text = record().to_string();
    text.insert_str(text.len() - 1, r#","n":18446744073709551616"#);
    let report = check_structure(&parsed(&text));
    assert!(report.errors().any(|f| f.code == "float_in_digest_field"));
    assert!(!report
        .errors()
        .any(|f| f.code == "unsafe_integer_in_digest_field"));
}

/// A value of the wrong JSON type in a string-typed member fails check 1
/// with `field_not_string`, as in the reference (agent-action-capsule#147 and
/// the follow-up for the provenance_mode and references[].retention members).
/// A well-typed unregistered value is still not judged.
#[test]
fn a_non_string_in_a_string_field_fails_check_1() {
    let retention = |member: &str, value: Value| {
        let mut retention =
            json!({"declarant": "ACME-CO", "retained_until": "2027-01-01T00:00:00Z"});
        retention[member] = value;
        let reference =
            json!({"type": "x-artifact", "digest_alg": "SHA-256", "digest": "3".repeat(64)});
        let mut second = reference.clone();
        second["retention"] = retention;
        json!({"references": [reference, second]})
    };
    let provenance = |member: &str, value: Value| {
        let mut pm = json!({
            "mode": "backfilled",
            "source_ref": {"type": "t", "digest_alg": "SHA-256", "digest": "d"},
            "source_asserted_at": "2026-01-01T00:00:00Z",
            "import_batch": "b",
            "imported_at": "2026-01-02T00:00:00Z",
        });
        pm[member] = value;
        json!({"provenance_mode": pm})
    };
    let mut cases = vec![
        (
            json!({"disposition": {"approver": []}}),
            "disposition.approver MUST be a string (§5.4)".to_string(),
        ),
        (
            json!({"epoch_id": {}}),
            "epoch_id MUST be a string when present (§5.1)".to_string(),
        ),
    ];
    for (block, member) in [
        ("disposition", "decision"),
        ("disposition", "verdict_class"),
        ("effect", "status"),
        ("effect", "type"),
        ("effect", "irreversibility_class"),
        ("effect", "effect_attestation"),
        ("effect", "external_ref"),
        ("chain", "relation"),
        ("cross_party", "correlator"),
        ("assurance", "effect_mode"),
        ("assurance", "attestation_mode"),
        ("assurance", "ledger_mode"),
        ("assurance", "cross_party_rung"),
    ] {
        for value in [json!([]), json!({}), json!(7), Value::Null] {
            let mut members = serde_json::Map::new();
            members.insert(member.into(), value);
            cases.push((
                json!({ block: members }),
                format!("{block}.{member} MUST be a string when present (§6 check 1)"),
            ));
        }
    }
    for member in ["source_asserted_at", "import_batch", "imported_at"] {
        cases.push((
            provenance(member, json!(["x"])),
            format!("provenance_mode.{member} MUST be a string when present (§6 check 1)"),
        ));
    }
    for member in ["declarant", "retained_until", "not_retained_after"] {
        cases.push((
            retention(member, json!({"value": "x"})),
            format!("references[1].retention.{member} MUST be a string when present (§5.5.5)"),
        ));
    }
    for (change, detail) in cases {
        let mut capsule = record();
        for (key, value) in change.as_object().unwrap() {
            match (capsule.get(key), value) {
                (Some(Value::Object(_)), Value::Object(members)) => {
                    for (k, v) in members {
                        capsule[key][k] = v.clone();
                    }
                }
                _ => capsule[key] = value.clone(),
            }
        }
        let report = check_structure(&seal(capsule));
        let typed: Vec<_> = report
            .errors()
            .filter(|f| f.code == "field_not_string")
            .map(|f| (f.check, f.detail.as_str()))
            .collect();
        assert_eq!(
            typed,
            [(Some(1), detail.as_str())],
            "{change}: {:?}",
            report.findings
        );
    }

    // A well-typed unregistered value is informational in the reference, so
    // not reported here; the record passes.
    let mut capsule = record();
    capsule["effect"]["type"] = json!("x-unregistered");
    assert!(check_structure(&seal(capsule)).ok());
}
