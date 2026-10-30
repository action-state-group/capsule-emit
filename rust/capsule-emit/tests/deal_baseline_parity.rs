//! Byte-identity of `produce::build` with Go's `emit.Seal` on x-deal-v0
//! records (#385).
//!
//! `tests/vectors/evidencebook-deal/baseline-vectors.json` is evidencebook's
//! `dealprofile/testdata/baseline-vectors.json`, frozen from Go's `emit.Seal`
//! without its Producer Envelope. For every deal step and report it holds the
//! payload and the Capsule Go sealed over it. The test takes each Capsule's
//! members, commits the payload's own digest as `agent_input_digest`, builds
//! with `produce::build` and requires Go's bytes and `capsule_id`. The frozen
//! Capsules cover fyi and decide steps (disposition and effect present only on
//! decide, `irreversibility_class`), chained and standalone records, a model
//! attestation with no model, and top-level extension members (`deal_id`,
//! `settlement`). None leaves out `verdict_class` or carries `reason_digest`;
//! `produce_interop.rs` checks those against Go's produce cross-check pack.

use std::path::Path;

use capsule_emit::produce::{self, *};
use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

/// The members `produce::build` writes itself; any other top-level member of
/// a frozen Capsule is an extension.
const BUILT_MEMBERS: &[&str] = &[
    "spec_version",
    "format_version",
    "canonicalization_id",
    "capsule_id",
    "action_id",
    "action_type",
    "operator",
    "developer",
    "timestamp",
    "assurance",
    "epoch_id",
    "domain",
    "provenance",
    "provenance_mode",
    "disposition",
    "effect",
    "chain",
    "model_attestation",
    "references",
];

fn decode(data: &[u8]) -> Value {
    capsule_emit::jcs::decode_record(data).unwrap()
}

fn text(map: &Map<String, Value>, key: &str) -> String {
    map.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

/// The `Input` for a frozen Capsule's members, with `agent_input_digest`
/// taken from the payload rather than from the Capsule.
fn input_for(capsule: &Map<String, Value>, agent_input_digest: String) -> Input {
    for member in [
        "epoch_id",
        "domain",
        "provenance",
        "provenance_mode",
        "references",
    ] {
        assert!(!capsule.contains_key(member), "unexpected {member}");
    }
    assert_eq!(
        capsule["model_attestation"],
        serde_json::json!({"compute_attestation": {"agent_input_digest": agent_input_digest}}),
        "the frozen Capsule commits only the payload digest"
    );
    let timestamp = DateTime::parse_from_rfc3339(&text(capsule, "timestamp"))
        .unwrap()
        .with_timezone(&Utc);
    let object = |key: &str| capsule.get(key).map(|v| v.as_object().unwrap());
    Input {
        action_id: text(capsule, "action_id"),
        action_type: text(capsule, "action_type"),
        operator: text(capsule, "operator"),
        developer: text(capsule, "developer"),
        timestamp: Some(timestamp),
        disposition: object("disposition").map(|d| Disposition {
            decision: text(d, "decision"),
            approver: text(d, "approver"),
            human_disposed: d["human_disposed"].as_bool().unwrap(),
            verdict_class: text(d, "verdict_class"),
            reason_digest: text(d, "reason_digest"),
        }),
        effect: object("effect").map(|e| Effect {
            effect_type: text(e, "type"),
            status: text(e, "status"),
            irreversibility_class: text(e, "irreversibility_class"),
            effect_attestation: text(e, "effect_attestation"),
            request_digest: text(e, "request_digest"),
            response_digest: text(e, "response_digest"),
            external_ref: text(e, "external_ref"),
        }),
        chain: object("chain").map(|c| Chain {
            parent_capsule_id: text(c, "parent_capsule_id"),
            relation: text(c, "relation"),
        }),
        compute: Some(ComputeAttestation {
            agent_input_digest,
            ..Default::default()
        }),
        extensions: capsule
            .iter()
            .filter(|(name, _)| !BUILT_MEMBERS.contains(&name.as_str()))
            .map(|(name, value)| Extension {
                name: name.clone(),
                value: serde_json::to_vec(value).unwrap(),
            })
            .collect(),
        ..Default::default()
    }
}

/// Build one frozen record's Capsule from its payload and require Go's bytes.
/// Returns the Capsule's members.
fn check(at: &str, record: &Value) -> Map<String, Value> {
    let payload = decode(record["payload"].as_str().unwrap().as_bytes());
    let digest = produce::digest_json(&payload).unwrap();
    if let Some(frozen) = record.get("digest") {
        assert_eq!(frozen.as_str().unwrap(), digest, "{at} payload digest");
    }
    let want = record["capsule"].as_str().unwrap();
    let Value::Object(capsule) = decode(want.as_bytes()) else {
        panic!("{at}: Capsule is not an object")
    };
    let built = produce::build(input_for(&capsule, digest)).unwrap_or_else(|e| panic!("{at}: {e}"));
    assert_eq!(
        built.capsule_id,
        record["capsule_id"].as_str().unwrap(),
        "{at} capsule_id"
    );
    assert_eq!(String::from_utf8(built.json).unwrap(), want, "{at} Capsule");
    capsule
}

#[test]
fn produce_build_seals_go_deal_capsules() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/vectors/evidencebook-deal/baseline-vectors.json");
    let vectors = decode(&std::fs::read(&path).unwrap());
    let (mut steps, mut decides, mut reports, mut extensions) = (0, 0, 0, Vec::new());
    for deal in vectors["deals"].as_array().unwrap() {
        let flow = deal["flow"].as_str().unwrap();
        for step in deal["steps"].as_array().unwrap() {
            let capsule = check(&format!("{flow} step {}", step["n"]), step);
            steps += 1;
            if capsule["action_type"] == ACTION_TYPE_DECIDE {
                decides += 1;
            }
            extensions.extend(
                capsule
                    .keys()
                    .filter(|k| !BUILT_MEMBERS.contains(&k.as_str()))
                    .cloned(),
            );
        }
        for report in deal
            .get("reports")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            check(&format!("{flow} {} report", report["form"]), report);
            reports += 1;
        }
    }
    // The set must keep exercising what #385 is about.
    assert_eq!((steps, reports), (71, 7));
    assert!(decides > 0, "a decide step is frozen");
    for name in ["deal_id", "settlement"] {
        assert!(
            extensions.iter().any(|e| e == name),
            "an extension {name} is frozen"
        );
    }
}
