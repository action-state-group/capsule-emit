//! The signed refusal (§4.2).
//!
//! A refusal carries the request digest, one registered reason and an
//! issuance time, signed by the responder. -00 leaves member names and the
//! signature format to the deployment; this crate uses the profile the
//! conformance vectors use (A7): Ed25519 over the RFC 8785 serialization of
//! the object with exactly the members `issued_at`, `reason` and
//! `request_digest`; `key_id` is the raw 32-byte public key and `sig` the
//! 64-byte signature, both lowercase hex. In the CBOR binding the same map
//! is the frame; the signature is still over the RFC 8785 body.
//!
//! [`check`] keeps three questions apart, as the vectors require:
//! - `signature_valid`: does `sig` verify under `key_id` over the signed
//!   members as they appear (whatever their values)?
//! - `reason_registered`: is `reason` exactly one registered token?
//! - `conformant`: both, and every member §4.2 requires is present and well
//!   formed.
//!
//! A correctly signed object with an unregistered reason is not a conforming
//! refusal, and is still the responder's signed act (A13): see
//! [`crate::outcome`].

use crate::registry::{is_digest, Reason};
use crate::time::parse_utc;
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde_json::{json, Map, Value};

/// The members the signature covers.
pub const SIGNED_MEMBERS: [&str; 3] = ["issued_at", "reason", "request_digest"];

/// A refusal as sent: the three signed members plus `key_id` and `sig`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub request_digest: String,
    pub reason: Reason,
    pub issued_at: String,
    pub key_id: String,
    pub sig: String,
}

impl Refusal {
    /// The refusal as a JSON object (the JSON binding).
    pub fn to_json(&self) -> Value {
        json!({
            "issued_at": self.issued_at,
            "reason": self.reason.token(),
            "request_digest": self.request_digest,
            "key_id": self.key_id,
            "sig": self.sig,
        })
    }

    /// The refusal as a CBOR map (the CBOR binding).
    pub fn to_cbor(&self) -> Vec<u8> {
        use ciborium::Value as C;
        let text = |s: &str| C::Text(s.to_string());
        let map = C::Map(vec![
            (text("issued_at"), text(&self.issued_at)),
            (text("reason"), text(self.reason.token())),
            (text("request_digest"), text(&self.request_digest)),
            (text("key_id"), text(&self.key_id)),
            (text("sig"), text(&self.sig)),
        ]);
        let mut out = Vec::new();
        ciborium::into_writer(&map, &mut out).expect("writing to a Vec never fails");
        out
    }
}

/// The bytes a refusal's signature covers: RFC 8785 of the signed members
/// present in `object`.
pub fn signing_body(object: &Map<String, Value>) -> Result<String, crate::jcs::JcsError> {
    let body: Map<String, Value> = SIGNED_MEMBERS
        .iter()
        .filter_map(|k| object.get(*k).map(|v| ((*k).to_string(), v.clone())))
        .collect();
    crate::jcs::to_string(&Value::Object(body))
}

/// Why a refusal cannot be signed.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum SignError {
    #[error("request_digest must be 64 lowercase hex")]
    RequestDigest,
    #[error("issued_at must be an RFC 3339 UTC time")]
    IssuedAt,
}

/// Sign a refusal of the request whose digest is `request_digest`.
pub fn sign(
    request_digest: &str,
    reason: Reason,
    issued_at: &str,
    signing_key: &SigningKey,
) -> Result<Refusal, SignError> {
    if !is_digest(request_digest) {
        return Err(SignError::RequestDigest);
    }
    if parse_utc(issued_at).is_none() {
        return Err(SignError::IssuedAt);
    }
    let mut body = Map::new();
    body.insert("issued_at".into(), json!(issued_at));
    body.insert("reason".into(), json!(reason.token()));
    body.insert("request_digest".into(), json!(request_digest));
    let message = signing_body(&body).expect("text members always serialize");
    let sig = signing_key.sign(message.as_bytes());
    Ok(Refusal {
        request_digest: request_digest.to_string(),
        reason,
        issued_at: issued_at.to_string(),
        key_id: hex::encode(signing_key.verifying_key().to_bytes()),
        sig: hex::encode(sig.to_bytes()),
    })
}

/// The three answers about a received refusal object.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RefusalCheck {
    pub signature_valid: bool,
    pub reason_registered: bool,
    pub conformant: bool,
}

