//! Interaction outcomes and the three-state discipline (§4, §4.5).
//!
//! What the requester observed maps to exactly one recorded state. The
//! rules that keep the three outcomes apart:
//! - a signed refusal is the responder's act and is never recorded as an
//!   absence, whatever the window or its reason;
//! - silence, a transport error, or a peer that does not offer the
//!   subprotocol is never a refusal: it is pending while the waiting window
//!   is open (§4.3, A8) and a recorded absence once it closes;
//! - a retention commitment does not turn silence into a refusal (§4.4).

use crate::registry::Reason;

/// What arrived.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Received {
    Artifact {
        verified: bool,
    },
    /// A refusal object: whether its signature verifies, and its `reason`
    /// member as sent.
    Refusal {
        signature_valid: bool,
        reason: String,
    },
    Nothing,
    TransportError,
    SubprotocolNotOffered,
}

/// Whether the requester's waiting window is still open.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Window {
    Open,
    Closed,
}

/// The one state an interaction is recorded as.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    ArtifactVerified,
    ArtifactVerificationFailed,
    /// A conforming signed refusal.
    Refusal,
    /// Correctly signed, but its reason is not one registered token (A13):
    /// still the responder's signed answer, never an absence.
    RefusalNonconformant,
    /// A refusal-shaped object whose signature does not verify: not the
    /// responder's act, and not an absence either. (Not covered by the
    /// vectors.)
    RefusalSignatureInvalid,
    Pending,
    RecordedAbsence,
}

impl State {
    /// The state's name as the vectors spell it.
    pub fn token(self) -> &'static str {
        match self {
            State::ArtifactVerified => "artifact_verified",
            State::ArtifactVerificationFailed => "artifact_verification_failed",
            State::Refusal => "refusal",
            State::RefusalNonconformant => "refusal_nonconformant",
            State::RefusalSignatureInvalid => "refusal_signature_invalid",
            State::Pending => "pending",
            State::RecordedAbsence => "recorded_absence",
        }
    }
}

/// The recorded state for what was received in `window`.
pub fn record(received: &Received, window: Window) -> State {
    match received {
        Received::Artifact { verified: true } => State::ArtifactVerified,
        Received::Artifact { verified: false } => State::ArtifactVerificationFailed,
        Received::Refusal {
            signature_valid: false,
            ..
        } => State::RefusalSignatureInvalid,
        Received::Refusal { reason, .. } => match Reason::from_token(reason) {
            Some(_) => State::Refusal,
            None => State::RefusalNonconformant,
        },
        Received::Nothing | Received::TransportError | Received::SubprotocolNotOffered => {
            match window {
                Window::Open => State::Pending,
                Window::Closed => State::RecordedAbsence,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_signed_refusal_is_never_an_absence() {
        for window in [Window::Open, Window::Closed] {
            for reason in ["no_such_subject", "retention_expired", "no_such_record", ""] {
                let state = record(
                    &Received::Refusal {
                        signature_valid: true,
                        reason: reason.into(),
                    },
                    window,
                );
                assert_ne!(state, State::RecordedAbsence);
                assert_ne!(state, State::Pending);
            }
        }
    }

    #[test]
    fn silence_is_never_a_refusal() {
        for received in [
            Received::Nothing,
            Received::TransportError,
            Received::SubprotocolNotOffered,
        ] {
            assert_eq!(record(&received, Window::Open), State::Pending);
            assert_eq!(record(&received, Window::Closed), State::RecordedAbsence);
        }
    }
}
