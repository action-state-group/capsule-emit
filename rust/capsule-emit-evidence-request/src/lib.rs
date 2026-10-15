//! **Deprecated: renamed to [`evidence-request`](https://crates.io/crates/evidence-request).**
//!
//! The Evidence Request protocol (draft-mih-agent-evidence-request-00) moved
//! to agent-action-capsule, next to the draft and its conformance vectors, and
//! is published as `evidence-request` (lib `evidence_request`). This final
//! release re-exports it, so code written against 0.0.4 keeps compiling.
//! Replace the dependency with `evidence-request = "0.1"` and
//! `capsule_emit_evidence_request::` with `evidence_request::` in `use` paths.
//! No further versions of this crate will be published.

pub use evidence_request::*;
