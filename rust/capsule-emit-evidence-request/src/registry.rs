//! The strings -00 fixes (§3, §4.2, §13).

/// The draft these rules implement.
pub const DRAFT: &str = "draft-mih-agent-evidence-request-00";

/// The subprotocol identifier of the stream binding (§10, §13).
pub const SUBPROTOCOL: &str = "evidence-request/1";

/// The `history_card/1` derivation (§8.1, §13).
pub const DERIVATION_HISTORY_CARD: &str = "history_card/1";

/// The six subject forms (§3.1).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum SubjectForm {
    FullHistory,
    Checkpoints,
    Record,
    Range,
    Correlation,
    Exchange,
}

impl SubjectForm {
    pub const ALL: [SubjectForm; 6] = [
        SubjectForm::FullHistory,
        SubjectForm::Checkpoints,
        SubjectForm::Record,
        SubjectForm::Range,
        SubjectForm::Correlation,
        SubjectForm::Exchange,
    ];

    /// The form's token.
    pub fn token(self) -> &'static str {
        match self {
            SubjectForm::FullHistory => "full_history",
            SubjectForm::Checkpoints => "checkpoints",
            SubjectForm::Record => "record",
            SubjectForm::Range => "range",
            SubjectForm::Correlation => "correlation",
            SubjectForm::Exchange => "exchange",
        }
    }

    /// The form for an exact token; `None` for anything else.
    pub fn from_token(token: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|f| f.token() == token)
    }
}

/// The eight registered refusal reasons (§4.2, §13).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Reason {
    NotAuthorized,
    NoSuchSubject,
    CoverageUnsatisfiable,
    DerivationUnsupported,
    PolicyDeclined,
    DeadlineUnmet,
    RequestMalformed,
    RetentionExpired,
}

impl Reason {
    pub const ALL: [Reason; 8] = [
        Reason::NotAuthorized,
        Reason::NoSuchSubject,
        Reason::CoverageUnsatisfiable,
        Reason::DerivationUnsupported,
        Reason::PolicyDeclined,
        Reason::DeadlineUnmet,
        Reason::RequestMalformed,
        Reason::RetentionExpired,
    ];

    /// The registered token.
    pub fn token(self) -> &'static str {
        match self {
            Reason::NotAuthorized => "not_authorized",
            Reason::NoSuchSubject => "no_such_subject",
            Reason::CoverageUnsatisfiable => "coverage_unsatisfiable",
            Reason::DerivationUnsupported => "derivation_unsupported",
            Reason::PolicyDeclined => "policy_declined",
            Reason::DeadlineUnmet => "deadline_unmet",
            Reason::RequestMalformed => "request_malformed",
            Reason::RetentionExpired => "retention_expired",
        }
    }

    /// The reason for an exact registered token. Matching is exact:
    /// a different case, or an unregistered token such as `no_such_record`,
    /// is `None`.
    pub fn from_token(token: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|r| r.token() == token)
    }
}

impl std::fmt::Display for Reason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.token())
    }
}

/// Whether `s` is a digest as the vectors read -00: SHA-256 as 64 lowercase
/// hex characters (ambiguity A2).
pub fn is_digest(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens_round_trip_exactly() {
        for r in Reason::ALL {
            assert_eq!(Reason::from_token(r.token()), Some(r));
        }
        for f in SubjectForm::ALL {
            assert_eq!(SubjectForm::from_token(f.token()), Some(f));
        }
        for unregistered in [
            "no_such_record",
            "recorded_absence",
            "No_Such_Subject",
            "chain_segment",
            "",
        ] {
            assert_eq!(Reason::from_token(unregistered), None);
            assert_eq!(SubjectForm::from_token(unregistered), None);
        }
    }

    #[test]
    fn digests_are_64_lowercase_hex() {
        assert!(is_digest(&"a".repeat(64)));
        assert!(!is_digest(&"A".repeat(64)));
        assert!(!is_digest(&"a".repeat(63)));
        assert!(!is_digest(&"g".repeat(64)));
    }
}
