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
    let mut encoded = Vec::new();
    floats::serialize(value, &mut serde_json::Serializer::new(&mut encoded))
        .map_err(|e| ProduceError::invalid(format!("marshal JSON digest input: {e}")))?;
    decode_strict_json(&encoded)
        .map_err(|e| ProduceError::invalid(format!("decode JSON digest input: {e}")))
}

/// Native floats as Go's `encoding/json/v2` marshals them, which `serde_json`
/// does differently: Go refuses NaN and infinities (`serde_json` writes
/// `null`) and writes an integral value below 1e21 as the shortest digits
/// that round-trip at the value's own width, without a fraction (`2`,
/// `100000010` for the f32 nearest it), where `serde_json` writes `2.0`.
///
/// [`serialize`] makes one pass over the caller's value: [`GoFloats`] wraps
/// the `serde_json` serializer, rewrites each float as it arrives and
/// forwards everything else unchanged, wrapping every nested value again.
mod floats {
    use serde::ser::{self, Error as _, Serialize, Serializer};

    /// Serializes `value` into `serializer` with Go's float handling.
    pub(super) fn serialize<T: ?Sized + Serialize, S: Serializer>(
        value: &T,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        value.serialize(GoFloats(serializer))
    }

    /// What Go writes for a finite float: `Some(digits)` when the value is
    /// integral and below 1e21, which `Display` (shortest round-trip digits,
    /// never an exponent) prints without a fraction; `None` when the value
    /// keeps `serde_json`'s float form, which the strict decoder then reads
    /// as a float, as it reads Go's fraction or exponent form.
    pub(super) fn integer_text(text: String, magnitude: f64) -> Option<String> {
        (magnitude < 1e21 && !text.contains(['.', 'e', 'E'])).then_some(text)
    }

    fn unsupported<E: ser::Error>(value: f64) -> E {
        let name = if value.is_nan() {
            "NaN"
        } else if value > 0.0 {
            "+Inf"
        } else {
            "-Inf"
        };
        E::custom(format!("unsupported value: {name}"))
    }

    fn write_integer<S: Serializer>(serializer: S, digits: &str) -> Result<S::Ok, S::Error> {
        // Below 1e21, so always within i128.
        let integer: i128 = digits
            .parse()
            .map_err(|_| S::Error::custom("float digits are not an integer"))?;
        serializer.serialize_i128(integer)
    }

