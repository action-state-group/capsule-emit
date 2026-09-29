//! The draft-mih-agent-evidence-request-00 conformance vectors
//! (`tests/vectors/evidence-request/`, pinned in `tests/vectors/SOURCES.md`),
//! every corpus, every case. The pinned files are checked against the
//! source's own `SHA256SUMS` before any case is read.

use capsule_emit_evidence_request::digest::{identifies_request, request_digest};
use capsule_emit_evidence_request::invariance::{compare, Observation};
use capsule_emit_evidence_request::outcome::{record, Received, Window};
use capsule_emit_evidence_request::refusal::{check, check_cbor, sign, verify_for, VerifyError};
use capsule_emit_evidence_request::registry::{Reason, SubjectForm, SUBPROTOCOL};
use capsule_emit_evidence_request::request::{parse_cbor, parse_json, Derivation};
use capsule_emit_evidence_request::resolve::{resolve, Anchor, Resolution, Responder};
use capsule_emit_evidence_request::retention::{
    classify_absence, classify_refusal, commitment_well_formed, Commitment,
};
use ed25519_dalek::SigningKey;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::PathBuf;

fn dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/vectors/evidence-request")
}

/// Every corpus goes through here, and this checks every pinned file
/// against the source's `SHA256SUMS` before returning anything.
fn load(name: &str) -> Value {
    verify_pinned_checksums();
    let bytes = std::fs::read(dir().join(name)).unwrap_or_else(|e| panic!("read {name}: {e}"));
    serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("parse {name}: {e}"))
}

fn cases(corpus: &Value) -> &Vec<Value> {
    let cases = corpus["cases"].as_array().expect("cases");
    assert_eq!(
        corpus["count"].as_u64(),
        Some(cases.len() as u64),
        "case count"
    );
    cases
}

fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key]
        .as_str()
        .unwrap_or_else(|| panic!("{key} is not text in {v}"))
}

#[test]
fn pinned_files_match_the_source_checksums() {
    verify_pinned_checksums();
}

fn verify_pinned_checksums() {
    let sums = std::fs::read_to_string(dir().join("SHA256SUMS")).expect("SHA256SUMS");
    let mut checked = 0;
    for line in sums.lines().filter(|l| !l.trim().is_empty()) {
        let (want, name) = line.split_once("  ").expect("sha256sum line");
        let got = hex::encode(Sha256::digest(
            std::fs::read(dir().join(name.trim())).expect("pinned file"),
        ));
        assert_eq!(got, want, "{name} does not match its pinned checksum");
        checked += 1;
    }
    assert!(
        checked >= 12,
        "expected every vector file listed, found {checked}"
    );
}

