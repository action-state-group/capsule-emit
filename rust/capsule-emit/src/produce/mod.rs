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
/// JCS form. `value` is serialized as Go's `encoding/json/v2` would write it
/// and decoded strictly again, so non-integral floats, NaN and infinities,
/// integers beyond +/-2^53-1 and duplicate member names are refused; a native
/// float with an integral value below 1e21 is written as an integer, and
/// negative zero as zero.
///
/// A [`serde_json::Value`] number keeps the text it was made from: `2.0`
/// parsed from JSON or built with `json!(2.0)` stays a float and is refused,
/// as Go refuses a `json.Number("2.0")`. `json!(f64::NAN)` is already `null`
/// before this function sees it.
pub fn digest_json<T: serde::Serialize + ?Sized>(value: &T) -> Result<String, ProduceError> {
    let decoded = marshal_digest_value(value)?;
    canonical::json_digest(&decoded)
        .map_err(|e| ProduceError::invalid(format!("canonicalize JSON digest input: {e}")))
}

fn marshal_digest_value<T: serde::Serialize + ?Sized>(value: &T) -> Result<Value, ProduceError> {
    let marshal_error =
        |e: String| ProduceError::invalid(format!("marshal JSON digest input: {e}"));
    value
        .serialize(floats::FiniteCheck)
        .map_err(|e| marshal_error(e.0))?;
    let mut encoded = Vec::new();
    value
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut encoded,
            floats::GoFloats,
        ))
        .map_err(|e| marshal_error(e.to_string()))?;
    decode_strict_json(&encoded)
        .map_err(|e| ProduceError::invalid(format!("decode JSON digest input: {e}")))
}

/// How Go's `encoding/json/v2` marshals native floats, which `serde_json`
/// does differently: Go refuses NaN and infinities (`serde_json` writes
/// `null`) and writes an integral value below 1e21 without a fraction (`2`,
/// `-0`), where `serde_json` writes `2.0`.
mod floats {
    use serde::ser::{self, Serialize};
    use std::io;

    /// Writes finite integral floats below 1e21 as integers; larger or
    /// fractional values keep `serde_json`'s form, which the strict decoder
    /// then reads as a float, as it reads Go's exponent form.
    pub(super) struct GoFloats;

    fn integral(value: f64) -> bool {
        value.fract() == 0.0 && value.abs() < 1e21
    }

    impl serde_json::ser::Formatter for GoFloats {
        fn write_f64<W: ?Sized + io::Write>(
            &mut self,
            writer: &mut W,
            value: f64,
        ) -> io::Result<()> {
            if integral(value) {
                write!(writer, "{value:.0}")
            } else {
                serde_json::ser::CompactFormatter.write_f64(writer, value)
            }
        }

        fn write_f32<W: ?Sized + io::Write>(
            &mut self,
            writer: &mut W,
            value: f32,
        ) -> io::Result<()> {
            if integral(f64::from(value)) {
                write!(writer, "{value:.0}")
            } else {
                serde_json::ser::CompactFormatter.write_f32(writer, value)
            }
        }
    }

    /// Why a value has no JSON form: a NaN or infinite float.
    #[derive(Debug)]
    pub(super) struct NonFinite(pub(super) String);

