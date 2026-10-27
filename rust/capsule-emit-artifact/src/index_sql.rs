//! The index extension's SQL logic, shared by the SQLite and MySQL backends
//! (the same SQL as `go/artifact/internal/index/sql.go`). Each backend adapts
//! its driver's transaction to [`SqlTx`] and supplies a [`Dialect`].

use std::sync::atomic::{AtomicBool, Ordering};

use crate::error::{ArtifactError, Result};
use crate::index::{check, decode_paths, encode_paths, extract, normalize_paths, Page, Query};
use crate::record::sha256_hex;

/// A statement parameter, converted to the driver's own value type.
pub(crate) enum Param {
    Text(String),
    Int(i64),
    Null,
}

impl From<&str> for Param {
    fn from(s: &str) -> Self {
        Param::Text(s.to_string())
    }
}

impl From<i64> for Param {
    fn from(n: i64) -> Self {
        Param::Int(n)
    }
}

/// The few operations the shared logic needs from a driver's transaction.
pub(crate) trait SqlTx {
    /// The first column of the first row as an integer, if any row.
    fn query_int(&mut self, sql: &str, params: Vec<Param>) -> Result<Option<i64>>;
    /// The first column of the first row as text, if any row.
    fn query_text(&mut self, sql: &str, params: Vec<Param>) -> Result<Option<String>>;
    /// Runs a statement and returns the rows it affected.
    fn exec(&mut self, sql: &str, params: Vec<Param>) -> Result<u64>;
    /// The auto-increment value of the last insert.
    fn last_insert_id(&mut self) -> Result<i64>;
    /// Rows of (text, bytes).
    fn query_capsules(&mut self, sql: &str, params: Vec<Param>) -> Result<Vec<(String, Vec<u8>)>>;
    /// Rows of (integer, text).
    fn query_seq_ids(&mut self, sql: &str, params: Vec<Param>) -> Result<Vec<(i64, String)>>;
}

/// The SQL that differs between the SQLite and MySQL backends.
#[derive(Debug)]
pub(crate) struct Dialect {
    /// Counts the index tables; it must return 3 once they exist.
    pub tables_exist: &'static str,
    /// End a SELECT that must see, and hold, the latest committed rows (MySQL
    /// `FOR SHARE` / `FOR UPDATE`; empty for SQLite, which serializes
    /// writers).
    pub lock_shared: &'static str,
    pub lock_update: &'static str,
    /// Starts an insert that skips rows already present.
    pub insert_ignore: &'static str,
}

/// The key SQL backends store for a value: SHA-256 of its UTF-8 bytes in
/// lowercase hex, so values of any length fit an index key.
pub(crate) fn value_key(value: &str) -> String {
    sha256_hex(value.as_bytes())
}

/// One SQL store's view of the index tables.
#[derive(Debug)]
pub(crate) struct SqlIndex {
    dialect: &'static Dialect,
    namespace: String,
    /// The index tables exist; cached once true, since `init` never drops them.
    exist: AtomicBool,
}

impl SqlIndex {
    pub fn new(dialect: &'static Dialect, namespace: &str) -> Self {
        SqlIndex {
            dialect,
            namespace: namespace.to_string(),
            exist: AtomicBool::new(false),
        }
    }

    fn ns(&self) -> Param {
        Param::Text(self.namespace.clone())
    }

    /// Reads the namespace's declaration inside `tx`. A database provisioned
    /// before the index tables existed has no declaration until init runs.
    fn declared(&self, tx: &mut dyn SqlTx, lock: &str) -> Result<Option<Vec<String>>> {
        if !self.exist.load(Ordering::Acquire) {
            let n = tx
                .query_int(self.dialect.tables_exist, vec![])?
                .unwrap_or(0);
            if n < 3 {
                return Ok(None);
            }
            self.exist.store(true, Ordering::Release);
        }
        let sql = format!("SELECT paths FROM capsule_store_index_paths WHERE namespace=?{lock}");
        match tx.query_text(&sql, vec![self.ns()])? {
            None => Ok(None),
            Some(stored) => decode_paths(&stored).map(Some),
        }
    }

    /// Indexes a record the caller has just inserted, when the namespace
    /// declares paths. Callers run it in the same transaction as the insert.
    pub fn admit(&self, tx: &mut dyn SqlTx, id: &str, capsule: &[u8]) -> Result<()> {
        match self.declared(tx, self.dialect.lock_shared)? {
            Some(paths) => self.add(tx, &paths, id, capsule),
            None => Ok(()),
        }
    }

