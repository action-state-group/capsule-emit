//! The request map (§3) in both bindings, and its well-formedness check.
//!
//! Wire shapes the vectors fix where -00 leaves them open:
//! - A subject is a map with exactly one member, named by the form token;
//!   its value is the content: null for `full_history` and `checkpoints`, a
//!   digest for `record` and `exchange`, `[a, b]` (unsigned, `a <= b`) for
//!   `range`, text for `correlation` (A1, A6).
//! - Digests are SHA-256 as 64 lowercase hex characters (A2).
//! - `coverage` is `{"expected_pin": <digest>}` or `{"min_freshness": <size
//!   or time>}`: an unsigned integer is a log size, text is an RFC 3339 UTC
//!   time (A3). An absent `coverage` counts as neither member; a coverage
//!   that is not a map, or a member value that does not conform, is
//!   malformed (A4).
//! - `deadline` (an RFC 3339 UTC time), `nonce` and `route` are text (A5).
//! - `derivation` is a registered token (`name/N`) or the digest of a
//!   definition (A14).
//!
//! Members this document does not define are ignored (§3).

use crate::registry::{is_digest, Reason, SubjectForm};
use crate::time::{parse_utc, UtcTime};
use crate::value::Val;
use std::collections::BTreeMap;

/// What a request asks about (§3.1).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Subject {
    FullHistory,
    Checkpoints,
    Record(String),
    /// Positions `a` through `b` inclusive, under the coverage anchor's
    /// ordering.
    Range(u64, u64),
    Correlation(String),
    /// The digest of the requester's own sealed half of a prior exchange.
    Exchange(String),
}

impl Subject {
    pub fn form(&self) -> SubjectForm {
        match self {
            Subject::FullHistory => SubjectForm::FullHistory,
            Subject::Checkpoints => SubjectForm::Checkpoints,
            Subject::Record(_) => SubjectForm::Record,
            Subject::Range(..) => SubjectForm::Range,
            Subject::Correlation(_) => SubjectForm::Correlation,
            Subject::Exchange(_) => SubjectForm::Exchange,
        }
    }
}

/// A `min_freshness` value: a log size or a time (A3).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Freshness {
    Size(u64),
    Time(UtcTime),
}

/// The coverage constraint: exactly one member (§3.2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Coverage {
    ExpectedPin(String),
    MinFreshness(Freshness),
}

/// A `derivation` value (§3.3, A14).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Derivation {
    /// A registered token, e.g. `history_card/1`.
    Token(String),
    /// The digest of a definition.
    Digest(String),
}

/// A well-formed request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    pub subject: Subject,
    pub coverage: Coverage,
    pub derivation: Option<Derivation>,
    pub deadline: Option<UtcTime>,
    pub nonce: Option<String>,
    pub route: Option<String>,
}

/// Why a request is not well formed: always one of two refusal reasons.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum RequestError {
    /// The request, or one of its members, does not conform
    /// (`request_malformed`).
    #[error("request_malformed: {0}")]
    Malformed(&'static str),
    /// Coverage carries both members or neither (`coverage_unsatisfiable`,
    /// §3.2).
    #[error(
        "coverage_unsatisfiable: coverage must carry exactly one of expected_pin, min_freshness"
    )]
    CoverageUnsatisfiable,
}

impl RequestError {
    /// The refusal reason a responder answers with.
    pub fn reason(&self) -> Reason {
        match self {
            RequestError::Malformed(_) => Reason::RequestMalformed,
            RequestError::CoverageUnsatisfiable => Reason::CoverageUnsatisfiable,
        }
    }
}

/// Parse and check a request in the JSON binding, from the bytes as
/// received. Duplicate member names are not detected in this binding.
pub fn parse_json(bytes: &[u8]) -> Result<Request, RequestError> {
    let value: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|_| RequestError::Malformed("not well-formed JSON"))?;
    from_val(&Val::from_json(&value))
}

/// Parse and check a request in the CBOR binding, from the bytes as
/// received. Trailing bytes after the request item are malformed, and so is
/// a request not in core deterministic encoding (§10; RFC 8949 §4.2.1),
/// at any depth: a duplicate or out-of-order map key, a non-shortest
/// argument, an indefinite length, a tag, a float, a byte string, or an
/// integer outside 0 to 2^53 - 1 ([`crate::det_cbor`]).
pub fn parse_cbor(bytes: &[u8]) -> Result<Request, RequestError> {
    let mut reader = bytes;
    let value: ciborium::Value = ciborium::from_reader(&mut reader)
        .map_err(|_| RequestError::Malformed("not well-formed CBOR"))?;
    if !reader.is_empty() {
        return Err(RequestError::Malformed("bytes after the request item"));
    }
    crate::det_cbor::check(bytes).map_err(RequestError::Malformed)?;
    from_val(&Val::from_cbor(&value))
}