#[test]
fn registry_strings_match() {
    let registry = load("registry.json");
    let reasons: Vec<&str> = registry["refusal_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(reasons, Reason::ALL.map(Reason::token));
    let forms: Vec<&str> = registry["subject_forms"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(forms, SubjectForm::ALL.map(SubjectForm::token));
    assert_eq!(s(&registry, "subprotocol"), SUBPROTOCOL);
    for unregistered in registry["not_registered_examples"].as_array().unwrap() {
        let token = unregistered.as_str().unwrap();
        assert_eq!(Reason::from_token(token), None, "{token}");
        assert_eq!(SubjectForm::from_token(token), None, "{token}");
    }
}

#[test]
fn request_corpus() {
    let corpus = load("request.json");
    for case in cases(&corpus) {
        let id = s(case, "id");
        let json_bytes = s(case, "request_json").as_bytes();
        assert_eq!(
            request_digest(json_bytes),
            s(case, "request_json_digest"),
            "{id}: JSON digest"
        );
        let mut results = vec![("json", parse_json(json_bytes))];
        if let Some(cbor_hex) = case["request_cbor_hex"].as_str() {
            let cbor = hex::decode(cbor_hex).unwrap();
            assert_eq!(
                request_digest(&cbor),
                s(case, "request_cbor_digest"),
                "{id}: CBOR digest"
            );
            results.push(("cbor", parse_cbor(&cbor)));
        }
        let expect = &case["expect"];
        for (binding, result) in results {
            if expect["well_formed"] == Value::Bool(true) {
                assert!(
                    result.is_ok(),
                    "{id} ({binding}): expected well formed, got {result:?}"
                );
            } else {
                let err = result.expect_err(&format!("{id} ({binding}): expected a refusal"));
                assert_eq!(
                    err.reason().token(),
                    s(expect, "reason"),
                    "{id} ({binding})"
                );
            }
        }
    }
}

/// The declared responder state of `resolution.json`, with a case's
/// overrides applied.
struct Declared(Value);

impl Declared {
    fn texts(&self, key: &str) -> Vec<&str> {
        self.0[key]
            .as_array()
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default()
    }
}

impl Responder for Declared {
    fn holds_record(&self, digest: &str) -> bool {
        self.texts("records").contains(&digest)
    }
    fn holds_correlation(&self, id: &str) -> bool {
        self.0["correlations"]
            .as_object()
            .is_some_and(|m| m.contains_key(id))
    }
    fn cites_exchange(&self, half: &str) -> bool {
        self.0["exchange_citations"]
            .as_object()
            .is_some_and(|m| m.contains_key(half))
    }
    fn positional_ordering(&self) -> bool {
        self.0["positional_ordering"].as_bool().unwrap()
    }
    fn anchors(&self) -> Vec<Anchor> {
        self.0["anchors"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| Anchor {
                digest: s(a, "digest").into(),
                size: a["size"].as_u64().unwrap(),
                issued_at: s(a, "issued_at").into(),
            })
            .collect()
    }
    fn derivation_forms(&self, derivation: &Derivation) -> Option<Vec<SubjectForm>> {
        let Derivation::Token(token) = derivation else {
            return None;
        };
        let forms = self.0["supported_derivations"].get(token)?.as_array()?;
        Some(
            forms
                .iter()
                .map(|f| SubjectForm::from_token(f.as_str().unwrap()).unwrap())
                .collect(),
        )
    }
    fn uniform_policy_declined(&self) -> bool {
        self.0["uniform_policy_declined"].as_bool().unwrap()
    }
}

#[test]
fn resolution_corpus() {
    let corpus = load("resolution.json");
    for case in cases(&corpus) {
        let id = s(case, "id");
        let mut state = corpus["responder"].clone();
        if let Some(overrides) = case["responder_overrides"].as_object() {
            for (k, v) in overrides {
                state[k] = v.clone();
            }
        }
        let responder = Declared(state);
        let bytes = s(case, "request_json").as_bytes();
        let answer = match parse_json(bytes) {
            Ok(request) => resolve(&request, &responder),
            Err(e) => Resolution::Refuse(e.reason()),
        };
        let expect = &case["expect"];
        match s(expect, "answer") {
            "artifact" => assert!(
                matches!(answer, Resolution::Artifact(_)),
                "{id}: expected an artifact, got {answer:?}"
            ),
            "refusal" => assert_eq!(
                answer,
                Resolution::Refuse(Reason::from_token(s(expect, "reason")).unwrap()),
                "{id}"
            ),
            other => panic!("{id}: unknown answer {other}"),
        }
        // Resolution is decided from the request and the holdings alone:
        // the CBOR binding of the same request resolves the same way.
        if let Some(cbor_hex) = case["request_cbor_hex"].as_str() {
            let cbor_answer = match parse_cbor(&hex::decode(cbor_hex).unwrap()) {
                Ok(request) => resolve(&request, &responder),
                Err(e) => Resolution::Refuse(e.reason()),
            };
            assert_eq!(
                cbor_answer, answer,
                "{id}: CBOR binding resolves differently"
            );
        }
    }
}

#[test]
fn digest_corpus() {
    let corpus = load("digest.json");
    for case in cases(&corpus) {
        let id = s(case, "id");
        let bytes = match case["request_cbor_hex"].as_str() {
            Some(h) => hex::decode(h).unwrap(),
            None => s(case, "request_json").as_bytes().to_vec(),
        };
        let expect = &case["expect"];
        if let Some(want) = expect["request_digest"].as_str() {
            assert_eq!(request_digest(&bytes), want, "{id}");
        }
        if let Some(identifies) = expect["identifies_request"].as_bool() {
            assert_eq!(
                identifies_request(s(case, "refusal_request_digest"), &bytes),
                identifies,
                "{id}"
            );
        }
    }
}

#[test]
fn refusal_corpus() {
    let corpus = load("refusal.json");
    let primary = &corpus["keys"]["primary"];
    let seed: [u8; 32] = hex::decode(s(primary, "seed_hex"))
        .unwrap()
        .try_into()
        .unwrap();
    let key = SigningKey::from_bytes(&seed);
    assert_eq!(
        hex::encode(key.verifying_key().to_bytes()),
        s(primary, "public_key_hex")
    );

    for case in cases(&corpus) {
        let id = s(case, "id");
        let expect = &case["expect"];
        let result = if let Some(file) = case["file"].as_str() {
            let bytes = std::fs::read(dir().join(file)).unwrap();
            assert_eq!(
                hex::encode(Sha256::digest(&bytes)),
                s(case, "file_sha256"),
                "{id}: file digest"
            );
            check(&serde_json::from_slice(&bytes).unwrap())
        } else if let Some(cbor_hex) = case["refusal_cbor_hex"].as_str() {
            check_cbor(&hex::decode(cbor_hex).unwrap())
        } else {
            check(&case["refusal"])
        };
        assert_eq!(
            result.signature_valid,
            s(expect, "signature") == "valid",
            "{id}: signature"
        );
        assert_eq!(
            result.reason_registered,
            expect["reason_registered"].as_bool().unwrap(),
            "{id}: registry"
        );
        assert_eq!(
            result.conformant,
            expect["conformant"].as_bool().unwrap(),
            "{id}: conformant"
        );

        let r = &case["refusal"];
        // Authentication: a conforming refusal is accepted only for the key
        // and request it names, and never for the other fixed key.
        if case["refusal"].is_object() {
            let other = &corpus["keys"]["other"];
            let other_key: [u8; 32] = hex::decode(s(other, "public_key_hex"))
                .unwrap()
                .try_into()
                .unwrap();
            let other_key = ed25519_dalek::VerifyingKey::from_bytes(&other_key).unwrap();
            if expect["conformant"] == Value::Bool(true) && r["key_id"] == primary["public_key_hex"]
            {
                let digest = s(r, "request_digest");
                assert!(
                    verify_for(r, &key.verifying_key(), digest).is_ok(),
                    "{id}: verify_for"
                );
                assert_eq!(
                    verify_for(r, &other_key, digest),
                    Err(VerifyError::WrongKey),
                    "{id}"
                );
                assert_eq!(
                    verify_for(r, &key.verifying_key(), &"0".repeat(64)),
                    Err(VerifyError::WrongRequest),
                    "{id}"
                );
            } else if expect["conformant"] == Value::Bool(false) {
                assert!(
                    verify_for(r, &key.verifying_key(), s(r, "request_digest")).is_err(),
                    "{id}: accepted a non-conforming refusal"
                );
            }
        }

        // Every conforming refusal the primary key signed is reproduced byte
        // for byte by `sign` (Ed25519 is deterministic).
        if expect["conformant"] == Value::Bool(true) && r["key_id"] == primary["public_key_hex"] {
            let ours = sign(
                s(r, "request_digest"),
                Reason::from_token(s(r, "reason")).unwrap(),
                s(r, "issued_at"),
                &key,
            )
            .unwrap();
            assert_eq!(ours.sig, s(r, "sig"), "{id}: signature bytes");
            if let Some(body) = case["signing_body"].as_str() {
                let object = ours.to_json();
                let signed = capsule_emit_evidence_request::refusal::signing_body(
                    object.as_object().unwrap(),
                )
                .unwrap();
                assert_eq!(signed, body, "{id}: signing body");
            }
        }
    }
}

#[test]
fn outcome_corpus() {
    let corpus = load("outcomes.json");
    for case in cases(&corpus) {
        let id = s(case, "id");
        let facts = &case["facts"];
        let received = match s(facts, "received") {
            "artifact" => Received::Artifact {
                verified: s(facts, "artifact_verification") == "verified",
            },
            "refusal" => Received::Refusal {
                signature_valid: s(facts, "refusal_signature") == "valid",
                reason: s(facts, "refusal_reason").into(),
            },
            "nothing" => Received::Nothing,
            "transport_error" => Received::TransportError,
            "subprotocol_not_offered" => Received::SubprotocolNotOffered,
            other => panic!("{id}: unknown received {other}"),
        };
        let window = match s(facts, "window") {
            "open" => Window::Open,
            "closed" => Window::Closed,
            other => panic!("{id}: unknown window {other}"),
        };
        let state = record(&received, window);
        assert_eq!(state.token(), s(&case["expect"], "state"), "{id}");
        for never in case["expect"]["must_not_record_as"].as_array().unwrap() {
            assert_ne!(state.token(), never.as_str().unwrap(), "{id}");
        }
    }
}

#[test]
fn invariance_corpus() {
    let corpus = load("invariance.json");
    for case in cases(&corpus) {
        let id = s(case, "id");
        let artifacts: Vec<(String, Vec<u8>)> = case["observations"]
            .as_array()
            .unwrap()
            .iter()
            .map(|o| {
                (
                    s(o, "resolved_anchor").to_string(),
                    hex::decode(s(o, "artifact_hex")).unwrap(),
                )
            })
            .collect();
        let observations: Vec<Observation<'_>> = artifacts
            .iter()
            .map(|(a, bytes)| Observation {
                resolved_anchor: a,
                artifact: bytes,
            })
            .collect();
        assert_eq!(
            compare(&observations).token(),
            s(&case["expect"], "invariance"),
            "{id}"
        );
    }
}

#[test]
fn retention_corpus() {
    let corpus = load("retention.json");
    for case in cases(&corpus) {
        let id = s(case, "id");
        let expect = &case["expect"];
        if let Some(commitment) = case.get("commitment") {
            assert_eq!(
                commitment_well_formed(commitment),
                expect["well_formed"].as_bool().unwrap(),
                "{id}"
            );
            continue;
        }
        let facts = &case["facts"];
        let commitment = match s(facts, "commitment") {
            "in_force" => Commitment::InForce,
            "lapsed" => Commitment::Lapsed,
            "none" => Commitment::None,
            other => panic!("{id}: unknown commitment {other}"),
        };
        match facts["refusal_reason"].as_str() {
            Some(reason) => {
                let class = classify_refusal(commitment, Reason::from_token(reason).unwrap());
                assert_eq!(class.token(), s(expect, "classification"), "{id}");
            }
            None => {
                assert_eq!(
                    classify_absence(commitment).token(),
                    s(expect, "classification"),
                    "{id}"
                );
                // An absence stays an absence under any commitment.
                let window = if s(facts, "window") == "closed" {
                    Window::Closed
                } else {
                    Window::Open
                };
                assert_eq!(
                    record(&Received::Nothing, window).token(),
                    s(expect, "state"),
                    "{id}"
                );
            }
        }
    }
}
