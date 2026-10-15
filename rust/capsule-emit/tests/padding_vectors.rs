//! This crate's padding records against the agent-action-capsule Evidence
//! Layer padding vectors (`tests/vectors/aac-evidence-layer-padding/`, see
//! `tests/vectors/SOURCES.md`).
//!
//! A positive case passes `check_padding_shape`, its `capsule_id` recomputes
//! from the case's literal preimage, and `padding_record_with_nonce` rebuilds
//! it member for member from its nonce. A negative case is refused for the
//! case's reason: by the shape check, naming the case's member, or, for
//! `capsule_id_mismatch`, by recomputing the id (the shape check leaves ids
//! to the ledger's reload check, which recomputes every line's).

use capsule_emit::jcs::{compute_capsule_id, decode_record, jcs};
use capsule_emit::padding::{check_padding_shape, is_padding, padding_record_with_nonce};
use serde_json::{Map, Value};
use std::path::Path;

fn cases() -> Vec<Value> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/vectors/aac-evidence-layer-padding/cases.json");
    let set = decode_record(&std::fs::read(path).expect("vendored padding vectors")).unwrap();
    assert_eq!(set["draft"], "draft-mih-agent-evidence-layer-00");
    let cases = set["cases"].as_array().expect("cases").clone();
    assert_eq!(Some(cases.len() as u64), set["count"].as_u64());
    cases
}

fn preimage(record: &Value) -> Value {
    let body: Map<String, Value> = record
        .as_object()
        .unwrap()
        .iter()
        .filter(|(k, _)| *k != "capsule_id")
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    Value::Object(body)
}

#[test]
fn padding_records_match_the_aac_vectors() {
    let (mut positive, mut negative) = (0, 0);
    for case in cases() {
        let id = case["id"].as_str().unwrap();
        let record = &case["record"];
        assert!(is_padding(record), "{id}");
        assert_eq!(
            jcs(&preimage(record)).unwrap(),
            case["capsule_id_preimage"].as_str().unwrap().as_bytes(),
            "{id}: preimage"
        );
        let carried = record["capsule_id"].as_str().unwrap();
        let id_ok = compute_capsule_id(record).unwrap() == carried;
        let shape = check_padding_shape(record);

        match case["expected"]["valid"].as_bool() {
            Some(true) => {
                positive += 1;
                shape.unwrap_or_else(|e| panic!("{id}: {e}"));
                assert!(id_ok, "{id}: capsule_id");
                let nonce = record["store_nonce"].as_str().unwrap();
                assert_eq!(&padding_record_with_nonce(nonce).unwrap(), record, "{id}");
            }
            Some(false) => {
                negative += 1;
                let reason = case["expected"]["reason"].as_str().unwrap();
                let member = case["expected"]["member"].as_str().unwrap();
                if reason == "capsule_id_mismatch" {
                    shape.unwrap_or_else(|e| panic!("{id}: shape is not the defect: {e}"));
                    assert!(!id_ok, "{id}: capsule_id must not recompute");
                } else {
                    let why = shape.expect_err(id);
                    assert!(why.contains(member), "{id}: {why:?} names {member}");
                }
            }
            None => panic!("{id}: expected.valid"),
        }
    }
    assert_eq!((positive, negative), (3, 14));
}

/// The producer envelope (`signature`, `key_id`) sits beside the record,
/// outside its id: a positive case with it attached still passes.
#[test]
fn the_producer_envelope_is_outside_the_record() {
    let case = cases().into_iter().next().unwrap();
    let mut record = case["record"].clone();
    record["signature"] = Value::String("00".repeat(64));
    record["key_id"] = Value::String("11".repeat(32));
    check_padding_shape(&record).unwrap();
    assert_eq!(
        compute_capsule_id(&record).unwrap(),
        record["capsule_id"].as_str().unwrap()
    );
}