fn from_val(value: &Val) -> Result<Request, RequestError> {
    let Val::Map(map) = value else {
        return Err(RequestError::Malformed("the request is not a map"));
    };
    let subject = parse_subject(
        map.get("subject")
            .ok_or(RequestError::Malformed("subject is missing"))?,
    )?;
    let derivation = map.get("derivation").map(parse_derivation).transpose()?;
    let deadline = map
        .get("deadline")
        .map(|v| {
            v.as_text()
                .and_then(parse_utc)
                .ok_or(RequestError::Malformed(
                    "deadline is not an RFC 3339 UTC time",
                ))
        })
        .transpose()?;
    let text = |key: &'static str, err: &'static str| {
        map.get(key)
            .map(|v| {
                v.as_text()
                    .map(str::to_string)
                    .ok_or(RequestError::Malformed(err))
            })
            .transpose()
    };
    let nonce = text("nonce", "nonce is not text")?;
    let route = text("route", "route is not text")?;
    let coverage = parse_coverage(map.get("coverage"))?;
    Ok(Request {
        subject,
        coverage,
        derivation,
        deadline,
        nonce,
        route,
    })
}

fn parse_subject(value: &Val) -> Result<Subject, RequestError> {
    let Val::Map(map) = value else {
        return Err(RequestError::Malformed("subject is not a map"));
    };
    let mut members = map.iter();
    let (Some((token, content)), None) = (members.next(), members.next()) else {
        return Err(RequestError::Malformed(
            "subject must have exactly one member",
        ));
    };
    let form = SubjectForm::from_token(token).ok_or(RequestError::Malformed(
        "subject form is not one of the six",
    ))?;
    let digest = |v: &Val| match v.as_text() {
        Some(d) if is_digest(d) => Ok(d.to_string()),
        _ => Err(RequestError::Malformed(
            "subject digest is not 64 lowercase hex",
        )),
    };
    match form {
        SubjectForm::FullHistory | SubjectForm::Checkpoints => match content {
            Val::Null => Ok(if form == SubjectForm::FullHistory {
                Subject::FullHistory
            } else {
                Subject::Checkpoints
            }),
            _ => Err(RequestError::Malformed(
                "this subject form carries no content",
            )),
        },
        SubjectForm::Record => Ok(Subject::Record(digest(content)?)),
        SubjectForm::Exchange => Ok(Subject::Exchange(digest(content)?)),
        SubjectForm::Correlation => match content.as_text() {
            Some(id) if !id.is_empty() => Ok(Subject::Correlation(id.to_string())),
            _ => Err(RequestError::Malformed("correlation is not non-empty text")),
        },
        SubjectForm::Range => match content {
            Val::Array(pair) => match pair.as_slice() {
                [Val::UInt(a), Val::UInt(b)] if a <= b => Ok(Subject::Range(*a, *b)),
                [Val::UInt(_), Val::UInt(_)] => {
                    Err(RequestError::Malformed("range start is after its end"))
                }
                _ => Err(RequestError::Malformed(
                    "range is not two unsigned integers",
                )),
            },
            _ => Err(RequestError::Malformed(
                "range is not two unsigned integers",
            )),
        },
    }
}

fn parse_derivation(value: &Val) -> Result<Derivation, RequestError> {
    let text = value
        .as_text()
        .ok_or(RequestError::Malformed("derivation is not text"))?;
    if is_digest(text) {
        return Ok(Derivation::Digest(text.to_string()));
    }
    match text.rsplit_once('/') {
        Some((name, version))
            if !name.is_empty()
                && !version.is_empty()
                && version.bytes().all(|b| b.is_ascii_digit()) =>
        {
            Ok(Derivation::Token(text.to_string()))
        }
        _ => Err(RequestError::Malformed(
            "derivation is neither a registered token nor a digest",
        )),
    }
}

