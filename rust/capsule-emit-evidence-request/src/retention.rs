//! Retention commitments (§9).
//!
//! A commitment promises that a subject stays answerable until a time.
//! Inside `until`, only the four reasons about the request or the
//! responder's capability are consistent with the promise; any other
//! refusal breaches it (A9). After `until`, `retention_expired` is the
//! correct token, and `no_such_subject` is the wrong one: a lapsed promise
//! must not read as "never held" (A10). A commitment names one subject a
//! digest or a position under a checkpoint identifies: `record`, `exchange`
//! or `range` (A11).

use crate::registry::{is_digest, Reason};
use crate::time::parse_utc;
use serde_json::Value;

/// The state of the commitment when a refusal arrived.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Commitment {
    InForce,
    Lapsed,
    None,
}

/// How a refusal stands against a commitment.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RefusalClass {
    NoBreach,
    Breach,
    /// After `until`, a refusal that reads as "never held".
    WrongToken,
    /// After `until`, `retention_expired`.
    Correct,
    /// There is no commitment to breach.
    NoCommitment,
}

impl RefusalClass {
    pub fn token(self) -> &'static str {
        match self {
            RefusalClass::NoBreach => "no_breach",
            RefusalClass::Breach => "breach",
            RefusalClass::WrongToken => "wrong_token",
            RefusalClass::Correct => "correct",
            RefusalClass::NoCommitment => "no_commitment",
        }
    }
}

/// Classify a refusal with `reason` against `commitment`.
pub fn classify_refusal(commitment: Commitment, reason: Reason) -> RefusalClass {
    let about_the_request = matches!(
        reason,
        Reason::NotAuthorized
            | Reason::CoverageUnsatisfiable
            | Reason::DerivationUnsupported
            | Reason::RequestMalformed
    );
    match commitment {
        Commitment::None => RefusalClass::NoCommitment,
        Commitment::InForce if about_the_request => RefusalClass::NoBreach,
        Commitment::InForce => RefusalClass::Breach,
        Commitment::Lapsed => match reason {
            Reason::RetentionExpired => RefusalClass::Correct,
            Reason::NoSuchSubject => RefusalClass::WrongToken,
            _ => RefusalClass::NoBreach,
        },
    }
}

/// How a recorded absence stands against a commitment: with one in force it
/// is attributable to the committed party; otherwise it records only the
/// attempt. Either way it stays a recorded absence, never a refusal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AbsenceClass {
    AttributableAbsence,
    AttemptOnly,
}

impl AbsenceClass {
    pub fn token(self) -> &'static str {
        match self {
            AbsenceClass::AttributableAbsence => "attributable_absence",
            AbsenceClass::AttemptOnly => "attempt_only",
        }
    }
}

pub fn classify_absence(commitment: Commitment) -> AbsenceClass {
    match commitment {
        Commitment::InForce => AbsenceClass::AttributableAbsence,
        Commitment::Lapsed | Commitment::None => AbsenceClass::AttemptOnly,
    }
}

/// Whether a commitment object is well formed: `evidence_stream` text, a
/// `subject` of form `record` or `exchange` (a digest) or `range` (two
/// unsigned integers, `a <= b`), and `until` an RFC 3339 UTC time.
pub fn commitment_well_formed(commitment: &Value) -> bool {
    let Some(map) = commitment.as_object() else {
        return false;
    };
    let stream_ok = map
        .get("evidence_stream")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty());
    let until_ok = map
        .get("until")
        .and_then(Value::as_str)
        .is_some_and(|u| parse_utc(u).is_some());
    let subject_ok = map
        .get("subject")
        .and_then(Value::as_object)
        .is_some_and(|s| {
            let mut members = s.iter();
            match (members.next(), members.next()) {
                (Some((form, content)), None) => match form.as_str() {
                    "record" | "exchange" => content.as_str().is_some_and(is_digest),
                    "range" => match content.as_array().map(Vec::as_slice) {
                        Some([a, b]) => {
                            matches!((a.as_u64(), b.as_u64()), (Some(a), Some(b)) if a <= b)
                        }
                        _ => false,
                    },
                    _ => false,
                },
                _ => false,
            }
        });
    stream_ok && until_ok && subject_ok
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn no_commitment_means_no_breach_class() {
        assert_eq!(
            classify_refusal(Commitment::None, Reason::NoSuchSubject),
            RefusalClass::NoCommitment
        );
        assert_eq!(
            classify_absence(Commitment::Lapsed),
            AbsenceClass::AttemptOnly
        );
    }

    #[test]
    fn range_commitments_need_an_ordered_pair() {
        let base = |subject: Value| json!({"evidence_stream": "s", "subject": subject, "until": "2027-01-01T00:00:00Z"});
        assert!(commitment_well_formed(&base(json!({"range": [1, 2]}))));
        assert!(!commitment_well_formed(&base(json!({"range": [3, 2]}))));
        assert!(!commitment_well_formed(&base(json!({"record": "x"}))));
        assert!(!commitment_well_formed(&base(
            json!({"record": "a".repeat(64), "range": [1, 2]})
        )));
    }
}
