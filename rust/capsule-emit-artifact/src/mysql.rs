//! MySQL 8.4/InnoDB store (feature `mysql`), on the pure-Rust `mysql`
//! client. Same tables and rows as the Go, TypeScript and Python MySQL
//! stores. The caller owns the pool, credentials and TLS. Configure
//! `max_allowed_packet` above 8 MiB.

use std::time::Duration;

use mysql::prelude::Queryable;
use mysql::{params::Params, AccessMode, Pool, Transaction, TxOpts, Value};

use crate::error::{ArtifactError, Result};
use crate::index::{normalize_paths, IndexStore, Page, Query};
use crate::index_sql::{Dialect, Param, SqlIndex, SqlTx};
use crate::record::{
    check_config, from_rows, has_purged, prepare, storage_checksum, valid_id, verify, Record,
    StoredArtifact,
};
use crate::store::{Store, TxStore};

/// The v1 DDL, identical to `go/artifact/mysql/schema.sql`.
pub const SCHEMA: &str = include_str!("mysql_schema.sql");

static DIALECT: Dialect = Dialect {
    tables_exist: "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name IN ('capsule_store_index_paths','capsule_store_index_records','capsule_store_index')",
    lock_shared: " FOR SHARE",
    lock_update: " FOR UPDATE",
    insert_ignore: "INSERT IGNORE INTO",
};

const DUPLICATE_KEY: u16 = 1062;
const DEADLOCK: u16 = 1213;
const MAX_DEADLOCK_RETRIES: u32 = 4;

fn storage(e: mysql::Error) -> ArtifactError {
    ArtifactError::storage(e)
}

fn server_code(e: &ArtifactError) -> Option<u16> {
    match e {
        ArtifactError::Storage(inner) => match inner.downcast_ref::<mysql::Error>() {
            Some(mysql::Error::MySqlError(server)) => Some(server.code),
            _ => None,
        },
        _ => None,
    }
}

fn bytes(value: Value) -> Result<Option<Vec<u8>>> {
    match value {
        Value::NULL => Ok(None),
        Value::Bytes(b) => Ok(Some(b)),
        _ => Err(ArtifactError::Corrupt("unexpected column type".into())),
    }
}

fn text(value: Value) -> Result<String> {
    match bytes(value)? {
        Some(b) => String::from_utf8(b).map_err(|_| ArtifactError::Corrupt("text column".into())),
        None => Err(ArtifactError::Corrupt("unexpected NULL".into())),
    }
}

fn int(value: Value) -> Result<i64> {
    match value {
        Value::Int(n) => Ok(n),
        Value::UInt(n) => {
            i64::try_from(n).map_err(|_| ArtifactError::Corrupt("integer column".into()))
        }
        Value::Bytes(b) => std::str::from_utf8(&b)
            .ok()
            .and_then(|s| s.parse().ok())
            .ok_or_else(|| ArtifactError::Corrupt("integer column".into())),
        _ => Err(ArtifactError::Corrupt("unexpected column type".into())),
    }
}

fn positional(params: Vec<Param>) -> Params {
    Params::Positional(
        params
            .into_iter()
            .map(|p| match p {
                Param::Text(s) => Value::from(s),
                Param::Int(n) => Value::Int(n),
                Param::Null => Value::NULL,
            })
            .collect(),
    )
}

/// An open transaction as the index logic's [`SqlTx`].
struct Tx<'a, 't>(&'a mut Transaction<'t>);

impl SqlTx for Tx<'_, '_> {
    fn query_int(&mut self, sql: &str, params: Vec<Param>) -> Result<Option<i64>> {
        let row: Option<Value> = self
            .0
            .exec_first(sql, positional(params))
            .map_err(storage)?;
        row.map(int).transpose()
    }

    fn query_text(&mut self, sql: &str, params: Vec<Param>) -> Result<Option<String>> {
        let row: Option<Value> = self
            .0
            .exec_first(sql, positional(params))
            .map_err(storage)?;
        row.map(text).transpose()
    }

    fn exec(&mut self, sql: &str, params: Vec<Param>) -> Result<u64> {
        self.0.exec_drop(sql, positional(params)).map_err(storage)?;
        Ok(self.0.affected_rows())
    }

    fn last_insert_id(&mut self) -> Result<i64> {
        let id = self
            .0
            .last_insert_id()
            .ok_or_else(|| ArtifactError::Corrupt("no insert id".into()))?;
        i64::try_from(id).map_err(|_| ArtifactError::Corrupt("insert id".into()))
    }

    fn query_capsules(&mut self, sql: &str, params: Vec<Param>) -> Result<Vec<(String, Vec<u8>)>> {
        let rows: Vec<(Value, Value)> = self.0.exec(sql, positional(params)).map_err(storage)?;
        rows.into_iter()
            .map(|(id, capsule)| Ok((text(id)?, bytes(capsule)?.unwrap_or_default())))
            .collect()
    }

