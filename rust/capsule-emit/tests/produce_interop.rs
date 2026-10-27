//! Byte-identity of `capsule_emit::produce` with the Go module.
//!
//! Two Go-owned packs are replayed:
//! - `go/testdata/capsule-emit/format4-interop-v05`, the Python-generated pack
//!   Go and TypeScript replay (seal, received, WHO/CAN/DID composition);
//! - `go/testdata/produce-cross-check`, whose `expected.json` Go's
//!   `produce_cross_check_test.go` writes from `input.json` and Python's
//!   `tests/test_produce_interop.py` also replays: every producing call,
//!   digests, timestamp normalization, and the error text of each refused
//!   input.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use capsule_emit::produce::{self, *};
use chrono::{DateTime, Utc};
use ed25519_dalek::SigningKey;
use serde_json::{Map, Value};

fn go_testdata(path: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../go/testdata")
        .join(path)
}

fn read_json(path: &Path) -> Value {
    let data = std::fs::read(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    capsule_emit::jcs::decode_record(&data).unwrap()
}

fn text(map: &Map<String, Value>, key: &str) -> String {
    map.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn identity(seed_hex: &str) -> SigningIdentity {
    let seed: [u8; 32] = hex::decode(seed_hex).unwrap().try_into().unwrap();
    SigningIdentity::from_ed25519(SigningKey::from_bytes(&seed))
}

fn timestamp(value: &str) -> Option<DateTime<Utc>> {
    (!value.is_empty()).then(|| {
        DateTime::parse_from_rfc3339(value)
            .unwrap()
            .with_timezone(&Utc)
    })
}

fn model(value: &Value) -> Model {
    let value = value.as_object().unwrap();
    Model {
        provider: text(value, "provider"),
        model_id: text(value, "model_id"),
    }
}

fn slot(slot: &str, member: &Sealed) -> SlotMember {
    match slot {
        "who" => who(member),
        "can" => can(member),
        "did" => did(member),
        "audit" => audit(member),
        other => panic!("unknown slot {other}"),
    }
}

fn members(value: &Value, key: &str, prior: &HashMap<String, Sealed>) -> Vec<SlotMember> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|member| {
            let member = member.as_object().unwrap();
            let name = text(member, key);
            let sealed = prior
                .get(&name)
                .unwrap_or_else(|| panic!("member {name} must precede its use"));
            slot(&text(member, "slot"), sealed)
        })
        .collect()
}

// --- format4-interop-v05 ---------------------------------------------------

