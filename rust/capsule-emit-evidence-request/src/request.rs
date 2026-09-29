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
/// received. Trailing bytes after the request item are malformed.
pub fn parse_cbor(bytes: &[u8]) -> Result<Request, RequestError> {
    let mut reader = bytes;
    let value: ciborium::Value = ciborium::from_reader(&mut reader)
        .map_err(|_| RequestError::Malformed("not well-formed CBOR"))?;
    if !reader.is_empty() {
        return Err(RequestError::Malformed("bytes after the request item"));
    }
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
        assert_eq!(
            parse_cbor(&bytes).unwrap_err().reason(),
            Reason::RequestMalformed
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