    impl std::fmt::Display for NonFinite {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str(&self.0)
        }
    }

    impl std::error::Error for NonFinite {}

    impl ser::Error for NonFinite {
        fn custom<T: std::fmt::Display>(message: T) -> Self {
            Self(message.to_string())
        }
    }

    fn check(value: f64) -> Result<(), NonFinite> {
        let name = if value.is_nan() {
            "NaN"
        } else if value == f64::INFINITY {
            "+Inf"
        } else if value == f64::NEG_INFINITY {
            "-Inf"
        } else {
            return Ok(());
        };
        Err(NonFinite(format!("unsupported value: {name}")))
    }

    /// A serializer that writes nothing and refuses the first NaN or
    /// infinite float anywhere in a value.
    pub(super) struct FiniteCheck;

    type Done = Result<(), NonFinite>;

    impl ser::Serializer for FiniteCheck {
        type Ok = ();
        type Error = NonFinite;
        type SerializeSeq = Self;
        type SerializeTuple = Self;
        type SerializeTupleStruct = Self;
        type SerializeTupleVariant = Self;
        type SerializeMap = Self;
        type SerializeStruct = Self;
        type SerializeStructVariant = Self;

        fn serialize_f32(self, v: f32) -> Done {
            check(f64::from(v))
        }
        fn serialize_f64(self, v: f64) -> Done {
            check(v)
        }
        fn serialize_bool(self, _: bool) -> Done {
            Ok(())
        }
        fn serialize_i8(self, _: i8) -> Done {
            Ok(())
        }
        fn serialize_i16(self, _: i16) -> Done {
            Ok(())
        }
        fn serialize_i32(self, _: i32) -> Done {
            Ok(())
        }
        fn serialize_i64(self, _: i64) -> Done {
            Ok(())
        }
        fn serialize_i128(self, _: i128) -> Done {
            Ok(())
        }
        fn serialize_u8(self, _: u8) -> Done {
            Ok(())
        }
        fn serialize_u16(self, _: u16) -> Done {
            Ok(())
        }
        fn serialize_u32(self, _: u32) -> Done {
            Ok(())
        }
        fn serialize_u64(self, _: u64) -> Done {
            Ok(())
        }
        fn serialize_u128(self, _: u128) -> Done {
            Ok(())
        }
        fn serialize_char(self, _: char) -> Done {
            Ok(())
        }
        fn serialize_str(self, _: &str) -> Done {
            Ok(())
        }
        fn serialize_bytes(self, _: &[u8]) -> Done {
            Ok(())
        }
        fn serialize_none(self) -> Done {
            Ok(())
        }
        fn serialize_some<T: ?Sized + Serialize>(self, value: &T) -> Done {
            value.serialize(self)
        }
        fn serialize_unit(self) -> Done {
            Ok(())
        }
        fn serialize_unit_struct(self, _: &'static str) -> Done {
            Ok(())
        }
        fn serialize_unit_variant(self, _: &'static str, _: u32, _: &'static str) -> Done {
            Ok(())
        }
        fn serialize_newtype_struct<T: ?Sized + Serialize>(
            self,
            _: &'static str,
            value: &T,
        ) -> Done {
            value.serialize(self)
        }
        fn serialize_newtype_variant<T: ?Sized + Serialize>(
            self,
            _: &'static str,
            _: u32,
            _: &'static str,
            value: &T,
        ) -> Done {
            value.serialize(self)
        }
        fn serialize_seq(self, _: Option<usize>) -> Result<Self, NonFinite> {
            Ok(self)
        }
        fn serialize_tuple(self, _: usize) -> Result<Self, NonFinite> {
            Ok(self)
        }
        fn serialize_tuple_struct(self, _: &'static str, _: usize) -> Result<Self, NonFinite> {
            Ok(self)
        }
        fn serialize_tuple_variant(
            self,
            _: &'static str,
            _: u32,
            _: &'static str,
            _: usize,
        ) -> Result<Self, NonFinite> {
            Ok(self)
        }
        fn serialize_map(self, _: Option<usize>) -> Result<Self, NonFinite> {
            Ok(self)
        }
        fn serialize_struct(self, _: &'static str, _: usize) -> Result<Self, NonFinite> {
            Ok(self)
        }
        fn serialize_struct_variant(
            self,
            _: &'static str,
            _: u32,
            _: &'static str,
            _: usize,
        ) -> Result<Self, NonFinite> {
            Ok(self)
        }
    }

    impl ser::SerializeSeq for FiniteCheck {
        type Ok = ();
        type Error = NonFinite;
        fn serialize_element<T: ?Sized + Serialize>(&mut self, value: &T) -> Done {
            value.serialize(FiniteCheck)
        }
        fn end(self) -> Done {
            Ok(())
        }
    }

    impl ser::SerializeTuple for FiniteCheck {
        type Ok = ();
        type Error = NonFinite;
        fn serialize_element<T: ?Sized + Serialize>(&mut self, value: &T) -> Done {
            value.serialize(FiniteCheck)
        }
        fn end(self) -> Done {
            Ok(())
        }
    }

    impl ser::SerializeTupleStruct for FiniteCheck {
        type Ok = ();
        type Error = NonFinite;
        fn serialize_field<T: ?Sized + Serialize>(&mut self, value: &T) -> Done {
            value.serialize(FiniteCheck)
        }
        fn end(self) -> Done {
            Ok(())
        }
    }

    impl ser::SerializeTupleVariant for FiniteCheck {
        type Ok = ();
        type Error = NonFinite;
        fn serialize_field<T: ?Sized + Serialize>(&mut self, value: &T) -> Done {
            value.serialize(FiniteCheck)
        }
        fn end(self) -> Done {
            Ok(())
        }
    }

    impl ser::SerializeMap for FiniteCheck {
        type Ok = ();
        type Error = NonFinite;
        fn serialize_key<T: ?Sized + Serialize>(&mut self, key: &T) -> Done {
            key.serialize(FiniteCheck)
        }
        fn serialize_value<T: ?Sized + Serialize>(&mut self, value: &T) -> Done {
            value.serialize(FiniteCheck)
        }
        fn end(self) -> Done {
            Ok(())
        }
    }

    impl ser::SerializeStruct for FiniteCheck {
        type Ok = ();
        type Error = NonFinite;
        fn serialize_field<T: ?Sized + Serialize>(&mut self, _: &'static str, value: &T) -> Done {
            value.serialize(FiniteCheck)
        }
        fn end(self) -> Done {
            Ok(())
        }
    }

    impl ser::SerializeStructVariant for FiniteCheck {
        type Ok = ();
        type Error = NonFinite;
        fn serialize_field<T: ?Sized + Serialize>(&mut self, _: &'static str, value: &T) -> Done {
            value.serialize(FiniteCheck)
        }
        fn end(self) -> Done {
            Ok(())
        }
    }
}