    fn add(&self, tx: &mut dyn SqlTx, paths: &[String], id: &str, capsule: &[u8]) -> Result<()> {
        let extracted = extract(capsule, paths)?;
        let ts = extracted.ts.map_or(Param::Null, Param::Int);
        let sql = format!(
            "{} capsule_store_index_records (namespace,capsule_id,ts) VALUES(?,?,?)",
            self.dialect.insert_ignore
        );
        if tx.exec(&sql, vec![self.ns(), id.into(), ts])? == 0 {
            return Ok(()); // already indexed
        }
        let seq = tx.last_insert_id()?;
        for (path, value) in extracted.entries {
            tx.exec(
                "INSERT INTO capsule_store_index (namespace,path,value_sha256,seq) VALUES(?,?,?,?)",
                vec![
                    self.ns(),
                    Param::Text(path),
                    Param::Text(value_key(&value)),
                    seq.into(),
                ],
            )?;
        }
        Ok(())
    }

    /// Runs `init_index`'s declaration inside `tx`, after the caller
    /// provisioned the tables.
    pub fn declare(&self, tx: &mut dyn SqlTx, paths: &[&str]) -> Result<()> {
        let normalized = normalize_paths(paths)?;
        match self.declared(tx, self.dialect.lock_update)? {
            Some(current) if current != normalized => Err(ArtifactError::Conflict),
            Some(_) => Ok(()),
            None => {
                tx.exec(
                    "INSERT INTO capsule_store_index_paths (namespace,paths) VALUES(?,?)",
                    vec![self.ns(), Param::Text(encode_paths(&normalized))],
                )?;
                self.backfill(tx, &normalized)
            }
        }
    }

    /// Replaces the declaration and rebuilds the index inside `tx`.
    pub fn reindex(&self, tx: &mut dyn SqlTx, paths: &[&str]) -> Result<()> {
        let normalized = normalize_paths(paths)?;
        // Lock the declaration first, so a concurrent put waits for the
        // rebuild. A damaged declaration is replaced.
        match self.declared(tx, self.dialect.lock_update) {
            Ok(_) | Err(ArtifactError::Corrupt(_)) => {}
            Err(e) => return Err(e),
        }
        for sql in [
            "DELETE FROM capsule_store_index WHERE namespace=?",
            "DELETE FROM capsule_store_index_records WHERE namespace=?",
            "DELETE FROM capsule_store_index_paths WHERE namespace=?",
        ] {
            tx.exec(sql, vec![self.ns()])?;
        }
        tx.exec(
            "INSERT INTO capsule_store_index_paths (namespace,paths) VALUES(?,?)",
            vec![self.ns(), Param::Text(encode_paths(&normalized))],
        )?;
        self.backfill(tx, &normalized)
    }

    /// Indexes the stored records in creation order. A record whose Capsule
    /// bytes no longer decode is left out; `get` reports it as corrupt.
    fn backfill(&self, tx: &mut dyn SqlTx, paths: &[String]) -> Result<()> {
        let sql = format!(
            "SELECT capsule_id,capsule_bytes FROM capsule_store_capsules WHERE namespace=? ORDER BY created_at,capsule_id{}",
            self.dialect.lock_shared
        );
        for (id, capsule) in tx.query_capsules(&sql, vec![self.ns()])? {
            match self.add(tx, paths, &id, &capsule) {
                Ok(()) | Err(ArtifactError::Corrupt(_)) => {}
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }

    /// Answers `query` inside `tx`.
    pub fn find(&self, tx: &mut dyn SqlTx, query: &Query) -> Result<Page> {
        let paths = self
            .declared(tx, "")?
            .ok_or_else(|| ArtifactError::Invalid("no index paths declared".into()))?;
        let c = check(query, &paths)?;
        let mut sql = String::new();
        let mut params = vec![self.ns()];
        match c.r#where.split_first() {
            None => {
                sql.push_str("SELECT r.seq,r.capsule_id FROM capsule_store_index_records r WHERE r.namespace=? AND r.seq>?");
                params.push(c.after.into());
            }
            Some(((path, value), rest)) => {
                sql.push_str("SELECT r.seq,r.capsule_id FROM capsule_store_index i0 JOIN capsule_store_index_records r ON r.seq=i0.seq WHERE i0.namespace=? AND i0.path=? AND i0.value_sha256=? AND i0.seq>?");
                params.extend([
                    path.as_str().into(),
                    Param::Text(value_key(value)),
                    c.after.into(),
                ]);
                for (path, value) in rest {
                    sql.push_str(" AND EXISTS (SELECT 1 FROM capsule_store_index i WHERE i.namespace=r.namespace AND i.path=? AND i.value_sha256=? AND i.seq=r.seq)");
                    params.extend([path.as_str().into(), Param::Text(value_key(value))]);
                }
            }
        }
        if let Some(since) = c.since {
            sql.push_str(" AND r.ts>=?");
            params.push(since.into());
        }
        sql.push_str(" ORDER BY r.seq LIMIT ?");
        params.push((c.limit + 1).into());
        let mut page = Page::default();
        let mut last = 0;
        for (seq, id) in tx.query_seq_ids(&sql, params)? {
            if page.ids.len() as i64 == c.limit {
                page.next = Some(last.to_string());
                break;
            }
            page.ids.push(id);
            last = seq;
        }
        Ok(page)
    }
}