    fn query_seq_ids(&mut self, sql: &str, params: Vec<Param>) -> Result<Vec<(i64, String)>> {
        let rows: Vec<(Value, Value)> = self.0.exec(sql, positional(params)).map_err(storage)?;
        rows.into_iter()
            .map(|(seq, id)| Ok((int(seq)?, text(id)?)))
            .collect()
    }
}

/// Runs `attempt` once, then again after an InnoDB deadlock at most four more
/// times with bounded backoff. Safe only for a transaction `attempt` owns and
/// fully rolls back; `put_tx` never retries the caller's transaction.
fn retry_deadlock(mut attempt: impl FnMut() -> Result<()>) -> Result<()> {
    let mut retries = 0;
    loop {
        match attempt() {
            Err(e) if server_code(&e) == Some(DEADLOCK) && retries < MAX_DEADLOCK_RETRIES => {
                std::thread::sleep(Duration::from_millis(10 << retries));
                retries += 1;
            }
            result => return result,
        }
    }
}

/// A MySQL store over a connection pool.
#[derive(Debug, Clone)]
pub struct MysqlStore {
    pool: Pool,
    namespace: String,
    trusted: Vec<[u8; 32]>,
    lookup: std::sync::Arc<SqlIndex>,
}

impl MysqlStore {
    /// Validates the configuration; does not connect.
    pub fn new<K: AsRef<[u8]>>(
        pool: Pool,
        namespace: &str,
        trusted: impl IntoIterator<Item = K>,
    ) -> Result<Self> {
        Ok(MysqlStore {
            trusted: check_config(namespace, trusted)?,
            namespace: namespace.to_string(),
            pool,
            lookup: std::sync::Arc::new(SqlIndex::new(&DIALECT, namespace)),
        })
    }

    fn with_tx<T>(
        &self,
        opts: TxOpts,
        run: impl FnOnce(&mut Transaction<'_>) -> Result<T>,
    ) -> Result<T> {
        let mut conn = self.pool.get_conn().map_err(storage)?;
        let mut tx = conn.start_transaction(opts).map_err(storage)?;
        let value = run(&mut tx)?; // dropping tx on error rolls back
        tx.commit().map_err(storage)?;
        Ok(value)
    }

    fn read(&self, tx: &mut Transaction<'_>, id: &str, lock: bool) -> Result<Record> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let lock = if lock { " FOR UPDATE" } else { "" };
        let row: Option<(Value, Value, Value)> = tx
            .exec_first(
                format!("SELECT capsule_bytes,producer_envelope,record_sha256 FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?{lock}"),
                (&self.namespace, id),
            )
            .map_err(storage)?;
        let (capsule, producer_envelope, expected) = row.ok_or(ArtifactError::NotFound)?;
        let rows: Vec<(Value, Value, Value, Value, Value)> = tx
            .exec(
                format!("SELECT name,digest_field,content_bytes,content_sha256,retention_state FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? ORDER BY name{lock}"),
                (&self.namespace, id),
            )
            .map_err(storage)?;
        let mut artifacts = Vec::with_capacity(rows.len());
        for (name, binding, content, sha, state) in rows {
            let state = text(state)?;
            artifacts.push(StoredArtifact {
                name: text(name)?,
                binding: text(binding)?,
                content: bytes(content)?,
                content_sha256: text(sha)?,
                state,
            });
        }
        let record = from_rows(
            id,
            bytes(capsule)?.unwrap_or_default(),
            bytes(producer_envelope)?.unwrap_or_default(),
            artifacts,
            &text(expected)?,
        )?;
        verify(&record, &self.trusted)?;
        Ok(record)
    }
}

impl Store for MysqlStore {
    fn namespace(&self) -> &str {
        &self.namespace
    }

    /// Creates the v1 tables, and the index extension's, if absent. Run during provisioning, never inside
    /// an application transaction: MySQL DDL commits implicitly.
    fn init(&self) -> Result<()> {
        let mut conn = self.pool.get_conn().map_err(storage)?;
        for statement in SCHEMA.split(';').filter(|s| !s.trim().is_empty()) {
            conn.query_drop(statement).map_err(storage)?;
        }
        Ok(())
    }

    /// Admits a record in its own transaction, retrying an InnoDB deadlock at
    /// most four times with bounded backoff. [`TxStore::put_tx`] never retries.
    fn put(&self, record: &Record) -> Result<()> {
        retry_deadlock(|| self.with_tx(TxOpts::default(), |tx| self.put_tx(tx, record)))
    }

    fn get(&self, id: &str) -> Result<Record> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let opts = TxOpts::default().set_access_mode(Some(AccessMode::ReadOnly));
        self.with_tx(opts, |tx| self.read(tx, id, false))
    }

