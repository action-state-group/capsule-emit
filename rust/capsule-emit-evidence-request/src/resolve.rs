//! Exact-match resolution of a well-formed request (§3.1-§3.3, §4.2).
//!
//! [`resolve`] decides, from what a responder holds, either the anchor to
//! serve the artifact under or the most specific refusal reason. It never
//! matches a subject approximately: a digest, a correlation identifier or an
//! exchange half resolves exactly or not at all (§3.1). Building and signing
//! the artifact is the responder's.
//!
//! Order of the checks (each gives the most specific reason it can):
//! 1. the derivation: not supported at all is `derivation_unsupported`;
//!    supported, but not for this subject form, is `no_such_subject`;
//! 2. the subject: not held is `no_such_subject` (or `policy_declined` for a
//!    responder that answers every such case that way, §4.2/§12); a `range`
//!    under a mechanism with no positional ordering is
//!    `coverage_unsatisfiable`;
//! 3. the coverage: an `expected_pin` must be an anchor the responder holds
//!    (for an `exchange` subject, the requester's own half is a valid pin,
//!    §3.2); a `min_freshness` needs an anchor at least that large, or
//!    issued no earlier than that time. Otherwise `coverage_unsatisfiable`:
//!    never an artifact under weaker coverage.
//!
//! A responder that could issue a fresher commitment (§3.2 "MAY perform
//! work") does so before calling [`resolve`]; this function only reads the
//! anchors it has.

use crate::registry::{Reason, SubjectForm};
use crate::request::{Coverage, Derivation, Freshness, Request, Subject};
use crate::time::{parse_utc, UtcTime};

/// A coverage anchor: a commitment (for example a checkpoint) over the
/// responder's log.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Anchor {
    pub digest: String,
    /// The log size the anchor commits to.
    pub size: u64,
    /// When the anchor was issued (RFC 3339 UTC).
    pub issued_at: String,
}

/// What the responder holds, as resolution needs to see it.
pub trait Responder {
    /// Whether a record with exactly this digest is held.
    fn holds_record(&self, digest: &str) -> bool;
    /// Whether any record carries exactly this correlation identifier.
    fn holds_correlation(&self, id: &str) -> bool;
    /// Whether any held record cites exactly this exchange half.
    fn cites_exchange(&self, half_digest: &str) -> bool;
    /// Whether the coverage mechanism defines record positions.
    fn positional_ordering(&self) -> bool;
    /// The anchors the responder holds.
    fn anchors(&self) -> Vec<Anchor>;
    /// For a derivation the responder supports, the subject forms it
    /// supports it for; `None` if it does not support the derivation.
    fn derivation_forms(&self, derivation: &Derivation) -> Option<Vec<SubjectForm>>;
    /// Whether the responder answers `policy_declined` in place of
    /// `no_such_subject`, uniformly (§4.2, §12).
    fn uniform_policy_declined(&self) -> bool {
        false
    }
}

/// Where the artifact is anchored.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResolvedAnchor {
    /// One of the responder's anchors.
    Anchor(Anchor),
    /// The requester's own exchange half, pinned by its digest (§3.2).
    ExchangeHalf(String),
}

impl ResolvedAnchor {
    /// The anchor's digest, as the response identifies it.
    pub fn digest(&self) -> &str {
        match self {
            ResolvedAnchor::Anchor(a) => &a.digest,
            ResolvedAnchor::ExchangeHalf(d) => d,
        }
    }
}

/// The resolution of a well-formed request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Resolution {
    /// Serve the artifact for the request's subject under this anchor.
    Artifact(ResolvedAnchor),
    /// Refuse with this reason.
    Refuse(Reason),
}