/// Strict JSON decoding as Go's `decodeStrictJSON`: UTF-8, one value, numbers
/// kept as written, and no duplicate member names. Nesting is bounded at 128
/// levels by the agent-action-capsule decoder.
pub(crate) fn decode_strict_json(data: &[u8]) -> Result<Value, String> {
    let value =
        agent_action_capsule::verify::decode_capsule_json(data).map_err(|e| e.to_string())?;
    if let Some(key) = duplicate_key(data)? {
        return Err(format!("duplicate object key {key:?}"));
    }
    Ok(value)
}

/// The first duplicated object member name in `data`, which the
/// agent-action-capsule decoder has already accepted as JSON. That decoder
/// keeps the last of two equal names; the Go producer refuses them, so this
/// module does too. The scan is iterative, so it reaches every member at any
/// depth the decoder accepts; anything it cannot read is an error, never "no
/// duplicate".
fn duplicate_key(data: &[u8]) -> Result<Option<String>, String> {
    use std::collections::HashSet;

    // One entry per open container: the names seen so far for an object,
    // `None` for an array.
    let mut open: Vec<Option<HashSet<String>>> = Vec::new();
    let mut expect_name = false;
    let mut at = 0;
    while at < data.len() {
        match data[at] {
            b'{' => {
                open.push(Some(HashSet::new()));
                expect_name = true;
            }
            b'[' => {
                open.push(None);
                expect_name = false;
            }
            b'}' | b']' => {
                open.pop()
                    .ok_or("unbalanced JSON while scanning for duplicate names")?;
                expect_name = false;
            }
            b',' => expect_name = matches!(open.last(), Some(Some(_))),
            b'"' => {
                let start = at;
                at += 1;
                loop {
                    match data.get(at) {
                        Some(b'\\') => at += 2,
                        Some(b'"') => break,
                        Some(_) => at += 1,
                        None => {
                            return Err(
                                "unterminated string while scanning for duplicate names".into()
                            )
                        }
                    }
                }
                if expect_name {
                    let name: String = serde_json::from_slice(&data[start..=at])
                        .map_err(|e| format!("member name while scanning for duplicates: {e}"))?;
                    if let Some(Some(seen)) = open.last_mut() {
                        if !seen.insert(name.clone()) {
                            return Ok(Some(name));
                        }
                    }
                    expect_name = false;
                }
            }
            _ => {}
        }
        at += 1;
    }
    if !open.is_empty() {
        return Err("unbalanced JSON while scanning for duplicate names".into());
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn duplicate_names_are_found_at_any_depth() {
        assert_eq!(
            duplicate_key(br#"{"a":1,"b":[{"c":1,"c":2}]}"#),
            Ok(Some("c".to_string()))
        );
        assert_eq!(duplicate_key(br#"{"a":{"a":1},"b":{"a":2}}"#), Ok(None));
        assert_eq!(duplicate_key(br#"[1,"x",null,true,-0,1.5]"#), Ok(None));
        // Names compare decoded, and strings that look like structure do not
        // open or close containers.
        assert_eq!(
            duplicate_key(br#"{"a":"}{,\"","a":2}"#),
            Ok(Some("a".to_string()))
        );
        assert_eq!(duplicate_key(br#"{"a":"\\","b":"a"}"#), Ok(None));
        assert!(decode_strict_json(br#"{"a":1,"a":2}"#)
            .unwrap_err()
            .contains("duplicate object key \"a\""));
    }

    /// A duplicate after a value at the decoder's 128-level limit is still
    /// found: the scan has no recursion limit of its own.
    #[test]
    fn duplicates_after_the_deepest_accepted_value_are_found() {
        let deep = format!("{}0{}", "[".repeat(127), "]".repeat(127));
        let text = format!("{{\"a\":{deep},\"a\":1}}");
        assert_eq!(
            decode_strict_json(text.as_bytes()).unwrap_err(),
            "duplicate object key \"a\""
        );
        let unique = format!("{{\"a\":{deep},\"b\":1}}");
        assert!(decode_strict_json(unique.as_bytes()).is_ok());
    }

    #[test]
    fn scan_errors_are_refusals_not_absent_duplicates() {
        assert!(duplicate_key(b"{\"a\":1").is_err());
        assert!(duplicate_key(b"]").is_err());
        assert!(duplicate_key(b"{\"a").is_err());
    }

    /// Native floats are written as Go's `encoding/json/v2` writes them.
    #[test]
    fn native_floats_digest_as_go_marshals_them() {
        let digest = |text: &str| digest_json(&decode_strict_json(text.as_bytes()).unwrap());
        assert_eq!(digest_json(&2.0f64).unwrap(), digest("2").unwrap());
        assert_eq!(digest_json(&2.0f32).unwrap(), digest("2").unwrap());
        assert_eq!(digest_json(&-0.0f64).unwrap(), digest("0").unwrap());
        // Go writes 1e20 as 100000000000000000000, an unsafe integer.
        assert!(digest_json(&1e20f64)
            .unwrap_err()
            .to_string()
            .contains("unsafe integer"));
        assert_eq!(
            digest_json(&9007199254740991.0f64).unwrap(),
            digest("9007199254740991").unwrap()
        );
        for refused in [1.5f64, 1e21, 9007199254740992.0, 1e-7] {
            assert!(
                digest_json(&refused)
                    .unwrap_err()
                    .to_string()
                    .starts_with("canonicalize JSON digest input: "),
                "{refused}"
            );
        }
        #[derive(serde::Serialize)]
        struct Reading {
            values: Vec<f64>,
        }
        for (value, name) in [
            (f64::INFINITY, "+Inf"),
            (f64::NEG_INFINITY, "-Inf"),
            (f64::NAN, "NaN"),
        ] {
            assert_eq!(
                digest_json(&value).unwrap_err().to_string(),
                format!("marshal JSON digest input: unsupported value: {name}")
            );
            let nested = Reading {
                values: vec![1.0, value],
            };
            assert!(digest_json(&nested).is_err(), "{name}");
        }
        assert_eq!(
            digest_json(&Reading {
                values: vec![1.0, -2.0]
            })
            .unwrap(),
            digest(r#"{"values":[1,-2]}"#).unwrap()
        );
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