    /// Erases present originals. Requires only the record's existence in the
    /// namespace, not its integrity or signer.
    fn purge(&self, id: &str) -> Result<()> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        self.with_tx(TxOpts::default(), |tx| {
            let exists: Option<Value> = tx
                .exec_first(
                    "SELECT capsule_id FROM capsule_store_capsules WHERE namespace=? AND capsule_id=? FOR UPDATE",
                    (&self.namespace, id),
                )
                .map_err(storage)?;
            exists.ok_or(ArtifactError::NotFound)?;
            tx.exec_drop(
                "UPDATE capsule_store_artifacts SET content_bytes=NULL,retention_state='purged',purged_at=UTC_TIMESTAMP(6) WHERE namespace=? AND capsule_id=? AND retention_state='present'",
                (&self.namespace, id),
            )
            .map_err(storage)
        })
    }

    fn as_index_store(&self) -> Option<&dyn IndexStore> {
        Some(self)
    }
}

/// The declaration persists in `capsule_store_index_paths`, so every store
/// opened on the namespace indexes its writes.
impl IndexStore for MysqlStore {
    /// Provisions the tables as `init` does, then declares paths for the
    /// namespace and indexes its stored records, retrying an InnoDB deadlock
    /// as `put` does.
    fn init_index(&self, paths: &[&str]) -> Result<()> {
        normalize_paths(paths)?;
        self.init()?;
        retry_deadlock(|| {
            self.with_tx(TxOpts::default(), |tx| {
                self.lookup.declare(&mut Tx(tx), paths)
            })
        })
    }

    fn reindex(&self, paths: &[&str]) -> Result<()> {
        normalize_paths(paths)?;
        self.init()?;
        retry_deadlock(|| {
            self.with_tx(TxOpts::default(), |tx| {
                self.lookup.reindex(&mut Tx(tx), paths)
            })
        })
    }

    fn find(&self, query: &Query) -> Result<Page> {
        self.with_tx(TxOpts::default(), |tx| self.lookup.find(&mut Tx(tx), query))
    }
}

impl TxStore for MysqlStore {
    type Tx<'t> = Transaction<'t>;

    fn put_tx(&self, tx: &mut Transaction<'_>, record: &Record) -> Result<()> {
        let prepared = prepare(record)?;
        verify(&prepared, &self.trusted)?;
        let checksum = storage_checksum(&prepared);
        let inserted = tx.exec_drop(
            "INSERT INTO capsule_store_capsules (namespace,capsule_id,capsule_bytes,producer_envelope,record_sha256,created_at) VALUES(?,?,?,?,?,UTC_TIMESTAMP(6))",
            (&self.namespace, &prepared.capsule_id, &prepared.capsule, &prepared.producer_envelope, &checksum),
        );
        match inserted {
            Ok(()) => {}
            Err(mysql::Error::MySqlError(e)) if e.code == DUPLICATE_KEY => {
                let existing = self.read(tx, &prepared.capsule_id, true)?;
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
        for a in &prepared.artifacts {
            let content = match &a.content {
                Some(b) => Value::Bytes(b.clone()),
                None => Value::NULL,
            };
            tx.exec_drop(
                "INSERT INTO capsule_store_artifacts (namespace,capsule_id,name,digest_field,content_bytes,content_sha256,retention_state,created_at) VALUES(?,?,?,?,?,?,?,UTC_TIMESTAMP(6))",
                Params::Positional(vec![
                    Value::from(&self.namespace),
                    Value::from(&prepared.capsule_id),
                    Value::from(&a.name),
                    Value::from(&a.binding),
                    content,
                    Value::from(&a.content_sha256),
                    Value::from(a.state.as_str()),
                ]),
            )
            .map_err(storage)?;
        }
        self.lookup
            .admit(&mut Tx(tx), &prepared.capsule_id, &prepared.capsule)
    }

    fn get_tx(&self, tx: &mut Transaction<'_>, id: &str, lock: bool) -> Result<Record> {
        self.read(tx, id, lock)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn server_error(code: u16) -> ArtifactError {
        ArtifactError::storage(mysql::Error::MySqlError(mysql::MySqlError {
            state: "40001".into(),
            message: "test".into(),
            code,
        }))
    }

    #[test]
    fn deadlock_retries_are_bounded() {
        let mut calls = 0;
        let result = retry_deadlock(|| {
            calls += 1;
            Err(server_error(DEADLOCK))
        });
        assert_eq!(server_code(&result.unwrap_err()), Some(DEADLOCK));
        assert_eq!(calls, 1 + MAX_DEADLOCK_RETRIES);
    }

    #[test]
    fn other_errors_and_success_are_not_retried() {
        let mut calls = 0;
        let result = retry_deadlock(|| {
            calls += 1;
            Err(server_error(DUPLICATE_KEY))
        });
        assert!(result.is_err());
        assert_eq!(calls, 1);
        let mut calls = 0;
        assert!(retry_deadlock(|| {
            calls += 1;
            if calls < 3 {
                Err(server_error(DEADLOCK))
            } else {
                Ok(())
            }
        })
        .is_ok());
        assert_eq!(calls, 3);
        assert_eq!(server_code(&ArtifactError::Conflict), None);
    }
}