    /// A serialized value wrapped so its own floats go through [`GoFloats`].
    struct Wrap<'a, T: ?Sized>(&'a T);

    impl<T: ?Sized + Serialize> Serialize for Wrap<'_, T> {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            self.0.serialize(GoFloats(serializer))
        }
    }

    pub(super) struct GoFloats<S>(S);

    /// A compound serializer of the wrapped serializer, wrapping each element.
    pub(super) struct Compound<C>(C);

    impl<S: Serializer> Serializer for GoFloats<S> {
        type Ok = S::Ok;
        type Error = S::Error;
        type SerializeSeq = Compound<S::SerializeSeq>;
        type SerializeTuple = Compound<S::SerializeTuple>;
        type SerializeTupleStruct = Compound<S::SerializeTupleStruct>;
        type SerializeTupleVariant = Compound<S::SerializeTupleVariant>;
        type SerializeMap = Compound<S::SerializeMap>;
        type SerializeStruct = Compound<S::SerializeStruct>;
        type SerializeStructVariant = Compound<S::SerializeStructVariant>;

        fn serialize_f32(self, v: f32) -> Result<S::Ok, S::Error> {
            if !v.is_finite() {
                return Err(unsupported(f64::from(v)));
            }
            match integer_text(v.to_string(), f64::from(v.abs())) {
                Some(digits) => write_integer(self.0, &digits),
                None => self.0.serialize_f32(v),
            }
        }
        fn serialize_f64(self, v: f64) -> Result<S::Ok, S::Error> {
            if !v.is_finite() {
                return Err(unsupported(v));
            }
            match integer_text(v.to_string(), v.abs()) {
                Some(digits) => write_integer(self.0, &digits),
                None => self.0.serialize_f64(v),
            }
        }

        fn serialize_bool(self, v: bool) -> Result<S::Ok, S::Error> {
            self.0.serialize_bool(v)
        }
        fn serialize_i8(self, v: i8) -> Result<S::Ok, S::Error> {
            self.0.serialize_i8(v)
        }
        fn serialize_i16(self, v: i16) -> Result<S::Ok, S::Error> {
            self.0.serialize_i16(v)
        }
        fn serialize_i32(self, v: i32) -> Result<S::Ok, S::Error> {
            self.0.serialize_i32(v)
        }
        fn serialize_i64(self, v: i64) -> Result<S::Ok, S::Error> {
            self.0.serialize_i64(v)
        }
        fn serialize_i128(self, v: i128) -> Result<S::Ok, S::Error> {
            self.0.serialize_i128(v)
        }
        fn serialize_u8(self, v: u8) -> Result<S::Ok, S::Error> {
            self.0.serialize_u8(v)
        }
        fn serialize_u16(self, v: u16) -> Result<S::Ok, S::Error> {
            self.0.serialize_u16(v)
        }
        fn serialize_u32(self, v: u32) -> Result<S::Ok, S::Error> {
            self.0.serialize_u32(v)
        }
        fn serialize_u64(self, v: u64) -> Result<S::Ok, S::Error> {
            self.0.serialize_u64(v)
        }
        fn serialize_u128(self, v: u128) -> Result<S::Ok, S::Error> {
            self.0.serialize_u128(v)
        }
        fn serialize_char(self, v: char) -> Result<S::Ok, S::Error> {
            self.0.serialize_char(v)
        }
        fn serialize_str(self, v: &str) -> Result<S::Ok, S::Error> {
            self.0.serialize_str(v)
        }
        fn serialize_bytes(self, v: &[u8]) -> Result<S::Ok, S::Error> {
            self.0.serialize_bytes(v)
        }
        fn serialize_none(self) -> Result<S::Ok, S::Error> {
            self.0.serialize_none()
        }
        fn serialize_some<T: ?Sized + Serialize>(self, value: &T) -> Result<S::Ok, S::Error> {
            self.0.serialize_some(&Wrap(value))
        }
        fn serialize_unit(self) -> Result<S::Ok, S::Error> {
            self.0.serialize_unit()
        }
        fn serialize_unit_struct(self, name: &'static str) -> Result<S::Ok, S::Error> {
            self.0.serialize_unit_struct(name)
        }
        fn serialize_unit_variant(
            self,
            name: &'static str,
            index: u32,
            variant: &'static str,
        ) -> Result<S::Ok, S::Error> {
            self.0.serialize_unit_variant(name, index, variant)
        }
        fn serialize_newtype_struct<T: ?Sized + Serialize>(
            self,
            name: &'static str,
            value: &T,
        ) -> Result<S::Ok, S::Error> {
            self.0.serialize_newtype_struct(name, &Wrap(value))
        }
        fn serialize_newtype_variant<T: ?Sized + Serialize>(
            self,
            name: &'static str,
            index: u32,
            variant: &'static str,
            value: &T,
        ) -> Result<S::Ok, S::Error> {
            self.0
                .serialize_newtype_variant(name, index, variant, &Wrap(value))
        }
        fn serialize_seq(self, len: Option<usize>) -> Result<Self::SerializeSeq, S::Error> {
            self.0.serialize_seq(len).map(Compound)
        }
        fn serialize_tuple(self, len: usize) -> Result<Self::SerializeTuple, S::Error> {
            self.0.serialize_tuple(len).map(Compound)
        }
        fn serialize_tuple_struct(
            self,
            name: &'static str,
            len: usize,
        ) -> Result<Self::SerializeTupleStruct, S::Error> {
            self.0.serialize_tuple_struct(name, len).map(Compound)
        }
        fn serialize_tuple_variant(
            self,
            name: &'static str,
            index: u32,
            variant: &'static str,
            len: usize,
        ) -> Result<Self::SerializeTupleVariant, S::Error> {
            self.0
                .serialize_tuple_variant(name, index, variant, len)
                .map(Compound)
        }
        fn serialize_map(self, len: Option<usize>) -> Result<Self::SerializeMap, S::Error> {
            self.0.serialize_map(len).map(Compound)
        }
        fn serialize_struct(
            self,
            name: &'static str,
            len: usize,
        ) -> Result<Self::SerializeStruct, S::Error> {
            self.0.serialize_struct(name, len).map(Compound)
        }
        fn serialize_struct_variant(
            self,
            name: &'static str,
            index: u32,
            variant: &'static str,
            len: usize,
        ) -> Result<Self::SerializeStructVariant, S::Error> {
            self.0
                .serialize_struct_variant(name, index, variant, len)
                .map(Compound)
        }
        fn is_human_readable(&self) -> bool {
            self.0.is_human_readable()
        }
    }

    impl<C: ser::SerializeSeq> ser::SerializeSeq for Compound<C> {
        type Ok = C::Ok;
        type Error = C::Error;
        fn serialize_element<T: ?Sized + Serialize>(&mut self, value: &T) -> Result<(), C::Error> {
            self.0.serialize_element(&Wrap(value))
        }
        fn end(self) -> Result<C::Ok, C::Error> {
            self.0.end()
        }
    }

    impl<C: ser::SerializeTuple> ser::SerializeTuple for Compound<C> {
        type Ok = C::Ok;
        type Error = C::Error;
        fn serialize_element<T: ?Sized + Serialize>(&mut self, value: &T) -> Result<(), C::Error> {
            self.0.serialize_element(&Wrap(value))
        }
        fn end(self) -> Result<C::Ok, C::Error> {
            self.0.end()
        }
    }

    impl<C: ser::SerializeTupleStruct> ser::SerializeTupleStruct for Compound<C> {
        type Ok = C::Ok;
        type Error = C::Error;
        fn serialize_field<T: ?Sized + Serialize>(&mut self, value: &T) -> Result<(), C::Error> {
            self.0.serialize_field(&Wrap(value))
        }
        fn end(self) -> Result<C::Ok, C::Error> {
            self.0.end()
        }
    }

    impl<C: ser::SerializeTupleVariant> ser::SerializeTupleVariant for Compound<C> {
        type Ok = C::Ok;
        type Error = C::Error;
        fn serialize_field<T: ?Sized + Serialize>(&mut self, value: &T) -> Result<(), C::Error> {
            self.0.serialize_field(&Wrap(value))
        }
        fn end(self) -> Result<C::Ok, C::Error> {
            self.0.end()
        }
    }

    impl<C: ser::SerializeMap> ser::SerializeMap for Compound<C> {
        type Ok = C::Ok;
        type Error = C::Error;
        fn serialize_key<T: ?Sized + Serialize>(&mut self, key: &T) -> Result<(), C::Error> {
            self.0.serialize_key(&Wrap(key))
        }
        fn serialize_value<T: ?Sized + Serialize>(&mut self, value: &T) -> Result<(), C::Error> {
            self.0.serialize_value(&Wrap(value))
        }
        fn end(self) -> Result<C::Ok, C::Error> {
            self.0.end()
        }
    }

    impl<C: ser::SerializeStruct> ser::SerializeStruct for Compound<C> {
        type Ok = C::Ok;
        type Error = C::Error;
        fn serialize_field<T: ?Sized + Serialize>(
            &mut self,
            key: &'static str,
            value: &T,
        ) -> Result<(), C::Error> {
            self.0.serialize_field(key, &Wrap(value))
        }
        fn skip_field(&mut self, key: &'static str) -> Result<(), C::Error> {
            self.0.skip_field(key)
        }
        fn end(self) -> Result<C::Ok, C::Error> {
            self.0.end()
        }
    }

    impl<C: ser::SerializeStructVariant> ser::SerializeStructVariant for Compound<C> {
        type Ok = C::Ok;
        type Error = C::Error;
        fn serialize_field<T: ?Sized + Serialize>(
            &mut self,
            key: &'static str,
            value: &T,
        ) -> Result<(), C::Error> {
            self.0.serialize_field(key, &Wrap(value))
        }
        fn skip_field(&mut self, key: &'static str) -> Result<(), C::Error> {
            self.0.skip_field(key)
        }
        fn end(self) -> Result<C::Ok, C::Error> {
            self.0.end()
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

    /// Integral floats below 1e21 are written as Go's `encoding/json/v2`
    /// writes them: shortest round-trip digits at the value's own width.
    #[test]
    fn integral_float_text_is_go_shortest_form() {
        // Go's encoding/json/v2 output for each value, from a Go 1.27 run.
        for (text, magnitude, go) in [
            (100000010f32.to_string(), 100000008.0, Some("100000010")),
            (3e20f32.to_string(), 3e20, Some("300000000000000000000")),
            (
                123456789012345678901.0f64.to_string(),
                123456789012345678901.0,
                Some("123456789012345680000"),
            ),
            (1e20f64.to_string(), 1e20, Some("100000000000000000000")),
            (1e21f64.to_string(), 1e21, None),
            (1.5f64.to_string(), 1.5, None),
            (1e-7f64.to_string(), 1e-7, None),
        ] {
            assert_eq!(
                floats::integer_text(text.clone(), magnitude).as_deref(),
                go,
                "{text}"
            );
        }
    }

    /// The caller's value is serialized once: a value that changes between
    /// serializations cannot pass a check and then write something else.
    #[test]
    fn a_value_is_serialized_once() {
        struct Flaky(std::cell::Cell<u32>);
        impl serde::Serialize for Flaky {
            fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
                let calls = self.0.get();
                self.0.set(calls + 1);
                s.serialize_f64(if calls == 0 { 0.0 } else { f64::NAN })
            }
        }
        let flaky = Flaky(std::cell::Cell::new(0));
        assert_eq!(
            digest_json(&flaky).unwrap(),
            digest_json(&json!(0)).unwrap()
        );
        assert_eq!(flaky.0.get(), 1);
        assert_eq!(
            digest_json(&flaky).unwrap_err().to_string(),
            "marshal JSON digest input: unsupported value: NaN"
        );
    }

    /// Native floats are written as Go's `encoding/json/v2` writes them.
    #[test]
    fn native_floats_digest_as_go_marshals_them() {
        let digest = |text: &str| digest_json(&decode_strict_json(text.as_bytes()).unwrap());
        assert_eq!(digest_json(&2.0f64).unwrap(), digest("2").unwrap());
        assert_eq!(digest_json(&2.0f32).unwrap(), digest("2").unwrap());
        // The f32 nearest 100000010 is exactly 100000008; Go writes the
        // shortest digits that round-trip at 32 bits, 100000010.
        assert_eq!(f64::from(100000010f32), 100000008.0);
        assert_eq!(
            digest_json(&100000010f32).unwrap(),
            digest("100000010").unwrap()
        );
        assert_eq!(
            digest_json(&16777217f32).unwrap(),
            digest("16777216").unwrap()
        );
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
