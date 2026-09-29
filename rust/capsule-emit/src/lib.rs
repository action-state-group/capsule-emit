//! Seal, sign, chain and checkpoint Agent Action Capsule records.
//!
//! A capsule is a JSON record of one action an agent took, whose id
//! (`capsule_id`) is the SHA-256 of its canonical (RFC 8785 JCS) form, signed
//! as a COSE_Sign1 statement and kept on an append-only local ledger that is
//! checkpointed as a Merkle Mountain Range. This crate produces records that
//! verify against the Python reference implementation
//! ([`capsule-emit` on PyPI](https://pypi.org/project/capsule-emit/)) and the
//! Agent Action Capsule conformance vectors.
//!
//! Modules:
//! - [`jcs`]: RFC 8785 canonicalization and JSON-DIGEST (`capsule_id`).
//! - [`capsule`]: sealing a capsule, or a local record, with caller-supplied
//!   `compute_attestation` extension members; the inline producer envelope.
//! - [`keys`]: Ed25519 key generation, loading, persistence and rotation (PEM
//!   PKCS8 / SPKI).
//! - [`cose`]: COSE_Sign1 signed statements and producer envelopes.
//! - [`ledger`]: the durable local ledger: append, restart-safe chain
//!   recovery, torn-write recovery, statement lookup, caller-defined indexes.
//! - [`checkpoint`]: signed checkpoints of the ledger's log, cut on a cadence,
//!   with optional witness registration.
//! - [`padding`]: padding records, so a checkpoint's leaf count falls on a
//!   bucket boundary (Evidence Layer -00 §12.1).
//! - [`anchor`]: an optional client for a SCITT transparency service.
//! - [`structure`]: the Class 1 checks a capsule's own bytes must pass
//!   (§6), without a store: the reference verifier's gating checks.
//! - [`verify`]: offline verification: those checks, `capsule_id`
//!   recomputation, the COSE signature and chain-parent membership.
//! - [`sequence`]: per-counterparty monotone sequence numbers and the
//!   gap/regression check over a pair's records.
//! - [`timestamp`]: minute-granular timestamps.

pub mod anchor;
pub mod capsule;
pub mod checkpoint;
pub mod cose;
pub mod jcs;
pub mod keys;
pub mod ledger;
pub mod padding;
pub mod sequence;
pub mod structure;
pub mod timestamp;
pub mod verify;