#[test]
fn replays_the_format4_interop_v05_pack_byte_for_byte() {
    let root = go_testdata("capsule-emit/format4-interop-v05");
    let spec = read_json(&root.join("input.json"));
    let spec = spec.as_object().unwrap();
    let identity = identity(&text(spec, "seed_hex"));
    let disposition = spec["disposition"].as_object().unwrap();
    let mut results: HashMap<String, Sealed> = HashMap::new();
    let records = spec["records"].as_array().unwrap();
    assert_eq!(records.len(), 5);

    for record in records {
        let record = record.as_object().unwrap();
        let name = text(record, "name");
        let capsule = Input {
            action_id: text(record, "action_id"),
            action_type: text(record, "action_type"),
            operator: text(spec, "operator"),
            developer: text(spec, "developer"),
            timestamp: timestamp(&text(spec, "timestamp")),
            disposition: Some(Disposition {
                decision: text(disposition, "decision"),
                approver: text(disposition, "approver"),
                human_disposed: disposition["human_disposed"].as_bool().unwrap(),
                verdict_class: text(record, "verdict"),
                ..Disposition::default()
            }),
            ..Input::default()
        };
        let sealed = match text(record, "operation").as_str() {
            "seal" => produce::seal(SealInput {
                capsule,
                payload: record.get("payload").cloned(),
                agent_output: record.get("agent_output").cloned(),
                model: record.get("model").map(model),
                runtime: text(record, "runtime"),
                members: None,
                identity: identity.clone(),
            })
            .unwrap(),
            "received" => {
                let built = received(
                    capsule,
                    text(record, "artifact_utf8").as_bytes(),
                    &text(record, "artifact_type"),
                )
                .unwrap();
                let envelope = sign(&built, &identity).unwrap();
                Sealed {
                    capsule_id: built.capsule_id,
                    payload: built.json,
                    envelope,
                }
            }
            "composition" => produce::seal(SealInput {
                capsule,
                payload: None,
                agent_output: None,
                model: None,
                runtime: String::new(),
                members: Some(members(&record["members"], "record", &results)),
                identity: identity.clone(),
            })
            .unwrap(),
            other => panic!("unknown operation {other}"),
        };

        let case = root.join("valid").join(&name);
        let expected = read_json(&case.join("expected.json"));
        assert_eq!(
            sealed.capsule_id,
            expected["capsule_id"].as_str().unwrap(),
            "{name}"
        );
        assert_eq!(
            sealed.payload,
            std::fs::read(case.join("capsule.detached.jcs")).unwrap(),
            "{name}"
        );
        assert_eq!(
            sealed.envelope,
            std::fs::read(case.join("envelope.cose")).unwrap(),
            "{name}"
        );

        let verified = verify_envelope(&sealed.capsule_id, &sealed.envelope).unwrap();
        assert_eq!(
            hex::encode(&verified.public_key),
            expected["public_key_hex"].as_str().unwrap()
        );
        let stored = std::fs::read(case.join("capsule.stored.json")).unwrap();
        assert!(verify_capsule(&stored).unwrap().ok, "{name}");
        let stored = decode_payload(&stored).unwrap();
        assert_eq!(stored["capsule_id"], Value::from(sealed.capsule_id.clone()));
        assert_eq!(
            stored["signature"],
            Value::from(hex::encode(&sealed.envelope))
        );
        assert_eq!(stored["spec_version"], Value::from(SPEC_VERSION));
        results.insert(name, sealed);
    }
}

// --- produce-cross-check -----------------------------------------------------

/// Rejections whose text after the first ": " comes from each language's own
/// JSON decoder; the part before it must still match. Every other rejection,
/// canonicalizer number errors included, is Go's text word for word.
const DECODER_TEXT_DIFFERS: [&str; 1] = ["extension_trailing_value"];

fn reference(value: &Value) -> Reference {
    let value = value.as_object().unwrap();
    Reference {
        reference_type: text(value, "type"),
        digest_alg: text(value, "digest_alg"),
        digest: text(value, "digest"),
        citation_purpose: text(value, "citation_purpose"),
        log_coordinates: value
            .get("log_coordinates")
            .map(|v| v.as_object().unwrap().clone()),
    }
}

fn extension(value: &Value) -> Extension {
    let value = value.as_object().unwrap();
    // `value_text` is exact JSON text a JSON document cannot hold; `value`
    // is any JSON value, written back with its numbers as given.
    let bytes = match value.get("value_text") {
        Some(raw) => raw.as_str().unwrap().as_bytes().to_vec(),
        None => serde_json::to_vec(&value["value"]).unwrap(),
    };
    Extension {
        name: text(value, "name"),
        value: bytes,
    }
}

