//! The draft's settlement-records conformance vectors, reproduced by this
//! crate's producer (`test-vectors/settlement-records/cases.json`, vendored
//! byte for byte from agent-action-capsule; see its README).
//!
//! The vectors carry finished legs, so for every record of every case this
//! test checks that the Rust producer makes the same thing:
//!
//! 1. `build_leg` given the vector leg's members returns a `settlement`
//!    member JCS-equal to the vector's, or refuses with exactly the structural
//!    failure codes the case expects for that record;
//! 2. `capsule_id` recomputes from the vector capsule with this crate's JCS
//!    (and is refused where the vector has none: a JSON float);
//! 3. signing that `capsule_id` with the published seed for the record's key
//!    reproduces the vector's producer envelope byte for byte.
//!
//! Deriving settlement states is out of scope here (producer side only); the
//! Python reference runs the same vectors for that.

use capsule_emit::cose::sign_producer_envelope;
use capsule_emit::jcs::compute_capsule_id;
use capsule_emit::settlement::{build_leg, structure_failures};
use ed25519_dalek::SigningKey;
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;

const STRUCTURAL: &[&str] = &[
    "settlement_malformed",
    "amount_not_exact",
    "leg_role_mismatch",
];

fn vectors() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../test-vectors/settlement-records/cases.json");
    serde_json::from_slice(&std::fs::read(&path).expect("vendored vectors present")).unwrap()
}

fn canonical(v: &Value) -> Vec<u8> {
    capsule_emit::jcs::jcs(v).expect("JCS of a leg")
}

/// The structural failure codes the case expects for one record, in order.
fn expected_structural(case: &Value, label: &str) -> Vec<String> {
    case["expect"]["failures"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|f| f["records"] == serde_json::json!([label]))
        .filter_map(|f| f["code"].as_str())
        .filter(|c| STRUCTURAL.contains(c))
        .map(String::from)
        .collect()
}

#[test]
fn the_rust_producer_reproduces_every_vector_record() {
    let doc = vectors();
    let seeds: BTreeMap<String, String> = doc["keys"]
        .as_object()
        .unwrap()
        .values()
        .map(|k| {
            (
                k["public_key_hex"].as_str().unwrap().to_string(),
                k["seed_hex"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let (mut legs, mut refused, mut ids, mut envelopes) = (0, 0, 0, 0);
    let (mut records, mut with_id, mut with_envelope) = (0, 0, 0);
    for case in doc["cases"].as_array().unwrap() {
        let id = case["id"].as_str().unwrap();
        for record in case["records"].as_array().unwrap() {
            let label = record["label"].as_str().unwrap();
            records += 1;
            with_id += usize::from(record["capsule_id"].is_string());
            with_envelope += usize::from(record["envelope_hex"].is_string());
            let capsule = &record["capsule"];
            let member = &capsule["settlement"];

            // 1. the leg
            let expected = expected_structural(case, label);
            let got: Vec<String> = structure_failures(member)
                .iter()
                .map(|c| c.to_string())
                .collect();
            assert_eq!(got, expected, "{id}/{label}: structural failure codes");
            let mut members: Map<String, Value> = member.as_object().unwrap().clone();
            let leg = members.remove("leg").unwrap();
            let role = members.remove("sealer_role").unwrap();
            members.remove("version");
            match build_leg(leg.as_str().unwrap(), role.as_str().unwrap(), members) {
                Ok(built) => {
                    assert!(
                        expected.is_empty(),
                        "{id}/{label}: built a leg the vector refuses"
                    );
                    assert_eq!(
                        canonical(&built),
                        canonical(member),
                        "{id}/{label}: leg differs"
                    );
                    legs += 1;
                }
                Err(err) => {
                    let codes: Vec<String> = err.codes.iter().map(|c| c.to_string()).collect();
                    assert_eq!(codes, expected, "{id}/{label}: refusal codes");
                    refused += 1;
                }
            }

            // 2. the capsule_id
            let mut body = capsule.clone();
            body.as_object_mut().unwrap().remove("capsule_id");
            match record["capsule_id"].as_str() {
                Some(want) => {
                    assert_eq!(
                        compute_capsule_id(&body).unwrap(),
                        want,
                        "{id}/{label}: capsule_id"
                    );
                    ids += 1;
                }
                None => assert!(
                    compute_capsule_id(&body).is_err(),
                    "{id}/{label}: an id for a float"
                ),
            }

            // 3. the producer envelope
            if let (Some(hex_env), Some(kid), Some(cid)) = (
                record["envelope_hex"].as_str(),
                record["envelope_kid"].as_str(),
                record["capsule_id"].as_str(),
            ) {
                let seed: [u8; 32] = hex::decode(&seeds[kid]).unwrap().try_into().unwrap();
                let key = SigningKey::from_bytes(&seed);
                let envelope = sign_producer_envelope(&hex::decode(cid).unwrap(), &key);
                assert_eq!(
                    hex::encode(envelope),
                    hex_env,
                    "{id}/{label}: producer envelope"
                );
                envelopes += 1;
            }
        }
    }
    eprintln!("legs built {legs}, refused {refused}, ids {ids}, envelopes {envelopes}");
    // Every record went through every check that applies to it, and the set
    // is the vendored one (19 cases, 67 records).
    assert_eq!((legs + refused, records), (67, 67));
    assert_eq!((ids, envelopes), (with_id, with_envelope));
    assert!(refused > 0, "the negative cases were exercised");
}
