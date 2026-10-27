//! Producing format-4 Capsules: the calls the Go module and the TypeScript
//! package offer, with the same outputs byte for byte.
//!
//! ```text
//! JSON values -> digest_json -> agent input/output digests -> build -> Capsule
//! foreign bytes ------------------------> carry / received ----------> Capsule
//! existing Capsules -> who / can / did / audit -> build_composition -> Capsule
//!
//! Capsule + SigningIdentity -> sign -> Producer Envelope
//! SealInput -> digest_json + build/build_composition + sign -> Capsule + Envelope
//! ```
//!
//! Nothing here writes anything: there is no ledger, store or clock. The
//! caller supplies action ids and timestamps and keeps what it gets back.
//! Canonicalization, the Capsule ID, Class 1 verification and Producer
//! Envelope verification come from the agent-action-capsule crate.
//!
//! This module is separate from [`crate::capsule`], whose `seal` builds the
//! exchange records the ledger keeps. Invariants (shared with Go's
//! `go/CLAUDE.md`):
//!
//! - Format 4 only, `spec_version` -05, `canonicalization_id` `jcs`.
//! - Action types are `fyi` and `decide` only.
//! - Assurance is derived from the effect and chain, never supplied.
//! - [`carry`] and [`received`] digest the exact bytes and never parse them.
//! - Authored input or output digests cannot be combined with a carried
//!   artifact or composed members.
//! - A Producer Envelope's protected header is exactly content type, raw
//!   Ed25519 `kid` and EdDSA `alg`, in the byte order the cross-runtime
//!   corpus froze; its unprotected map is empty.

mod build;
mod constants;
mod construction;
mod model;
mod sign;
mod verify;

pub use build::build;
pub use constants::*;
pub use construction::{
    audit, build_composition, can, carry, did, received, who, Composable, SlotMember,
};
pub use model::{
    BuiltPayload, Chain, ComputeAttestation, Disposition, Effect, Extension, Input, Model,
    ProvenanceMode, Reference, SealInput, Sealed,
};
pub use sign::{seal, sign, EnvelopeSigner, SigningIdentity};
pub use verify::{decode_payload, verify_capsule, verify_envelope};

use agent_action_capsule::canonical;
use serde_json::Value;

/// Why a producing or verifying call refused.
#[derive(Debug, thiserror::Error)]
pub enum ProduceError {
    /// The input breaks a rule this module enforces, or JSON could not be
    /// decoded or canonicalized; the text says which.
    #[error("{0}")]
    Invalid(String),
    /// A Capsule failed AAC Class 1 verification.
    #[error(transparent)]
    Class1(Class1Error),
    /// A Capsule this module built failed AAC Class 1 verification, such as
    /// a backfilled `witnessed` time rung with no corroborating reference.
    #[error("built payload failed AAC Class 1: {0}")]
    BuiltClass1(Class1Error),
    /// A Producer Envelope did not verify.
    #[error(transparent)]
    Envelope(EnvelopeError),
}

impl ProduceError {
    fn invalid(text: impl Into<String>) -> Self {
        Self::Invalid(text.into())
    }
}

/// Structured AAC Class 1 findings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Class1Error {
    pub findings: Vec<agent_action_capsule::verify::Finding>,
}

impl std::fmt::Display for Class1Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let parts: Vec<String> = self
            .findings
            .iter()
            .map(|finding| {
                let check = finding
                    .check
                    .map_or_else(|| "none".to_string(), |check| check.to_string());
                format!(
                    "check={check} severity={} code={} detail={}",
                    finding.severity, finding.code, finding.detail
                )
            })
            .collect();
        write!(f, "AAC Class 1 verification failed: {}", parts.join("; "))
    }
}

impl std::error::Error for Class1Error {}

/// Producer Envelope verification findings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnvelopeError {
    pub findings: Vec<agent_action_capsule::envelope::Finding>,
}

impl std::fmt::Display for EnvelopeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let parts: Vec<String> = self
            .findings
            .iter()
            .map(|finding| format!("{}: {}", finding.code, finding.detail))
            .collect();
        write!(
            f,
            "Producer Envelope verification failed: {}",
            parts.join("; ")
        )
    }
}

impl std::error::Error for EnvelopeError {}

/// The AAC JSON-DIGEST of `value`: lowercase hex SHA-256 over its RFC 8785
/// JCS form. `value` is serialized with `serde_json` and decoded strictly
/// again, so floats, integers beyond +/-2^53-1 and duplicate member names are
/// refused, and negative zero is written as zero.
pub fn digest_json<T: serde::Serialize + ?Sized>(value: &T) -> Result<String, ProduceError> {
    let decoded = marshal_digest_value(value)?;
    canonical::json_digest(&decoded)
        .map_err(|e| ProduceError::invalid(format!("canonicalize JSON digest input: {e}")))
}