/// Mirror of Go's `crossCheckInput.toInput`.
fn cross_check_input(raw: &Value) -> Input {
    let raw = raw.as_object().unwrap();
    let block = |key: &str| raw.get(key).map(|v| v.as_object().unwrap().clone());
    Input {
        action_id: text(raw, "action_id"),
        action_type: text(raw, "action_type"),
        operator: text(raw, "operator"),
        developer: text(raw, "developer"),
        timestamp: timestamp(&text(raw, "timestamp")),
        epoch_id: text(raw, "epoch_id"),
        domain: text(raw, "domain"),
        provenance: text(raw, "provenance"),
        disposition: block("disposition").map(|d| Disposition {
            decision: text(&d, "decision"),
            approver: text(&d, "approver"),
            human_disposed: d
                .get("human_disposed")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            verdict_class: text(&d, "verdict_class"),
            reason_digest: text(&d, "reason_digest"),
        }),
        effect: block("effect").map(|e| Effect {
            effect_type: text(&e, "type"),
            status: text(&e, "status"),
            irreversibility_class: text(&e, "irreversibility_class"),
            effect_attestation: text(&e, "effect_attestation"),
            request_digest: text(&e, "request_digest"),
            response_digest: text(&e, "response_digest"),
            external_ref: text(&e, "external_ref"),
        }),
        chain: block("chain").map(|c| Chain {
            parent_capsule_id: text(&c, "parent_capsule_id"),
            relation: text(&c, "relation"),
        }),
        references: raw
            .get("references")
            .map(|r| r.as_array().unwrap().iter().map(reference).collect()),
        model: raw.get("model").map(model),
        compute: block("compute").map(|c| ComputeAttestation {
            agent_input_digest: text(&c, "agent_input_digest"),
            agent_output_digest: text(&c, "agent_output_digest"),
            runtime: text(&c, "runtime"),
        }),
        provenance_mode: block("provenance_mode").map(|m| ProvenanceMode {
            mode: text(&m, "mode"),
            source_ref: m.get("source_ref").map(reference),
            source_asserted_at: text(&m, "source_asserted_at"),
            import_batch: text(&m, "import_batch"),
            imported_at: text(&m, "imported_at"),
            time_rung: text(&m, "time_rung"),
        }),
        extensions: raw
            .get("extensions")
            .map(|list| list.as_array().unwrap().iter().map(extension).collect())
            .unwrap_or_default(),
    }
}

/// Mirror of Go's `runCrossCheckRecord`.
fn run_record(
    record: &Map<String, Value>,
    identity: &SigningIdentity,
    prior: &HashMap<String, Sealed>,
) -> Result<Sealed, ProduceError> {
    let input = cross_check_input(&record["input"]);
    let artifact = || match record.get("artifact_hex") {
        Some(raw) => hex::decode(raw.as_str().unwrap()).unwrap(),
        None => text(record, "artifact_utf8").into_bytes(),
    };
    let slotted = || record.get("members").map(|m| members(m, "record", prior));
    let built = match text(record, "operation").as_str() {
        "seal" => {
            let payload = if record.get("payload_null").and_then(Value::as_bool) == Some(true) {
                Some(Value::Null)
            } else {
                record.get("payload").cloned()
            };
            return produce::seal(SealInput {
                capsule: input,
                payload,
                agent_output: record.get("agent_output").cloned(),
                model: record.get("model").map(model),
                runtime: text(record, "runtime"),
                members: slotted(),
                identity: identity.clone(),
            });
        }
        "build" => build(input)?,
        "carry" => carry(input, &artifact())?,
        "received" => received(input, &artifact(), &text(record, "artifact_type"))?,
        "composition" => build_composition(input, &slotted().unwrap_or_default())?,
        other => panic!("unknown operation {other}"),
    };
    let envelope = sign(&built, identity)?;
    Ok(Sealed {
        capsule_id: built.capsule_id,
        payload: built.json,
        envelope,
    })
}

struct CrossCheck {
    spec: Map<String, Value>,
    expected: Map<String, Value>,
    identity: SigningIdentity,
}

fn cross_check() -> CrossCheck {
    let root = go_testdata("produce-cross-check");
    let spec = read_json(&root.join("input.json"));
    let expected: Value =
        serde_json::from_slice(&std::fs::read(root.join("expected.json")).unwrap()).unwrap();
    let identity = identity(spec["seed_hex"].as_str().unwrap());
    assert_eq!(
        hex::encode(identity.public_key()),
        expected["public_key_hex"].as_str().unwrap()
    );
    CrossCheck {
        spec: spec.as_object().unwrap().clone(),
        expected: expected.as_object().unwrap().clone(),
        identity,
    }
}

