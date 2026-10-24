//! A member named after `serde_json`'s private number token,
//! `"$serde_json::private::Number"`, is an ordinary object member to the Go,
//! Python and TypeScript references. With `serde_json`'s
//! `arbitrary_precision` feature on (this crate's canonicalizer turns it on),
//! `serde_json`'s own decoder reads such an object as a number, or refuses
//! it, so a record carrying it would get a different `capsule_id` in Rust or
//! wedge the ledger. This test holds every path that reads a record (ledger
//! recovery and lookup, offline verification, the checkpoint fold) to the
//! references' answer.
//!
//! The `"1"` case is the reference-parity case `number-token-object` in the
//! action-state-group/agent-action-capsule repository
//! (`rust/agent-action-capsule/tests/reference_parity.rs` there); both ids were
//! also recomputed with Python's `json` and RFC 8785 key order.

use capsule_emit::anchor::AnchorClient;
use capsule_emit::capsule::payload_bytes;
use capsule_emit::checkpoint::{CheckpointCadenceConfig, CheckpointState};
use capsule_emit::cose::{build_signed_statement, SignedStatementInput};
use capsule_emit::jcs::{compute_capsule_id, decode_record};
use capsule_emit::ledger::Ledger;
use capsule_emit::verify::verify_offline;
use ed25519_dalek::SigningKey;
use serde_json::Value;

/// A capsule whose `extension` member is `{"$serde_json::private::Number": <token>}`.
fn capsule_text(token: &str, capsule_id: &str) -> String {
    format!(
        r#"{{"spec_version":"draft-mih-scitt-agent-action-capsule-04","format_version":"4","canonicalization_id":"jcs","action_id":"rust-parity","action_type":"fyi","operator":"example-org","developer":"example-agent@v1","timestamp":"2026-10-14T00:00:00Z","extension":{{"$serde_json::private::Number":"{token}"}},"capsule_id":"{capsule_id}"}}"#
    )
}

const CASES: &[(&str, &str)] = &[
    (
        "1",
        "b44025f9171417a722a0cef88fd9a4b3c37bcedc6df592826942528b047325c6",
    ),
    (
        "zz",
        "3140978243a90ee10c19693bcb47bff4aaf81716326ce60d0cffb884e1f18508",
    ),
];

fn is_token_object(capsule: &Value) -> bool {
    capsule["extension"]
        .as_object()
        .is_some_and(|m| m.contains_key("$serde_json::private::Number"))
}

#[test]
fn serde_json_would_misread_the_member() {
    // Not a property of this crate: the hazard the rest of this file guards.
    let (one, zz) = (capsule_text("1", "x"), capsule_text("zz", "x"));
    let one: Value = serde_json::from_str(&one).unwrap();
    assert!(one["extension"].is_number());
    assert!(serde_json::from_str::<Value>(&zz).is_err());
}

#[test]
fn the_member_gets_the_references_capsule_id_on_every_read_path() {
    let key = SigningKey::from_bytes(&[7u8; 32]);
    for (token, capsule_id) in CASES {
        let text = capsule_text(token, capsule_id);
        let capsule = decode_record(text.as_bytes()).unwrap();
        assert!(is_token_object(&capsule), "{token}");
        assert_eq!(
            compute_capsule_id(&capsule).unwrap(),
            *capsule_id,
            "{token}"
        );

        let dir = tempfile::tempdir().unwrap();
        let statement = build_signed_statement(
            &SignedStatementInput {
                payload: &payload_bytes(&capsule),
                issuer: "number-token-test",
                subject: capsule_id,
                content_type: "application/vnd.agent-action-capsule+json",
            },
            &key,
        );
        Ledger::open(dir.path())
            .unwrap()
            .0
            .append(&capsule, &statement)
            .unwrap();

        // Reopening recomputes every stored line's capsule_id.
        let (ledger, _) = Ledger::open(dir.path()).unwrap();
        let entry = ledger.lookup(capsule_id).unwrap().expect("indexed");
        assert_eq!(entry.capsule, capsule, "{token}");
        let report = verify_offline(&entry.capsule, &statement, &key.verifying_key(), None);
        assert!(report.ok(), "{token}: {:?}", report.findings);

        let (mut state, _) = CheckpointState::load(
            dir.path(),
            "number-token-log",
            CheckpointCadenceConfig::default(),
        )
        .unwrap();
        state
            .reconnect(&key, &AnchorClient::new("http://127.0.0.1:1"))
            .unwrap()
            .expect("a checkpoint over the one record");
        let coverage = state
            .existing_coverage(capsule_id)
            .unwrap()
            .expect("covered");
        assert_eq!(coverage.leaf_index, 0, "{token}");
    }
}