/// Resolve `request` against `responder`.
pub fn resolve(request: &Request, responder: &dyn Responder) -> Resolution {
    let form = request.subject.form();
    let no_such_subject = || {
        Resolution::Refuse(if responder.uniform_policy_declined() {
            Reason::PolicyDeclined
        } else {
            Reason::NoSuchSubject
        })
    };

    if let Some(derivation) = &request.derivation {
        match responder.derivation_forms(derivation) {
            None => return Resolution::Refuse(Reason::DerivationUnsupported),
            Some(forms) if !forms.contains(&form) => return no_such_subject(),
            Some(_) => {}
        }
    }

    let held = match &request.subject {
        Subject::FullHistory | Subject::Checkpoints => true,
        Subject::Record(d) => responder.holds_record(d),
        Subject::Correlation(id) => responder.holds_correlation(id),
        Subject::Exchange(d) => responder.cites_exchange(d),
        Subject::Range(..) => {
            if !responder.positional_ordering() {
                return Resolution::Refuse(Reason::CoverageUnsatisfiable);
            }
            true
        }
    };
    if !held {
        return no_such_subject();
    }

    let anchors = responder.anchors();
    let selected = match &request.coverage {
        Coverage::ExpectedPin(pin) => match &request.subject {
            Subject::Exchange(half) if half == pin => {
                Some(ResolvedAnchor::ExchangeHalf(pin.clone()))
            }
            _ => anchors
                .into_iter()
                .find(|a| &a.digest == pin)
                .map(ResolvedAnchor::Anchor),
        },
        Coverage::MinFreshness(freshness) => {
            freshest(anchors, freshness).map(ResolvedAnchor::Anchor)
        }
    };
    let Some(anchor) = selected else {
        return Resolution::Refuse(Reason::CoverageUnsatisfiable);
    };
    // Positions of a range must exist under the chosen anchor.
    if let (Subject::Range(_, end), ResolvedAnchor::Anchor(a)) = (&request.subject, &anchor) {
        if *end >= a.size {
            return Resolution::Refuse(Reason::CoverageUnsatisfiable);
        }
    }
    Resolution::Artifact(anchor)
}

/// The anchor a `min_freshness` request is served under: among the anchors
/// that qualify, the one covering the largest log (then the latest issued),
/// so the choice depends only on the anchors held, never on the caller.
fn freshest(anchors: Vec<Anchor>, freshness: &Freshness) -> Option<Anchor> {
    let issued = |a: &Anchor| parse_utc(&a.issued_at);
    anchors
        .into_iter()
        .filter(|a| match freshness {
            Freshness::Size(n) => a.size >= *n,
            Freshness::Time(t) => issued(a).is_some_and(|at: UtcTime| at >= *t),
        })
        .max_by(|a, b| (a.size, issued(a)).cmp(&(b.size, issued(b))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::request::parse_json;

    struct Held;
    const PIN: &str = "6441370955d2b408884793962a664daec6bb25e06d76a2f53d8a80f66a675202";

    impl Responder for Held {
        fn holds_record(&self, _: &str) -> bool {
            true
        }
        fn holds_correlation(&self, _: &str) -> bool {
            true
        }
        fn cites_exchange(&self, _: &str) -> bool {
            true
        }
        fn positional_ordering(&self) -> bool {
            true
        }
        fn anchors(&self) -> Vec<Anchor> {
            vec![Anchor {
                digest: PIN.into(),
                size: 3,
                issued_at: "2026-09-27T12:00:00Z".into(),
            }]
        }
        fn derivation_forms(&self, _: &Derivation) -> Option<Vec<SubjectForm>> {
            None
        }
    }

    fn resolve_json(s: &str) -> Resolution {
        resolve(&parse_json(s.as_bytes()).unwrap(), &Held)
    }

    #[test]
    fn a_range_past_the_anchor_is_not_served_under_it() {
        let inside =
            format!(r#"{{"subject":{{"range":[0,2]}},"coverage":{{"expected_pin":"{PIN}"}}}}"#);
        assert!(matches!(resolve_json(&inside), Resolution::Artifact(_)));
        let past =
            format!(r#"{{"subject":{{"range":[0,3]}},"coverage":{{"expected_pin":"{PIN}"}}}}"#);
        assert_eq!(
            resolve_json(&past),
            Resolution::Refuse(Reason::CoverageUnsatisfiable)
        );
    }

    #[test]
    fn a_pin_that_is_not_the_exchange_half_must_be_a_held_anchor() {
        let other = "b".repeat(64);
        let r = format!(
            r#"{{"subject":{{"exchange":"{}"}},"coverage":{{"expected_pin":"{other}"}}}}"#,
            "a".repeat(64)
        );
        assert_eq!(
            resolve_json(&r),
            Resolution::Refuse(Reason::CoverageUnsatisfiable)
        );
        let r = format!(
            r#"{{"subject":{{"exchange":"{}"}},"coverage":{{"expected_pin":"{PIN}"}}}}"#,
            "a".repeat(64)
        );
        assert!(matches!(
            resolve_json(&r),
            Resolution::Artifact(ResolvedAnchor::Anchor(_))
        ));
    }
}