fn entries<'a>(map: &'a Map<String, Value>, key: &str) -> Vec<&'a Map<String, Value>> {
    map[key]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_object().unwrap())
        .collect()
}

fn replay_records(pack: &CrossCheck) -> HashMap<String, Sealed> {
    let mut prior = HashMap::new();
    for record in entries(&pack.spec, "records") {
        let name = text(record, "name");
        let sealed =
            run_record(record, &pack.identity, &prior).unwrap_or_else(|e| panic!("{name}: {e}"));
        prior.insert(name, sealed);
    }
    prior
}

#[test]
fn cross_check_records_are_byte_identical_to_go() {
    let pack = cross_check();
    let records = replay_records(&pack);
    let expected = entries(&pack.expected, "records");
    assert_eq!(expected.len(), entries(&pack.spec, "records").len());
    for want in expected {
        let name = text(want, "name");
        let sealed = &records[&name];
        assert_eq!(sealed.capsule_id, text(want, "capsule_id"), "{name}");
        assert_eq!(
            String::from_utf8(sealed.payload.clone()).unwrap(),
            text(want, "capsule_jcs"),
            "{name}"
        );
        assert_eq!(
            hex::encode(&sealed.envelope),
            text(want, "envelope_hex"),
            "{name}"
        );
        verify_envelope(&sealed.capsule_id, &sealed.envelope).unwrap();
        assert!(verify_capsule(&sealed.payload).unwrap().ok, "{name}");
    }
    assert_eq!(records.len(), 22);
}

#[test]
fn cross_check_digests_match_go() {
    let pack = cross_check();
    let digests = entries(&pack.spec, "digests");
    let expected = entries(&pack.expected, "digests");
    assert_eq!(digests.len(), expected.len());
    for (case, want) in digests.iter().zip(expected) {
        let name = text(case, "name");
        assert_eq!(name, text(want, "name"));
        assert_eq!(
            digest_json(&case["value"]).unwrap(),
            text(want, "digest"),
            "{name}"
        );
    }
}

#[test]
fn cross_check_timestamps_match_go() {
    let pack = cross_check();
    let inputs = pack.spec["timestamps"].as_array().unwrap();
    let expected = entries(&pack.expected, "timestamps");
    assert_eq!(inputs.len(), expected.len());
    for (input, want) in inputs.iter().zip(expected) {
        let input = input.as_str().unwrap();
        assert_eq!(input, text(want, "input"));
        let built = build(Input {
            action_id: "t/1".into(),
            action_type: ACTION_TYPE_FYI.into(),
            operator: "o".into(),
            developer: "d".into(),
            timestamp: timestamp(input),
            ..Input::default()
        })
        .unwrap();
        assert_eq!(
            built.value["timestamp"],
            Value::from(text(want, "wire")),
            "{input}"
        );
    }
}

/// Text up to the first ": ", the part of an error both implementations
/// author.
fn context(error: &str) -> &str {
    error.split(": ").next().unwrap()
}

#[test]
fn cross_check_rejections_match_go() {
    let pack = cross_check();
    let prior = replay_records(&pack);
    let rejections = entries(&pack.spec, "rejections");
    let expected = entries(&pack.expected, "rejections");
    assert_eq!(rejections.len(), expected.len());
    let mut differing = Vec::new();
    for (record, want) in rejections.iter().zip(expected) {
        let name = text(record, "name");
        assert_eq!(name, text(want, "name"));
        let go = text(want, "error");
        let error = match run_record(record, &pack.identity, &prior) {
            Ok(_) => panic!("{name}: Go refused ({go}); Rust did not"),
            Err(e) => e.to_string(),
        };
        if error != go {
            assert_eq!(context(&error), context(&go), "{name}: {error}");
            differing.push(name);
        }
    }
    assert_eq!(differing, DECODER_TEXT_DIFFERS);
}
