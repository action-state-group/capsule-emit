//! `capsule_emit::produce` behavior the Go-generated parity pack cannot
//! express: tampered results, external signers, and verification failures.

use std::sync::Arc;

use capsule_emit::produce::{self, *};
use chrono::{TimeZone, Utc};
use ed25519_dalek::SigningKey;
use serde_json::json;

fn input(action_id: &str) -> Input {
    Input {
        action_id: action_id.into(),
        action_type: ACTION_TYPE_FYI.into(),
        operator: "example-org".into(),
        developer: "example-agent@v1".into(),
        timestamp: Some(Utc.with_ymd_and_hms(2026, 9, 1, 12, 0, 0).unwrap()),
        ..Input::default()
    }
}

fn key(seed: u8) -> SigningKey {
    SigningKey::from_bytes(&[seed; 32])
}

fn seal_input(capsule: Input, identity: SigningIdentity) -> SealInput {
    SealInput {
        capsule,
        payload: Some(json!({"request": "test"})),
        agent_output: None,
        model: None,
        runtime: String::new(),
        members: None,
        identity,
    }
}

#[test]
fn sealed_capsules_verify_and_name_their_signer() {
    let identity = SigningIdentity::from_ed25519(key(1));
    let sealed = produce::seal(seal_input(input("a/1"), identity.clone())).unwrap();
    let class1 = verify_capsule(&sealed.payload).unwrap();
    assert!(class1.ok);
    assert_eq!(
        class1.capsule_id.as_deref(),
        Some(sealed.capsule_id.as_str())
    );
    let envelope = verify_envelope(&sealed.capsule_id, &sealed.envelope).unwrap();
    assert_eq!(envelope.public_key, identity.public_key().to_vec());
    let decoded = decode_payload(&sealed.payload).unwrap();
    assert_eq!(decoded["spec_version"], json!(SPEC_VERSION));
    assert_eq!(
        decoded["model_attestation"]["compute_attestation"]["agent_input_digest"],
        json!(digest_json(&json!({"request": "test"})).unwrap())
    );
}

#[test]
fn signing_never_changes_the_capsule() {
    let built = build(input("a/2")).unwrap();
    let before = built.clone();
    let first = sign(&built, &SigningIdentity::from_ed25519(key(1))).unwrap();
    let second = sign(&built, &SigningIdentity::from_ed25519(key(2))).unwrap();
    assert_ne!(first, second);
    assert_eq!(built, before);
    verify_envelope(&built.capsule_id, &first).unwrap();
    verify_envelope(&built.capsule_id, &second).unwrap();
}

#[test]
fn sign_refuses_a_payload_that_does_not_carry_its_id() {
    let identity = SigningIdentity::from_ed25519(key(1));
    let mut malformed = build(input("a/3")).unwrap();
    malformed.capsule_id = "ABC".into();
    assert_eq!(
        sign(&malformed, &identity).unwrap_err().to_string(),
        "Capsule ID must be 64 lowercase hexadecimal characters"
    );
    let mut swapped = build(input("a/3")).unwrap();
    swapped.capsule_id = build(input("a/4")).unwrap().capsule_id;
    assert_eq!(
        sign(&swapped, &identity).unwrap_err().to_string(),
        "built Capsule does not match Capsule ID"
    );
}

struct OtherKey(SigningKey);

impl EnvelopeSigner for OtherKey {
    fn sign_ed25519(&self, message: &[u8]) -> Result<[u8; 64], String> {
        self.0.sign_ed25519(message)
    }
}

struct Offline;

impl EnvelopeSigner for Offline {
    fn sign_ed25519(&self, _: &[u8]) -> Result<[u8; 64], String> {
        Err("signer unavailable".into())
    }
}

#[test]
fn an_external_signer_must_match_its_public_key() {
    let built = build(input("a/5")).unwrap();
    let public = key(1).verifying_key().to_bytes();

    let matching = SigningIdentity::new(Arc::new(OtherKey(key(1))), public);
    let envelope = sign(&built, &matching).unwrap();
    assert_eq!(
        envelope,
        sign(&built, &SigningIdentity::from_ed25519(key(1))).unwrap()
    );

    let mismatched = SigningIdentity::new(Arc::new(OtherKey(key(2))), public);
    assert_eq!(
        sign(&built, &mismatched).unwrap_err().to_string(),
        "Producer Envelope signer does not match public key"
    );
    let offline = SigningIdentity::new(Arc::new(Offline), public);
    assert_eq!(
        sign(&built, &offline).unwrap_err().to_string(),
        "sign Producer Envelope: signer unavailable"
    );
    let err = produce::seal(seal_input(input("a/5"), offline)).unwrap_err();
    assert_eq!(
        err.to_string(),
        "sign Producer Envelope: signer unavailable"
    );
    assert!(format!("{matching:?}").contains(&hex::encode(public)));
}