/// Check a refusal received in the JSON binding (a JSON object).
pub fn check(object: &Value) -> RefusalCheck {
    let Some(map) = object.as_object() else {
        return RefusalCheck {
            signature_valid: false,
            reason_registered: false,
            conformant: false,
        };
    };
    let text = |k: &str| map.get(k).and_then(Value::as_str);
    let signature_valid = signature_verifies(map);
    let reason_registered = text("reason").and_then(Reason::from_token).is_some();
    let members_well_formed = text("request_digest").is_some_and(is_digest)
        && text("issued_at").is_some_and(|t| parse_utc(t).is_some())
        && text("key_id").is_some_and(|k| is_hex_of_len(k, 32))
        && text("sig").is_some_and(|s| is_hex_of_len(s, 64));
    RefusalCheck {
        signature_valid,
        reason_registered,
        conformant: signature_valid && reason_registered && members_well_formed,
    }
}

/// Check a refusal received in the CBOR binding.
pub fn check_cbor(frame: &[u8]) -> RefusalCheck {
    let invalid = RefusalCheck {
        signature_valid: false,
        reason_registered: false,
        conformant: false,
    };
    let mut reader = frame;
    let Ok(value) = ciborium::from_reader::<ciborium::Value, _>(&mut reader) else {
        return invalid;
    };
    if !reader.is_empty() {
        return invalid;
    }
    match cbor_to_json(&value) {
        Some(object @ Value::Object(_)) => check(&object),
        _ => invalid,
    }
}

fn cbor_to_json(value: &ciborium::Value) -> Option<Value> {
    use ciborium::Value as C;
    Some(match value {
        C::Null => Value::Null,
        C::Bool(b) => json!(b),
        C::Text(s) => json!(s),
        C::Integer(i) => {
            let i = i128::from(*i);
            if let Ok(u) = u64::try_from(i) {
                json!(u)
            } else {
                json!(i64::try_from(i).ok()?)
            }
        }
        C::Array(items) => Value::Array(items.iter().map(cbor_to_json).collect::<Option<_>>()?),
        C::Map(entries) => {
            let mut map = Map::new();
            for (k, v) in entries {
                let C::Text(key) = k else { return None };
                if map.insert(key.clone(), cbor_to_json(v)?).is_some() {
                    return None;
                }
            }
            Value::Object(map)
        }
        _ => return None,
    })
}

fn is_hex_of_len(s: &str, bytes: usize) -> bool {
    s.len() == bytes * 2
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn signature_verifies(map: &Map<String, Value>) -> bool {
    let key = map
        .get("key_id")
        .and_then(Value::as_str)
        .and_then(|k| hex::decode(k).ok())
        .and_then(|b| <[u8; 32]>::try_from(b).ok())
        .and_then(|b| VerifyingKey::from_bytes(&b).ok());
    let sig = map
        .get("sig")
        .and_then(Value::as_str)
        .and_then(|s| hex::decode(s).ok())
        .and_then(|b| <[u8; 64]>::try_from(b).ok())
        .map(|b| Signature::from_bytes(&b));
    let (Some(key), Some(sig), Ok(body)) = (key, sig, signing_body(map)) else {
        return false;
    };
    key.verify(body.as_bytes(), &sig).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> SigningKey {
        SigningKey::from_bytes(&[5u8; 32])
    }

    #[test]
    fn a_signed_refusal_checks_conformant_in_both_bindings() {
        let r = sign(
            &"a".repeat(64),
            Reason::NoSuchSubject,
            "2026-09-28T00:00:00Z",
            &key(),
        )
        .unwrap();
        let ok = RefusalCheck {
            signature_valid: true,
            reason_registered: true,
            conformant: true,
        };
        assert_eq!(check(&r.to_json()), ok);
        assert_eq!(check_cbor(&r.to_cbor()), ok);
    }

    #[test]
    fn changing_a_signed_member_breaks_the_signature() {
        let r = sign(
            &"a".repeat(64),
            Reason::NoSuchSubject,
            "2026-09-28T00:00:00Z",
            &key(),
        )
        .unwrap();
        let mut v = r.to_json();
        v["reason"] = json!("policy_declined");
        let c = check(&v);
        assert!(!c.signature_valid && c.reason_registered && !c.conformant);
    }

    #[test]
    fn sign_refuses_malformed_members() {
        assert_eq!(
            sign("abc", Reason::NoSuchSubject, "2026-09-28T00:00:00Z", &key()),
            Err(SignError::RequestDigest)
        );
        assert_eq!(
            sign(&"a".repeat(64), Reason::NoSuchSubject, "now", &key()),
            Err(SignError::IssuedAt)
        );
    }

    #[test]
    fn non_objects_and_bad_frames_fail_every_check() {
        let none = RefusalCheck {
            signature_valid: false,
            reason_registered: false,
            conformant: false,
        };
        assert_eq!(check(&json!("refused")), none);
        assert_eq!(check_cbor(&[0xff]), none);
    }
}
