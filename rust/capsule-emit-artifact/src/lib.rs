//! An artifact store for Agent Action Capsules: exact sealed Capsule bytes,
//! one Producer Envelope and the business originals the Capsule commits to,
//! with retention and purge. It never seals, signs or appends to a CLL log.
//!
//! The contract is shared with the Go, TypeScript and Python stores
//! (`docs/artifact-store.md` in the capsule-emit repository), and the files
//! and rows each backend writes open in the other languages.
//!
//! | Backend | Module | Feature | Transactions |
//! |---|---|---|---|
//! | One JSONL file per namespace | [`jsonl`] | default | no |
//! | Process memory | [`memory`] | default | no |
//! | SQLite (bundled) | `sqlite` | `sqlite` | yes |
//! | MySQL 8.4 | `mysql` | `mysql` | yes |
//!
//! Without features the crate links no database driver. Every backend
//! implements [`Store`] and the optional index extension [`IndexStore`]; the
//! SQL backends also implement [`TxStore`].
//! [`conformance::run`] checks any [`Store`], including your own.

pub mod conformance;
mod error;
pub mod index;
#[cfg(any(feature = "sqlite", feature = "mysql"))]
mod index_sql;
pub mod jsonl;
pub mod memory;
#[cfg(feature = "mysql")]
pub mod mysql;
mod record;
#[cfg(feature = "sqlite")]
pub mod sqlite;
mod store;

pub use error::{ArtifactError, Result};
pub use index::{IndexStore, Page, Query};
pub use record::{
    check_config, decode_record, decode_strict_json, encode_record, is_padding, prepare, purged,
    storage_checksum, valid_id, valid_name, verify, Artifact, ArtifactVerification, Record,
    RetentionState, AGENT_OUTPUT_DIGEST, EFFECT_REQUEST_DIGEST, EFFECT_RESPONSE_DIGEST,
    PADDING_RECORD_TYPE, PAYLOAD_DIGEST,
};
pub use store::{Store, TxStore};
