//! The evidence request protocol of draft-mih-agent-evidence-request-00.
//!
//! A requester asks a responder for evidence about a **subject**, under a
//! **coverage** constraint. The interaction ends in exactly one of three
//! outcomes: an artifact, a signed refusal, or the requester's own record
//! that nothing arrived (a recorded absence).
//!
//! This crate implements what both sides need to agree on:
//!
//! - [`request`]: parse a request from its JSON or CBOR binding and check it
//!   against -00 (the six subject forms, exactly one coverage member, the
//!   optional fields), giving the exact refusal reason when it is not
//!   well formed.
//! - [`digest`]: the request digest, SHA-256 over the bytes as received.
//! - [`resolve`]: exact-match resolution of a well-formed request against a
//!   responder's holdings ([`resolve::Responder`]): the anchor to serve
//!   under, or the most specific refusal reason. Building the artifact
//!   itself is the responder's business.
//! - [`refusal`]: sign a refusal; authenticate one as a responder's answer
//!   to a request ([`refusal::verify_for`]); or check its shape and
//!   self-consistency ([`refusal::check`], not authentication).
//! - [`outcome`]: map what the requester observed to one recorded state,
//!   under the three-state discipline (a refusal is never an absence).
//! - [`invariance`]: caller invariance of artifacts.
//! - [`retention`]: retention commitments and the breach table.
//!
//! The conformance vectors of the draft (pinned in `tests/vectors/`) are the
//! test suite. Where -00 leaves a wire shape open, this crate follows the
//! reading the vectors document (their ambiguity notes A1-A14); the items a
//! future revision may change are marked in each module.
//!
//! - [`answer`] (since 0.0.2): build the artifact response over a
//!   checkpointed local log (records, a range or the full history with
//!   inclusion or range proofs, the checkpoints, the `history_card/1`
//!   derivation with consistency proofs), signed by the responder and bound
//!   to the request; and verify one offline against the responder's key and
//!   the request sent.
//!
//! To accept a refusal as the answer to a request, use
//! [`refusal::verify_for`]; [`refusal::check`] is not authentication. To
//! accept an artifact, use [`answer::verify`].
//!
//! Out of scope: transport, storage, and any store-and-forward or delivery
//! guarantee.

pub mod answer;
pub mod digest;
pub mod invariance;
pub mod jcs;
pub mod outcome;
pub mod receipt;
pub mod refusal;
pub mod registry;
pub mod request;
pub mod resolve;
pub mod retention;
pub mod time;
mod value;
