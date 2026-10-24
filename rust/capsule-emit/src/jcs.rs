//! Canonicalization and JSON-DIGEST (draft-mih-scitt-agent-action-capsule-04 §2, §5.1).
//!
//! The canonicalizer is the Agent Action Capsule crate's
//! ([`agent_action_capsule::canonical`]), the Rust sibling of the Go, Python
//! and TypeScript references, so a record this crate seals gets the same
//! `capsule_id` as in those implementations. This module keeps this crate's
//! names over it.
//!
//! **Numbers.** That crate turns on `serde_json`'s `arbitrary_precision`
//! feature, for this crate and every binary that links it: a number keeps
//! its source text, so a float is any number written with `.`, `e` or `E`,
//! and an unsafe integer is one beyond +/-[`MAX_SAFE_INTEGER`] whatever its
//! size. `-0` is the integer 0 and is written `0`, as RFC 8785 and the
//! references do. Parse record JSON with [`decode_record`], never
//! `serde_json::from_*`: with that feature, `serde_json`'s decoder turns an
//! object holding its private number token into a number, so the record
//! would get a different `capsule_id` here than everywhere else.
//!
//! **`capsule_id`.** [`compute_capsule_id`] is the digest primitive: it does
//! not check `format_version` or `canonicalization_id` (a verifier's job,
//! see [`crate::verify`]), so it also names records that are not
//! capsules, such as padding records.

use agent_action_capsule::canonical::{self, CanonicalError};
use serde_json::{Map, Value};

pub use agent_action_capsule::canonical::{CANONICALIZATION_JCS, MAX_SAFE_INTEGER};
pub use agent_action_capsule::verify::DecodeError;

/// Producer-envelope bookkeeping fields that are never part of any
/// `capsule_id` preimage: the envelope's `signature`/`key_id` are attached to
/// a ledger line after `capsule_id` is computed.
pub const LOCAL_ONLY_FIELDS: &[&str] = &["signature", "key_id"];

/// Why a value has no canonical form.
#[derive(Debug, thiserror::Error)]
pub enum JcsError {
    #[error("JSON floating-point value in a digest-bearing field; §5.1 requires exact decimal strings for monetary/quantity values")]
    FloatInDigest,
    #[error("integer outside the safe range +/-{MAX_SAFE_INTEGER}; represent large integers as exact decimal strings (§5.1)")]
    UnsafeInteger,
    #[error("value is not JSON-serializable here")]
    NotSerializable,
}

impl From<CanonicalError> for JcsError {
    fn from(e: CanonicalError) -> Self {
        match e {
            CanonicalError::Float(_) => Self::FloatInDigest,
            CanonicalError::UnsafeInt(_) => Self::UnsafeInteger,
            _ => Self::NotSerializable,
        }
    }
}

/// RFC 8785 JCS serialization of `v` as UTF-8 bytes.
pub fn jcs(v: &Value) -> Result<Vec<u8>, JcsError> {
    Ok(canonical::jcs(v)?)
}

/// JSON-DIGEST (§2): lowercase-hex SHA-256 of JCS(v).
pub fn json_digest(v: &Value) -> Result<String, JcsError> {
    Ok(canonical::json_digest(v)?)
}

/// `capsule_id` (§5.1): the JSON-DIGEST of the record with `capsule_id` and
/// the local-only producer-envelope fields removed. Every other member,
/// `chain` and `references` included, is in the preimage.
pub fn compute_capsule_id(record: &Value) -> Result<String, JcsError> {
    let obj = record.as_object().ok_or(JcsError::NotSerializable)?;
    let preimage: Map<String, Value> = obj
        .iter()
        .filter(|(k, _)| *k != "capsule_id" && !canonical::is_local_only_field(k))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    json_digest(&Value::Object(preimage))
}

/// Decode record JSON (a ledger line, a statement payload, a capsule file)
/// strictly, as the references do: RFC 8259, numbers kept as written, an
/// object never read as a number. See the module doc.
pub fn decode_record(data: &[u8]) -> Result<Value, DecodeError> {
    agent_action_capsule::verify::decode_capsule_json(data)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn local_only_fields_are_the_canonicalizers() {
        for field in LOCAL_ONLY_FIELDS {
            assert!(canonical::is_local_only_field(field));
        }
        for field in ["capsule_id", "chain", "references", "store_nonce"] {
            assert!(!canonical::is_local_only_field(field));
        }
    }

    #[test]
    fn capsule_id_drops_the_id_and_envelope_and_needs_no_format_version() {
        let body = json!({"record_type": "padding", "store_nonce": "ab"});
        let mut record = body.clone();
        record["capsule_id"] = json!("x");
        record["signature"] = json!("s");
        record["key_id"] = json!("k");
        assert_eq!(
            compute_capsule_id(&record).unwrap(),
            json_digest(&body).unwrap()
        );
        assert!(matches!(
            compute_capsule_id(&json!([1])),
            Err(JcsError::NotSerializable)
        ));
    }

    #[test]
    fn numbers_are_judged_by_their_text() {
        let digest = |text: &str| json_digest(&decode_record(text.as_bytes()).unwrap());
        for float in [
            r#"{"a":1.0}"#,
            r#"{"a":1e2}"#,
            r#"{"a":-0.0}"#,
            r#"{"a":1e400}"#,
        ] {
            assert!(
                matches!(digest(float), Err(JcsError::FloatInDigest)),
                "{float}"
            );
        }
        for unsafe_int in [
            r#"{"a":9007199254740992}"#,
            r#"{"a":-9007199254740992}"#,
            r#"{"a":18446744073709551616}"#,
            r#"{"a":100000000000000000000000000000}"#,
        ] {
            assert!(
                matches!(digest(unsafe_int), Err(JcsError::UnsafeInteger)),
                "{unsafe_int}"
            );
        }
        // RFC 8785 writes negative zero as 0, as the Go and Python references do.
        assert_eq!(
            digest(r#"{"a":-0}"#).unwrap(),
            json_digest(&json!({"a": 0})).unwrap()
        );
        assert_eq!(
            jcs(&json!({"b": 9007199254740991i64, "a": -9007199254740991i64})).unwrap(),
            br#"{"a":-9007199254740991,"b":9007199254740991}"#
        );
    }

    /// `serde_json`'s private number token is an ordinary member name to
    /// every other implementation; it must be one here too.
    #[test]
    fn the_private_number_token_is_an_ordinary_member() {
        let text = br#"{"x":{"$serde_json::private::Number":"1"}}"#;
        let decoded = decode_record(text).unwrap();
        assert!(decoded["x"].is_object());
        assert_eq!(jcs(&decoded).unwrap(), text.to_vec());
    }
}
