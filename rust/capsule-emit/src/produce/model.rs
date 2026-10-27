//! Typed inputs and results, as Go's `model.go` defines them.
//!
//! An empty string means "absent" for every optional text member, as in Go:
//! the member is left out of the Capsule. `Option` marks optional blocks.

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use super::sign::SigningIdentity;

/// The application-owned values needed to build one Capsule. `action_id`
/// and `timestamp` are supplied explicitly, so this module never invents
/// business identity or wall-clock time. The timestamp is truncated to
/// microseconds on the wire.
#[derive(Clone, Debug, Default)]
pub struct Input {
    pub action_id: String,
    /// `fyi` or `decide` ([`super::ACTION_TYPE_FYI`], [`super::ACTION_TYPE_DECIDE`]).
    pub action_type: String,
    pub operator: String,
    pub developer: String,
    /// Required; `None` is refused.
    pub timestamp: Option<DateTime<Utc>>,
    pub epoch_id: String,
    pub domain: String,
    pub provenance: String,
    pub disposition: Option<Disposition>,
    pub effect: Option<Effect>,
    pub chain: Option<Chain>,
    /// `Some(vec![])` writes an empty `references` array; `None` omits it.
    pub references: Option<Vec<Reference>>,
    pub model: Option<Model>,
    pub compute: Option<ComputeAttestation>,
    pub provenance_mode: Option<ProvenanceMode>,
    pub extensions: Vec<Extension>,
}

/// One additional top-level Capsule member that a companion document defines.
/// `value` is JSON text; it participates in `capsule_id` like every other
/// member. Building checks only that `name` is not a base-profile member and
/// that `value` is well-formed, float-free JSON; what the member means is the
/// caller's to check.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Extension {
    pub name: String,
    pub value: Vec<u8>,
}

/// Marks a Capsule as a backfilled import of a pre-existing external event
/// (draft -05 §5.3(bis)). Mode `backfilled` requires the four companion
/// fields; `time_rung` is meaningful only when the mode is `backfilled`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ProvenanceMode {
    pub mode: String,
    pub source_ref: Option<Reference>,
    pub source_asserted_at: String,
    pub import_batch: String,
    pub imported_at: String,
    pub time_rung: String,
}

/// The provider and model that performed the recorded work. Either may be
/// empty when the application did not observe it, but not both.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Model {
    pub provider: String,
    pub model_id: String,
}

/// Binds the model invocation to its input, output and runtime. Digests are
/// AAC JSON-DIGEST values from [`super::digest_json`].
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ComputeAttestation {
    pub agent_input_digest: String,
    pub agent_output_digest: String,
    pub runtime: String,
}

/// How a decision was disposed.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Disposition {
    pub decision: String,
    pub approver: String,
    pub human_disposed: bool,
    pub verdict_class: String,
    pub reason_digest: String,
}

/// An external side effect, binding request and response JSON by digest.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Effect {
    pub effect_type: String,
    pub status: String,
    pub irreversibility_class: String,
    pub effect_attestation: String,
    pub request_digest: String,
    pub response_digest: String,
    pub external_ref: String,
}

/// Links a format-4 Capsule to one parent Capsule.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Chain {
    pub parent_capsule_id: String,
    pub relation: String,
}

/// Cites an external artifact under its own digest context (draft-04
/// §5.5.5). `log_coordinates`, when present, is an opaque recorded claim;
/// the producer does not verify its proof.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Reference {
    pub reference_type: String,
    pub digest_alg: String,
    pub digest: String,
    pub citation_purpose: String,
    pub log_coordinates: Option<Map<String, Value>>,
}

/// A validated, signature-free format-4 Capsule: its id, its members, and its
/// canonical (JCS) bytes.
#[derive(Clone, Debug, PartialEq)]
pub struct BuiltPayload {
    pub capsule_id: String,
    pub value: Map<String, Value>,
    pub json: Vec<u8>,
}

/// A signature-free Capsule and one Producer Envelope over its id.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Sealed {
    pub capsule_id: String,
    pub payload: Vec<u8>,
    pub envelope: Vec<u8>,
}

/// The one-call producer input for [`super::seal`]. For a regular Capsule,
/// `payload` is digest-committed as `agent_input_digest` and `agent_output`
/// as `agent_output_digest`; `None` leaves each out and `Some(Value::Null)`
/// commits JSON null. For a composition, set `members` to values from
/// [`super::who`], [`super::can`], [`super::did`] or [`super::audit`];
/// `payload` and `agent_output` must then be `None`.
#[derive(Clone, Debug)]
pub struct SealInput {
    pub capsule: Input,
    pub payload: Option<Value>,
    pub agent_output: Option<Value>,
    pub model: Option<Model>,
    pub runtime: String,
    pub members: Option<Vec<super::SlotMember>>,
    pub identity: SigningIdentity,
}
