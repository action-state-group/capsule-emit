//! Protocol constants and wire vocabulary, as Go's `constants.go` names them.
//!
//! Registry-backed values (decision, verdict class, effect type,
//! irreversibility class, effect attestation, chain relation, domain,
//! provenance) are extensible: a value outside these constants is a Class 1
//! informational finding, never a rejection. Action type, approver,
//! provenance mode and time rung are closed sets that [`super::build`]
//! enforces.

/// The only canonicalization profile emitted for format-4 Capsules.
pub use crate::capsule::CANONICALIZATION_ID;
/// The AAC serialization-suite version.
pub use crate::capsule::FORMAT_VERSION;
/// The `spec_version` every Capsule this module builds carries.
pub use crate::capsule::SPEC_VERSION;

/// The Producer Envelope payload media type.
pub const CONTENT_TYPE: &str = crate::cose::CAPSULE_ID_MEDIA_TYPE;

/// The `spec_version` values a verifier must accept: the draft revisions that
/// define format 4. Informational only: `spec_version` selects no digest or
/// verification algorithm, so [`super::verify_capsule`] never branches on it.
pub const ACCEPTED_SPEC_VERSIONS: [&str; 2] =
    ["draft-mih-scitt-agent-action-capsule-04", SPEC_VERSION];

pub const ACTION_TYPE_FYI: &str = "fyi";
pub const ACTION_TYPE_DECIDE: &str = "decide";

pub const DECISION_ACCEPT: &str = "accept";
pub const DECISION_REJECT: &str = "reject";
pub const DECISION_NEEDS_INPUT: &str = "needs_input";
pub const DECISION_DEFERRED: &str = "deferred";

pub const APPROVER_HUMAN: &str = "human";
pub const APPROVER_POLICY: &str = "policy";
pub const APPROVER_COUNTERPARTY: &str = "counterparty";

pub const VERDICT_EXECUTED: &str = "executed";
pub const VERDICT_BLOCKED: &str = "blocked";
pub const VERDICT_HITL_DISPATCHED: &str = "hitl_dispatched";
pub const VERDICT_DENIED: &str = "denied";
pub const VERDICT_TIMEOUT: &str = "timeout";
pub const VERDICT_ERRORED: &str = "errored";
pub const VERDICT_ENGINE_FAILURE: &str = "engine_failure";
pub const VERDICT_DEFERRED: &str = "deferred";
pub const VERDICT_NEEDS_DECISION: &str = "needs_decision";
pub const VERDICT_EXPIRED: &str = "expired";
pub const VERDICT_ESCALATED: &str = "escalated";
pub const VERDICT_RESOLVED: &str = "resolved";
pub const VERDICT_EPOCH_BOUNDARY: &str = "epoch_boundary";

pub const EFFECT_PLANNED: &str = "planned";
pub const EFFECT_DISPATCHED: &str = "dispatched";
pub const EFFECT_CONFIRMED: &str = "confirmed";
pub const EFFECT_FAILED: &str = "failed";
pub const EFFECT_REVERTED: &str = "reverted";

pub const IRREVERSIBILITY_TWO_WAY: &str = "two_way";
pub const IRREVERSIBILITY_ONE_WAY_RECOVERABLE: &str = "one_way_recoverable";
pub const IRREVERSIBILITY_ONE_WAY_CONSEQUENTIAL: &str = "one_way_consequential";
pub const IRREVERSIBILITY_ONE_WAY_TERMINAL: &str = "one_way_terminal";

pub const ATTESTATION_GATE_EXECUTED: &str = "gate_executed";
pub const ATTESTATION_RUNTIME_CLAIMED: &str = "runtime_claimed";

pub const CHAIN_CONFIRMS: &str = "confirms";
pub const CHAIN_SUPERSEDES: &str = "supersedes";
pub const CHAIN_EPOCH_OPENS: &str = "epoch_opens";
/// Links a backfilled Capsule to the contemporaneous twin it duplicates
/// (draft -05 §5.3(bis)).
pub const CHAIN_DUPLICATES: &str = "duplicates";

pub const EFFECT_MODE_NOT_APPLICABLE: &str = "not_applicable";
pub const EFFECT_MODE_DISPATCHED_UNCONFIRMED: &str = "dispatched_unconfirmed";
pub const EFFECT_MODE_CONFIRMED: &str = "confirmed";
pub const ATTESTATION_MODE_SELF_ATTESTED: &str = "self_attested";
pub const ATTESTATION_MODE_ANCHORED: &str = "anchored";
pub const LEDGER_MODE_STANDALONE: &str = "standalone";
pub const LEDGER_MODE_CHAINED: &str = "chained";
pub const LEDGER_MODE_ANCHORED: &str = "anchored";

pub const DOMAIN_ACTION: &str = "action";
pub const DOMAIN_MEMORY: &str = "memory";
pub const DOMAIN_REASONING: &str = "reasoning";

pub const PROVENANCE_GATE: &str = "gate";
pub const PROVENANCE_RUNTIME: &str = "runtime";
pub const PROVENANCE_COLLECTOR: &str = "collector";

pub const PROVENANCE_MODE_CONTEMPORANEOUS: &str = "contemporaneous";
pub const PROVENANCE_MODE_BACKFILLED: &str = "backfilled";

pub const TIME_RUNG_SELF_ATTESTED: &str = "self_attested";
pub const TIME_RUNG_WITNESSED: &str = "witnessed";

/// Whether `value` is an irreversibility class AAC seeds, per the
/// agent-action-capsule crate's authoritative registry.
pub fn is_v4_irreversibility_class(value: &str) -> bool {
    agent_action_capsule::registries::Registries::authoritative()
        .map(|registries| registries.contains("irreversibility_class", value))
        .unwrap_or(false)
}