fn marshal_digest_value<T: serde::Serialize + ?Sized>(value: &T) -> Result<Value, ProduceError> {
    let encoded = serde_json::to_vec(value)
        .map_err(|e| ProduceError::invalid(format!("marshal JSON digest input: {e}")))?;
    decode_strict_json(&encoded)
        .map_err(|e| ProduceError::invalid(format!("decode JSON digest input: {e}")))
}

/// Strict JSON decoding as Go's `decodeStrictJSON`: UTF-8, one value, numbers
/// kept as written, and no duplicate member names. Nesting is bounded at 128
/// levels by the agent-action-capsule decoder.
pub(crate) fn decode_strict_json(data: &[u8]) -> Result<Value, String> {
    let value =
        agent_action_capsule::verify::decode_capsule_json(data).map_err(|e| e.to_string())?;
    if let Some(key) = duplicate_key(data) {
        return Err(format!("duplicate object key {key:?}"));
    }
    Ok(value)
}

/// The first duplicated object member name in `data`, which must already be
/// known to be valid JSON. The agent-action-capsule decoder keeps the last of
/// two equal names; the Go producer refuses them, so this module does too.
fn duplicate_key(data: &[u8]) -> Option<String> {
    use serde::de::{DeserializeSeed, Deserializer, Error, MapAccess, SeqAccess, Visitor};
    use std::cell::RefCell;
    use std::collections::HashSet;

    struct Walk<'a>(&'a RefCell<Option<String>>);

    impl<'de> DeserializeSeed<'de> for Walk<'_> {
        type Value = ();
        fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
            deserializer.deserialize_any(self)
        }
    }

    impl<'de> Visitor<'de> for Walk<'_> {
        type Value = ();
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("JSON")
        }
        fn visit_bool<E>(self, _: bool) -> Result<(), E> {
            Ok(())
        }
        fn visit_i64<E>(self, _: i64) -> Result<(), E> {
            Ok(())
        }
        fn visit_u64<E>(self, _: u64) -> Result<(), E> {
            Ok(())
        }
        fn visit_f64<E>(self, _: f64) -> Result<(), E> {
            Ok(())
        }
        fn visit_str<E>(self, _: &str) -> Result<(), E> {
            Ok(())
        }
        fn visit_unit<E>(self) -> Result<(), E> {
            Ok(())
        }
        fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
            while seq.next_element_seed(Walk(self.0))?.is_some() {}
            Ok(())
        }
        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
            let mut seen = HashSet::new();
            while let Some(key) = map.next_key::<String>()? {
                if !seen.insert(key.clone()) {
                    *self.0.borrow_mut() = Some(key);
                    return Err(A::Error::custom("duplicate object key"));
                }
                map.next_value_seed(Walk(self.0))?;
            }
            Ok(())
        }
    }

    let found = RefCell::new(None);
    let mut deserializer = serde_json::Deserializer::from_slice(data);
    let _ = Walk(&found).deserialize(&mut deserializer);
    found.into_inner()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn duplicate_names_are_found_at_any_depth() {
        assert_eq!(
            duplicate_key(br#"{"a":1,"b":[{"c":1,"c":2}]}"#),
            Some("c".to_string())
        );
        assert_eq!(duplicate_key(br#"{"a":{"a":1},"b":{"a":2}}"#), None);
        assert_eq!(duplicate_key(br#"[1,"x",null,true,-0,1.5]"#), None);
        assert!(decode_strict_json(br#"{"a":1,"a":2}"#)
            .unwrap_err()
            .contains("duplicate object key \"a\""));
    }

    #[test]
    fn digest_json_matches_the_canonical_digest_and_refuses_floats() {
        assert_eq!(
            digest_json(&json!({"b": 1, "a": "x"})).unwrap(),
            canonical::json_digest(&json!({"a": "x", "b": 1})).unwrap()
        );
        assert!(digest_json(&1.5f64)
            .unwrap_err()
            .to_string()
            .starts_with("canonicalize JSON digest input"));
        let mut not_json = std::collections::HashMap::new();
        not_json.insert(vec![1u8], 1);
        assert!(digest_json(&not_json)
            .unwrap_err()
            .to_string()
            .starts_with("marshal JSON digest input"));
    }

    #[test]
    fn error_texts_list_every_finding() {
        let class1 = Class1Error {
            findings: vec![agent_action_capsule::verify::Finding {
                code: "c".into(),
                detail: "d".into(),
                severity: "error".into(),
                check: None,
            }],
        };
        assert_eq!(
            class1.to_string(),
            "AAC Class 1 verification failed: check=none severity=error code=c detail=d"
        );
        let envelope = EnvelopeError {
            findings: vec![agent_action_capsule::envelope::Finding {
                code: "x".into(),
                detail: "y".into(),
            }],
        };
        assert_eq!(
            envelope.to_string(),
            "Producer Envelope verification failed: x: y"
        );
    }
}
