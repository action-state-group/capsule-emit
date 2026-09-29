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
//! **To accept a refusal as the answer to your request, use [`verify_for`]**:
//! it requires the signature to come from the responder's key you expected
//! and the refusal to name your request's digest. [`check`] only reports
//! shape and self-consistency: it verifies the signature under the key the
//! refusal itself names, so any key can produce a refusal that `check`
//! calls conformant. It is not authentication.
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
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
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

/// Check a refusal's **shape and self-consistency only; NOT
/// authentication.** The signature is verified under the refusal's own
/// `key_id`, which anyone can choose, and the request it names is not
/// compared with anything. To accept a refusal as the responder's answer to
/// a request, use [`verify_for`].
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

/// [`check`] for the CBOR binding: shape and self-consistency only; NOT
/// authentication (see [`verify_for_cbor`]).
pub fn check_cbor(frame: &[u8]) -> RefusalCheck {
    match decode_cbor_object(frame) {
        Some(object) => check(&object),
        None => RefusalCheck {
            signature_valid: false,
            reason_registered: false,
            conformant: false,
        },
    }
}

fn decode_cbor_object(frame: &[u8]) -> Option<Value> {
    let mut reader = frame;
    let value = ciborium::from_reader::<ciborium::Value, _>(&mut reader).ok()?;
    if !reader.is_empty() {
        return None;
    }
    match cbor_to_json(&value)? {
        object @ Value::Object(_) => Some(object),
        _ => None,
    }
}

/// Why [`verify_for`] did not accept a refusal.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum VerifyError {
    /// Not a conforming refusal (shape, registry or self-signature).
    #[error("not a conforming refusal")]
    NotConformant,
    /// Conforming, but signed under a key other than the responder's.
    #[error("the refusal is not signed by the expected responder key")]
    WrongKey,
    /// Conforming and from the responder, but about a different request.
    #[error("the refusal names a different request digest")]
    WrongRequest,
}

/// Authenticate a refusal as `responder_key`'s answer to the request whose
/// digest is `request_digest`: it must be conforming ([`check`]), its
/// `key_id` must be `responder_key`, its signature must verify under that key
/// (`verify_strict`: no small-order keys, canonical signatures only), and its
/// `request_digest` must be the one given. Returns the refusal's reason.
pub fn verify_for(
    object: &Value,
    responder_key: &VerifyingKey,
    request_digest: &str,
) -> Result<Reason, VerifyError> {
    let map = object.as_object().ok_or(VerifyError::NotConformant)?;
    if !check(object).conformant {
        return Err(VerifyError::NotConformant);
    }
    let text = |k: &str| map.get(k).and_then(Value::as_str);
    if text("key_id") != Some(hex::encode(responder_key.to_bytes()).as_str()) {
        return Err(VerifyError::WrongKey);
    }
    if !signature_verifies_under(map, responder_key) {
        return Err(VerifyError::WrongKey);
    }
    if text("request_digest") != Some(request_digest) {
        return Err(VerifyError::WrongRequest);
    }
    text("reason")
        .and_then(Reason::from_token)
        .ok_or(VerifyError::NotConformant)
}

/// [`verify_for`] for the CBOR binding.
pub fn verify_for_cbor(
    frame: &[u8],
    responder_key: &VerifyingKey,
    request_digest: &str,
) -> Result<Reason, VerifyError> {
    let object = decode_cbor_object(frame).ok_or(VerifyError::NotConformant)?;
    verify_for(&object, responder_key, request_digest)
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
    map.get("key_id")
        .and_then(Value::as_str)
        .and_then(|k| hex::decode(k).ok())
        .and_then(|b| <[u8; 32]>::try_from(b).ok())
        .and_then(|b| VerifyingKey::from_bytes(&b).ok())
        .is_some_and(|key| signature_verifies_under(map, &key))
}

fn signature_verifies_under(map: &Map<String, Value>, key: &VerifyingKey) -> bool {
    let sig = map
        .get("sig")
        .and_then(Value::as_str)
        .and_then(|s| hex::decode(s).ok())
        .and_then(|b| <[u8; 64]>::try_from(b).ok())
        .map(|b| Signature::from_bytes(&b));
    let (Some(sig), Ok(body)) = (sig, signing_body(map)) else {
        return false;
    };
    key.verify_strict(body.as_bytes(), &sig).is_ok()
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
    fn verify_for_binds_the_responder_key_and_the_request() {
        let responder = key();
        let digest = "a".repeat(64);
        let r = sign(
            &digest,
            Reason::NoSuchSubject,
            "2026-09-28T00:00:00Z",
            &responder,
        )
        .unwrap();

        // The right responder and the right request: accepted.
        assert_eq!(
            verify_for(&r.to_json(), &responder.verifying_key(), &digest),
            Ok(Reason::NoSuchSubject)
        );
        assert_eq!(
            verify_for_cbor(&r.to_cbor(), &responder.verifying_key(), &digest),
            Ok(Reason::NoSuchSubject)
        );
        // Expecting a different responder: refused.
        let other = SigningKey::from_bytes(&[6u8; 32]);
        assert_eq!(
            verify_for(&r.to_json(), &other.verifying_key(), &digest),
            Err(VerifyError::WrongKey)
        );
        // A different request: refused.
        assert_eq!(
            verify_for(&r.to_json(), &responder.verifying_key(), &"b".repeat(64)),
            Err(VerifyError::WrongRequest)
        );
    }

    #[test]
    fn a_refusal_forged_under_any_key_passes_check_but_not_verify_for() {
        // `check` is self-consistency only: an attacker signing with its own
        // key produces a refusal `check` calls conformant.
        let responder = key();
        let attacker = SigningKey::from_bytes(&[66u8; 32]);
        let digest = "a".repeat(64);
        let forged = sign(
            &digest,
            Reason::PolicyDeclined,
            "2026-09-28T00:00:00Z",
            &attacker,
        )
        .unwrap();
        assert!(check(&forged.to_json()).conformant);
        assert_eq!(
            verify_for(&forged.to_json(), &responder.verifying_key(), &digest),
            Err(VerifyError::WrongKey)
        );
        // Swapping in the responder's key_id does not help: the signature no
        // longer verifies, so it is not even conformant.
        let mut relabelled = forged.to_json();
        relabelled["key_id"] = json!(hex::encode(responder.verifying_key().to_bytes()));
        assert_eq!(
            verify_for(&relabelled, &responder.verifying_key(), &digest),
            Err(VerifyError::NotConformant)
        );
    }

    #[test]
    fn verify_for_refuses_non_conforming_refusals() {
        let responder = key();
        let digest = "a".repeat(64);
        let r = sign(
            &digest,
            Reason::NoSuchSubject,
            "2026-09-28T00:00:00Z",
            &responder,
        )
        .unwrap();
        let mut unregistered = r.to_json();
        unregistered["reason"] = json!("no_such_record");
        assert_eq!(
            verify_for(&unregistered, &responder.verifying_key(), &digest),
            Err(VerifyError::NotConformant)
        );
        assert_eq!(
            verify_for_cbor(&[0xff], &responder.verifying_key(), &digest),
            Err(VerifyError::NotConformant)
        );
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
