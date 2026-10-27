//! Capsule decoding and Class 1 verification, and Producer Envelope
//! verification, as Go's `verify.go` and `DecodePayload`.

use agent_action_capsule::{envelope, verify};
use serde_json::{Map, Value};

use super::constants::{CANONICALIZATION_ID, FORMAT_VERSION};
use super::{decode_strict_json, Class1Error, EnvelopeError, ProduceError};

/// Decode Capsule bytes strictly: UTF-8, one JSON object, numbers kept as
/// written, no duplicate member names.
pub fn decode_payload(payload: &[u8]) -> Result<Map<String, Value>, ProduceError> {
    match decode_strict_json(payload) {
        Ok(Value::Object(members)) => Ok(members),
        Ok(_) => Err(ProduceError::invalid("capsule payload is not an object")),
        Err(e) => Err(ProduceError::invalid(format!(
            "decode capsule payload: {e}"
        ))),
    }
}

/// Decode a format-4 Capsule and run AAC Class 1 verification. `spec_version`
/// is not compared: -04, -05 and unrecognized values reach Class 1 alike.
/// The local-only `signature` and `key_id` members are neither committed by
/// the Capsule ID nor authenticated here; verify the signature with
/// [`verify_envelope`] and bind any carried `key_id` to its public key.
pub fn verify_capsule(data: &[u8]) -> Result<verify::VerificationResult, ProduceError> {
    let payload = decode_payload(data)?;
    let is = |key: &str, want: &str| payload.get(key).and_then(Value::as_str) == Some(want);
    if !is("format_version", FORMAT_VERSION) || !is("canonicalization_id", CANONICALIZATION_ID) {
        return Err(ProduceError::invalid(format!(
            "unsupported Capsule profile: only AAC format 4 with canonicalization_id {CANONICALIZATION_ID:?} is supported"
        )));
    }
    let result = verify::verify(&Value::Object(payload), None);
    if !result.ok {
        return Err(ProduceError::Class1(Class1Error {
            findings: result.findings,
        }));
    }
    Ok(result)
}

/// Authenticate one Producer Envelope against a Capsule ID. The public key in
/// the result is evidence of who signed, not an authorization decision.
pub fn verify_envelope(
    capsule_id: &str,
    data: &[u8],
) -> Result<envelope::VerificationResult, ProduceError> {
    let result = envelope::verify(capsule_id, data);
    if !result.ok {
        return Err(ProduceError::Envelope(EnvelopeError {
            findings: result.findings,
        }));
    }
    Ok(result)
}
