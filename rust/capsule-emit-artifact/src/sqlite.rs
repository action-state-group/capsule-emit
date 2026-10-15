//! SQLite store (feature `sqlite`), on rusqlite with a bundled SQLite. Same
//! tables and rows as the Go, TypeScript and Python SQLite stores; the file
//! may also hold a CLL SQLite log in its own tables.

use std::sync::Mutex;

use rusqlite::types::ValueRef;
use rusqlite::{params, Connection, ErrorCode, OptionalExtension, Transaction};

use crate::error::{ArtifactError, Result};
use crate::record::{
    check_config, from_rows, has_purged, prepare, storage_checksum, valid_id, verify, Record,
    StoredArtifact,
};
use crate::store::{Store, TxStore};

/// The v1 DDL, identical to `go/artifact/sqlite/schema.sql`.
pub const SCHEMA: &str = include_str!("sqlite_schema.sql");

fn storage(e: rusqlite::Error) -> ArtifactError {
    ArtifactError::storage(e)
}

/// A SQLite store that owns its connection. Open the connection with
/// `PRAGMA foreign_keys=ON` and a busy timeout. To join a transaction of your
/// own, open another connection to the same file and use [`TxStore`].
#[derive(Debug)]
pub struct SqliteStore {
    conn: Mutex<Connection>,
    namespace: String,
    trusted: Vec<[u8; 32]>,
}

impl SqliteStore {
    /// Validates the configuration; does no I/O.
    pub fn new<K: AsRef<[u8]>>(
        conn: Connection,
        namespace: &str,
        trusted: impl IntoIterator<Item = K>,
    ) -> Result<Self> {
        Ok(SqliteStore {
            trusted: check_config(namespace, trusted)?,
            namespace: namespace.to_string(),
            conn: Mutex::new(conn),
        })
    }

    fn with_tx<T>(&self, run: impl FnOnce(&Self, &Transaction<'_>) -> Result<T>) -> Result<T> {
        let mut conn = self.conn.lock().unwrap_or_else(|p| p.into_inner());
        let tx = conn.transaction().map_err(storage)?;
        let value = run(self, &tx)?; // dropping tx on error rolls back
        tx.commit().map_err(storage)?;
        Ok(value)
    }

    fn put_on(&self, tx: &Connection, record: &Record) -> Result<()> {
        let prepared = prepare(record)?;
        verify(&prepared, &self.trusted)?;
        let checksum = storage_checksum(&prepared);
        let inserted = tx.execute(
            "INSERT INTO capsule_store_capsules (namespace,capsule_id,capsule_bytes,producer_envelope,record_sha256) VALUES(?,?,?,?,?)",
            params![self.namespace, prepared.capsule_id, prepared.capsule, prepared.producer_envelope, checksum],
        );
        match inserted {
            Ok(_) => {}
            Err(rusqlite::Error::SqliteFailure(e, _))
                if e.code == ErrorCode::ConstraintViolation =>
            {
                let existing = self.read_on(tx, &prepared.capsule_id)?;
                if storage_checksum(&existing) != checksum {
                    return Err(ArtifactError::Conflict);
                }
                if has_purged(&existing) {
                    return Err(ArtifactError::Purged);
                }
                return Ok(());
            }
            Err(e) => return Err(storage(e)),
        }
        let mut insert = tx
            .prepare("INSERT INTO capsule_store_artifacts (namespace,capsule_id,name,digest_field,content_bytes,content_sha256,retention_state) VALUES(?,?,?,?,?,?,?)")
            .map_err(storage)?;
        for a in &prepared.artifacts {
            insert
                .execute(params![
                    self.namespace,
                    prepared.capsule_id,
                    a.name,
                    a.binding,
                    a.content,
                    a.content_sha256,
                    a.state.as_str()
                ])
                .map_err(storage)?;
        }
        Ok(())
    }

    fn read_on(&self, tx: &Connection, id: &str) -> Result<Record> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let (capsule, producer_envelope, expected): (Vec<u8>, Vec<u8>, String) = tx
            .query_row(
                "SELECT capsule_bytes,producer_envelope,record_sha256 FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?",
                params![self.namespace, id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .map_err(storage)?
            .ok_or(ArtifactError::NotFound)?;
        let mut statement = tx
            .prepare("SELECT name,digest_field,content_bytes,content_sha256,retention_state FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? ORDER BY name")
            .map_err(storage)?;
        let mut rows = statement
            .query(params![self.namespace, id])
            .map_err(storage)?;
        let mut artifacts = Vec::new();
        while let Some(row) = rows.next().map_err(storage)? {
            // Only SQL NULL is absent content; a zero-length BLOB is present.
            let content = match row.get_ref(2).map_err(storage)? {
                ValueRef::Null => None,
                ValueRef::Blob(b) => Some(b.to_vec()),
                _ => return Err(ArtifactError::Corrupt("content_bytes is not a BLOB".into())),
            };
            let state: String = row.get(4).map_err(storage)?;
            artifacts.push(StoredArtifact {
                name: row.get(0).map_err(storage)?,
                binding: row.get(1).map_err(storage)?,
                content,
                content_sha256: row.get(3).map_err(storage)?,
                state,
            });
        }
        let record = from_rows(id, capsule, producer_envelope, artifacts, &expected)?;
        verify(&record, &self.trusted)?;
        Ok(record)
    }
}

impl Store for SqliteStore {
    fn namespace(&self) -> &str {
        &self.namespace
    }

    /// Creates the v1 tables if absent; existing tables are untouched.
    fn init(&self) -> Result<()> {
        let conn = self.conn.lock().unwrap_or_else(|p| p.into_inner());
        conn.execute_batch(SCHEMA).map_err(storage)
    }

    fn put(&self, record: &Record) -> Result<()> {
        self.with_tx(|s, tx| s.put_on(tx, record))
    }

    fn get(&self, id: &str) -> Result<Record> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        self.with_tx(|s, tx| s.read_on(tx, id))
    }

    /// Erases present originals. Requires only the record's existence in the
    /// namespace, not its integrity or signer.
    fn purge(&self, id: &str) -> Result<()> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        self.with_tx(|s, tx| {
            let exists = tx
                .query_row(
                    "SELECT capsule_id FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?",
                    params![s.namespace, id],
                    |_| Ok(()),
                )
                .optional()
                .map_err(storage)?;
            exists.ok_or(ArtifactError::NotFound)?;
            tx.execute(
                "UPDATE capsule_store_artifacts SET content_bytes=NULL, retention_state='purged', purged_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE namespace=? AND capsule_id=? AND retention_state='present'",
                params![s.namespace, id],
            )
            .map_err(storage)?;
            Ok(())
        })
    }
}

impl TxStore for SqliteStore {
    type Tx<'t> = Transaction<'t>;

    fn put_tx(&self, tx: &mut Transaction<'_>, record: &Record) -> Result<()> {
        self.put_on(tx, record)
    }

    /// `lock` changes nothing: SQLite already serializes writers.
    fn get_tx(&self, tx: &mut Transaction<'_>, id: &str, _lock: bool) -> Result<Record> {
        self.read_on(tx, id)
    }
}
