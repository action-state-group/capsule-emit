//! The backend-neutral contract, specified in `docs/artifact-store.md`.

use crate::error::Result;
use crate::record::Record;

/// The artifact store contract. Constructors stay per backend, because each
/// takes its own storage handle. Every built-in backend implements it; a
/// backend of your own conforms when it passes [`crate::conformance::run`].
pub trait Store {
    /// The storage scope the store was constructed with.
    fn namespace(&self) -> &str;
    /// Provisions storage. Idempotent; never alters existing data.
    fn init(&self) -> Result<()>;
    /// Admits an immutable record, accepting byte-identical retries.
    fn put(&self, record: &Record) -> Result<()>;
    /// Returns a stored record after fail-closed verification.
    fn get(&self, id: &str) -> Result<Record>;
    /// Erases a record's present originals, keeping their commitments.
    fn purge(&self, id: &str) -> Result<()>;
}

/// The optional transactional extension, implemented by the SQL backends:
/// `put` and `get` inside a caller-owned transaction `Tx`. `put_tx` never
/// commits or retries; on any error the caller rolls back the whole
/// transaction. With `lock`, `get_tx` serializes the read with concurrent
/// writers of the same record (MySQL `FOR UPDATE`; SQLite already serializes
/// writers).
pub trait TxStore: Store {
    /// The caller's open transaction.
    type Tx<'t>;
    /// Admits a record inside `tx`.
    fn put_tx(&self, tx: &mut Self::Tx<'_>, record: &Record) -> Result<()>;
    /// Reads a record inside `tx`.
    fn get_tx(&self, tx: &mut Self::Tx<'_>, id: &str, lock: bool) -> Result<Record>;
}