fn parse_coverage(value: Option<&Val>) -> Result<Coverage, RequestError> {
    let empty = BTreeMap::new();
    let map = match value {
        None => &empty,
        Some(Val::Map(map)) => map,
        Some(_) => return Err(RequestError::Malformed("coverage is not a map")),
    };
    let pin = map
        .get("expected_pin")
        .map(|v| match v.as_text() {
            Some(d) if is_digest(d) => Ok(d.to_string()),
            _ => Err(RequestError::Malformed("expected_pin is not a digest")),
        })
        .transpose()?;
    let freshness = map
        .get("min_freshness")
        .map(|v| match v {
            Val::UInt(size) => Ok(Freshness::Size(*size)),
            Val::Text(t) => parse_utc(t)
                .map(Freshness::Time)
                .ok_or(RequestError::Malformed(
                    "min_freshness time is not RFC 3339 UTC",
                )),
            _ => Err(RequestError::Malformed(
                "min_freshness is neither a size nor a time",
            )),
        })
        .transpose()?;
    match (pin, freshness) {
        (Some(pin), None) => Ok(Coverage::ExpectedPin(pin)),
        (None, Some(freshness)) => Ok(Coverage::MinFreshness(freshness)),
        _ => Err(RequestError::CoverageUnsatisfiable),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PIN: &str = "6441370955d2b408884793962a664daec6bb25e06d76a2f53d8a80f66a675202";

    fn json(s: &str) -> Result<Request, RequestError> {
        parse_json(s.as_bytes())
    }

    #[test]
    fn a_minimal_request_parses() {
        let r = json(&format!(
            r#"{{"subject":{{"checkpoints":null}},"coverage":{{"expected_pin":"{PIN}"}}}}"#
        ))
        .unwrap();
        assert_eq!(r.subject, Subject::Checkpoints);
        assert_eq!(r.coverage, Coverage::ExpectedPin(PIN.into()));
        assert_eq!(r.nonce, None);
    }

    #[test]
    fn coverage_is_checked_after_member_values() {
        // A malformed member value is malformed, even when the other member
        // is also present.
        let both_one_bad =
            r#"{"subject":{"checkpoints":null},"coverage":{"expected_pin":"x","min_freshness":1}}"#;
        assert_eq!(
            json(both_one_bad).unwrap_err().reason(),
            Reason::RequestMalformed
        );
        let both_good = format!(
            r#"{{"subject":{{"checkpoints":null}},"coverage":{{"expected_pin":"{PIN}","min_freshness":1}}}}"#
        );
        assert_eq!(
            json(&both_good).unwrap_err().reason(),
            Reason::CoverageUnsatisfiable
        );
    }

    #[test]
    fn trailing_cbor_bytes_are_malformed() {
        let mut bytes = Vec::new();
        ciborium::into_writer(&ciborium::Value::Map(vec![]), &mut bytes).unwrap();
        bytes.push(0);
        assert_eq!(
            parse_cbor(&bytes).unwrap_err().reason(),
            Reason::RequestMalformed
        );
    }

    #[test]
    fn a_duplicate_cbor_member_is_malformed() {
        use ciborium::Value as C;
        let map = C::Map(vec![
            (
                C::Text("subject".into()),
                C::Map(vec![(C::Text("checkpoints".into()), C::Null)]),
            ),
            (
                C::Text("subject".into()),
                C::Map(vec![(C::Text("full_history".into()), C::Null)]),
            ),
            (
                C::Text("coverage".into()),
                C::Map(vec![(
                    C::Text("min_freshness".into()),
                    C::Integer(1.into()),
                )]),
            ),
        ]);
        let mut bytes = Vec::new();
        ciborium::into_writer(&map, &mut bytes).unwrap();
        // The determinism check names the duplicate. The value model would
        // also refuse it ("the request is not a map"), so the reason alone
        // would not show which check caught it.
        assert_eq!(
            parse_cbor(&bytes),
            Err(RequestError::Malformed("a duplicate map key"))
        );
    }

    /// A key repeated with the same value is still a duplicate.
    #[test]
    fn a_duplicate_with_an_equal_value_is_malformed() {
        let mut entries = request_b_members(&["nonce", "nonce", "profile", "subject", "coverage"]);
        entries[1].1 = entries[0].1.clone();
        assert_eq!(
            parse_cbor(&encode(entries)),
            Err(RequestError::Malformed("a duplicate map key"))
        );
    }

    /// `evidence-interop/0` §10 request B: the 228 deterministic CBOR
    /// octets the stream, A2A and MCP bindings all carry.
    const REQUEST_B: &str = "a4656e6f6e636576323032362d31302d31325430303a30303a30355a2f626770726f66696c657265766964656e63652d696e7465726f702f30677375626a656374a1667265636f726478403261623335303839356638363764353461356366646331646431616132313463626337323363363539363633343630653735313164666631366135636363383568636f766572616765a16c65787065637465645f70696e784036343431333730393535643262343038383834373933393632613636346461656336626232356530366437366132663533643861383066363661363735323032";

    /// Request B's members, in the order given.
    fn request_b_members(order: &[&str]) -> Vec<(ciborium::Value, ciborium::Value)> {
        use ciborium::Value as C;
        let member = |k: &str| {
            let v = match k {
                "nonce" => C::Text("2026-10-12T00:00:05Z/b".into()),
                "profile" => C::Text("evidence-interop/0".into()),
                "subject" => C::Map(vec![(
                    C::Text("record".into()),
                    C::Text(
                        "2ab350895f867d54a5cfdc1dd1aa214cbc723c659663460e7511dff16a5ccc85".into(),
                    ),
                )]),
                "coverage" => C::Map(vec![(C::Text("expected_pin".into()), C::Text(PIN.into()))]),
                _ => unreachable!(),
            };
            (C::Text(k.into()), v)
        };
        order.iter().map(|k| member(k)).collect()
    }

    fn encode(entries: Vec<(ciborium::Value, ciborium::Value)>) -> Vec<u8> {
        let mut bytes = Vec::new();
        ciborium::into_writer(&ciborium::Value::Map(entries), &mut bytes).unwrap();
        bytes
    }

    #[test]
    fn request_b_is_accepted_byte_for_byte() {
        let bytes = hex::decode(REQUEST_B).unwrap();
        assert_eq!(bytes.len(), 228);
        assert_eq!(
            crate::digest::request_digest(&bytes),
            "ba7fee76b7afe9f3ec5ce46975865e06040492f0da733b28cbc86bd2be609d5e"
        );
        let r = parse_cbor(&bytes).unwrap();
        assert_eq!(r.coverage, Coverage::ExpectedPin(PIN.into()));
        assert_eq!(r.nonce.as_deref(), Some("2026-10-12T00:00:05Z/b"));
        // The same members in length-first key order are the same octets.
        let rebuilt = encode(request_b_members(&[
            "nonce", "profile", "subject", "coverage",
        ]));
        assert_eq!(rebuilt, bytes);
    }

    /// The same request with its keys in JCS (lexical) order is a different
    /// encoding of it, and not the deterministic one.
    #[test]
    fn request_b_in_jcs_key_order_is_malformed() {
        let jcs_order = encode(request_b_members(&[
            "coverage", "nonce", "profile", "subject",
        ]));
        assert_eq!(
            parse_cbor(&jcs_order),
            Err(RequestError::Malformed(
                "map keys not in the bytewise order of their encodings"
            ))
        );
    }

    #[test]
    fn request_b_with_a_second_subject_is_malformed() {
        let mut entries =
            request_b_members(&["nonce", "profile", "subject", "subject", "coverage"]);
        entries[3].1 = ciborium::Value::Map(vec![(
            ciborium::Value::Text("checkpoints".into()),
            ciborium::Value::Null,
        )]);
        assert_eq!(
            parse_cbor(&encode(entries)),
            Err(RequestError::Malformed("a duplicate map key"))
        );
    }

    /// A duplicate inside a member this crate ignores is still a duplicate:
    /// the frame is not deterministic, whatever the parser reads of it.
    #[test]
    fn a_duplicate_inside_an_ignored_member_is_malformed() {
        use ciborium::Value as C;
        let mut entries = request_b_members(&["nonce", "subject", "coverage"]);
        entries.insert(
            0,
            (
                C::Text("x".into()),
                C::Map(vec![
                    (C::Text("a".into()), C::Null),
                    (C::Text("a".into()), C::Null),
                ]),
            ),
        );
        assert_eq!(
            parse_cbor(&encode(entries)),
            Err(RequestError::Malformed("a duplicate map key"))
        );
    }

    #[test]
    fn optional_fields_must_have_their_types() {
        for bad in [
            r#""deadline":"tomorrow""#,
            r#""nonce":7"#,
            r#""route":["a"]"#,
            r#""derivation":"history_card""#,
        ] {
            let s = format!(
                r#"{{"subject":{{"checkpoints":null}},"coverage":{{"min_freshness":1}},{bad}}}"#
            );
            assert_eq!(
                json(&s).unwrap_err().reason(),
                Reason::RequestMalformed,
                "{bad}"
            );
        }
    }
}