#[test]
fn composition_refuses_members_that_are_not_their_capsule() {
    let member = build(input("m/1")).unwrap();
    let mut malformed = member.clone();
    malformed.capsule_id = "xyz".into();
    assert_eq!(
        build_composition(input("c/1"), &[did(&malformed)])
            .unwrap_err()
            .to_string(),
        "composition did member has malformed Capsule ID"
    );
    let mut tampered = member.clone();
    tampered.json = build(input("m/2")).unwrap().json;
    assert_eq!(
        build_composition(input("c/1"), &[audit(&tampered)])
            .unwrap_err()
            .to_string(),
        "composition audit member is not a matching verified format-4 Capsule"
    );

    let composed = build_composition(input("c/1"), &[can(&member)]).unwrap();
    let members = &composed.value["model_attestation"]["compute_attestation"]["composed_members"];
    assert_eq!(
        members,
        &json!([{"type": "capsule", "digest_alg": "SHA-256", "digest": member.capsule_id, "slot": "can"}])
    );
}

#[test]
fn verify_capsule_refuses_other_profiles_and_failed_class1() {
    let built = build(input("v/1")).unwrap();
    let mut value = built.value.clone();
    value.insert("format_version".into(), json!("2"));
    let other = serde_json::to_vec(&value).unwrap();
    assert_eq!(
        verify_capsule(&other).unwrap_err().to_string(),
        "unsupported Capsule profile: only AAC format 4 with canonicalization_id \"jcs\" is supported"
    );

    let mut value = built.value.clone();
    value.insert("operator".into(), json!("someone-else"));
    let altered = serde_json::to_vec(&value).unwrap();
    match verify_capsule(&altered).unwrap_err() {
        ProduceError::Class1(error) => {
            assert!(error.findings.iter().any(|f| f.severity == "error"));
            assert!(error
                .to_string()
                .starts_with("AAC Class 1 verification failed: "));
        }
        other => panic!("expected Class 1 findings, got {other}"),
    }

    // spec_version selects nothing: an unrecognized value still verifies.
    let mut value = built.value.clone();
    value.insert("spec_version".into(), json!("draft-unknown-99"));
    let id =
        capsule_emit::jcs::compute_capsule_id(&serde_json::Value::Object(value.clone())).unwrap();
    value.insert("capsule_id".into(), json!(id));
    assert!(
        verify_capsule(&serde_json::to_vec(&value).unwrap())
            .unwrap()
            .ok
    );
}

#[test]
fn decode_payload_is_strict() {
    for (data, want) in [
        (&b"[1]"[..], "capsule payload is not an object"),
        (
            br#"{"a":1,"a":2}"#,
            "decode capsule payload: duplicate object key \"a\"",
        ),
    ] {
        assert_eq!(decode_payload(data).unwrap_err().to_string(), want);
    }
    for data in [&b"{} {}"[..], b"{\"a\":\xff}", b"{"] {
        assert!(decode_payload(data)
            .unwrap_err()
            .to_string()
            .starts_with("decode capsule payload: "));
    }
    let decoded = decode_payload(br#"{"n":12345678901234567890123,"x":"y"}"#).unwrap();
    assert_eq!(decoded["n"].to_string(), "12345678901234567890123");
}

#[test]
fn verify_envelope_reports_findings() {
    let identity = SigningIdentity::from_ed25519(key(3));
    let sealed = produce::seal(seal_input(input("e/1"), identity)).unwrap();
    let other = build(input("e/2")).unwrap();
    match verify_envelope(&other.capsule_id, &sealed.envelope).unwrap_err() {
        ProduceError::Envelope(error) => {
            assert_eq!(error.findings[0].code, "envelope_payload_mismatch");
            assert!(error
                .to_string()
                .starts_with("Producer Envelope verification failed: envelope_payload_mismatch: "));
        }
        other => panic!("expected envelope findings, got {other}"),
    }
    assert!(verify_envelope(&sealed.capsule_id, b"not cbor").is_err());
}

#[test]
fn constants_mirror_the_go_module() {
    assert_eq!(SPEC_VERSION, "draft-mih-scitt-agent-action-capsule-05");
    assert_eq!(FORMAT_VERSION, "4");
    assert_eq!(CANONICALIZATION_ID, "jcs");
    assert_eq!(CONTENT_TYPE, "application/agent-action-capsule-id");
    assert_eq!(
        ACCEPTED_SPEC_VERSIONS,
        ["draft-mih-scitt-agent-action-capsule-04", SPEC_VERSION]
    );
    assert!(is_v4_irreversibility_class(
        IRREVERSIBILITY_ONE_WAY_TERMINAL
    ));
    assert!(!is_v4_irreversibility_class("custom_class"));
}

fn nested_arrays(levels: usize) -> Vec<u8> {
    format!("{}0{}", "[".repeat(levels), "]".repeat(levels)).into_bytes()
}

/// A Capsule is one level deeper than its deepest extension, so the deepest
/// extension `build` accepts is the deepest whose Capsule still decodes.
#[test]
fn build_refuses_a_capsule_nested_deeper_than_verify_capsule_decodes() {
    let with_extension = |levels: usize| Input {
        extensions: vec![Extension {
            name: "note".into(),
            value: nested_arrays(levels),
        }],
        ..input("d/1")
    };
    let deepest = build(with_extension(127)).unwrap();
    assert!(verify_capsule(&deepest.json).unwrap().ok);
    sign(&deepest, &SigningIdentity::from_ed25519(key(1))).unwrap();
    assert_eq!(
        build(with_extension(128)).unwrap_err().to_string(),
        "built Capsule nests 129 levels deep; a Capsule may nest at most 128"
    );

    // Reference coordinates sit three levels down: references, the entry,
    // then log_coordinates.
    let deep = capsule_emit::jcs::decode_record(&nested_arrays(125)).unwrap();
    let mut coordinates = serde_json::Map::new();
    coordinates.insert("proof".into(), deep);
    let with_reference = Input {
        references: Some(vec![Reference {
            reference_type: "r".into(),
            digest_alg: "SHA-256".into(),
            digest: "d".into(),
            log_coordinates: Some(coordinates),
            ..Reference::default()
        }]),
        ..input("d/2")
    };
    assert_eq!(
        build(with_reference).unwrap_err().to_string(),
        "built Capsule nests 129 levels deep; a Capsule may nest at most 128"
    );
}

#[test]
fn go_zero_time_is_no_timestamp() {
    let zero = Utc.with_ymd_and_hms(1, 1, 1, 0, 0, 0).unwrap();
    let at = |timestamp| Input {
        timestamp: Some(timestamp),
        ..input("z/1")
    };
    assert_eq!(
        build(at(zero)).unwrap_err().to_string(),
        "timestamp is required"
    );
    // One nanosecond later is not Go's zero time, though it truncates to it.
    let built = build(at(zero + chrono::Duration::nanoseconds(1))).unwrap();
    assert_eq!(built.value["timestamp"], json!("0001-01-01T00:00:00Z"));
}

/// Native floats go through `digest_json` as Go marshals them; a
/// `serde_json::Value` number keeps its text, so `2.0` there is a float, as
/// Go's `json.Number("2.0")` is.
#[test]
fn integral_floats_follow_their_representation() {
    assert_eq!(
        digest_json(&[2.0f64, -0.0]).unwrap(),
        digest_json(&json!([2, 0])).unwrap()
    );
    assert!(digest_json(&json!(2.0)).is_err());

    let coordinates = |value: serde_json::Value| Input {
        references: Some(vec![Reference {
            reference_type: "r".into(),
            digest_alg: "SHA-256".into(),
            digest: "d".into(),
            log_coordinates: Some(value.as_object().unwrap().clone()),
            ..Reference::default()
        }]),
        ..input("f/2")
    };
    build(coordinates(
        json!({"log_id": "l", "leaf_index": 2, "inclusion_proof": []}),
    ))
    .unwrap();
    assert!(build(coordinates(
        json!({"log_id": "l", "leaf_index": 2.0, "inclusion_proof": []})
    ))
    .unwrap_err()
    .to_string()
    .starts_with("compute capsule id: "));
}
