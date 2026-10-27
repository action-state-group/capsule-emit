//! Backend-specific behavior the shared suite cannot express: damaged storage,
//! transactions, strict decoding and error codes.

use capsule_emit_artifact::conformance::{self, diff};
use capsule_emit_artifact::jsonl::JsonlStore;
use capsule_emit_artifact::{
    decode_record, prepare, verify, Artifact, ArtifactError, IndexStore, Query, Record,
    RetentionState, Store,
};

fn vectors() -> conformance::Vectors {
    conformance::vectors()
}

fn trusted(v: &conformance::Vectors) -> Vec<Vec<u8>> {
    vec![hex::decode(&v.keys["trusted"]).unwrap()]
}

fn id(v: &conformance::Vectors, name: &str) -> String {
    v.record(name).unwrap().capsule_id
}

fn find_ids(store: &dyn IndexStore, pairs: &[(&str, &str)]) -> Vec<String> {
    let query = Query {
        r#where: pairs
            .iter()
            .map(|(p, v)| (p.to_string(), v.to_string()))
            .collect(),
        ..Query::default()
    };
    store.find(&query).unwrap().ids
}

fn key32(v: &conformance::Vectors) -> [u8; 32] {
    hex::decode(&v.keys["trusted"]).unwrap().try_into().unwrap()
}

#[test]
fn error_codes() {
    assert_eq!(ArtifactError::Conflict.code(), Some("conflict"));
    assert_eq!(ArtifactError::NotFound.code(), Some("not_found"));
    let storage = ArtifactError::Storage(Box::new(std::io::Error::other("disk")));
    assert_eq!(storage.code(), None);
    assert!(storage.to_string().contains("disk"));
}

#[test]
fn decode_rejects_malformed_lines_as_corrupt() {
    for line in [
        "not json",
        "null",
        r#"{"capsule_id":"a","capsule":"!!","producer_envelope":"AA==","artifacts":[]}"#,
        r#"{"capsule_id":"a","capsule":"!!","producer_envelope":"AA==","artifacts":[{"name":"x","state":"gone"}]}"#,
    ] {
        assert_eq!(
            decode_record(line.as_bytes()).unwrap_err().code(),
            Some("corrupt"),
            "{line}"
        );
    }
    // A well-formed record whose state the codec cannot represent is invalid,
    // as in Go, TypeScript and Python, which carry the state as a string.
    for unknown in [
        r#"{"capsule_id":"a","capsule":"e30=","producer_envelope":"AA==","artifacts":[{"name":"x","state":"gone"}]}"#,
        r#"{"capsule_id":"a","capsule":"e30=","producer_envelope":"AA==","artifacts":[{"name":"x"}]}"#,
    ] {
        assert_eq!(
            decode_record(unknown.as_bytes()).unwrap_err().code(),
            Some("invalid"),
            "{unknown}"
        );
    }
}

#[test]
fn prepare_enforces_the_size_limits() {
    let v = vectors();
    let mut big = v.record("basic").unwrap();
    big.producer_envelope = vec![0; 65_536];
    assert_eq!(prepare(&big).unwrap_err().code(), Some("invalid"));
    let mut big = v.record("basic").unwrap();
    big.artifacts[2].content = Some(vec![b'x'; 8 * 1024 * 1024]);
    assert_eq!(prepare(&big).unwrap_err().code(), Some("invalid"));
    let mut fits = v.record("basic").unwrap();
    fits.producer_envelope = vec![0; 65_535];
    assert!(prepare(&fits).is_ok());
}

#[test]
fn jsonl_get_of_an_unknown_state_is_invalid() {
    let v = vectors();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.jsonl");
    let line = v.lines.iter().find(|l| l.record == "basic.stored").unwrap();
    let damaged = line
        .line
        .replacen(r#""state":"present""#, r#""state":"archived""#, 1);
    std::fs::write(&path, format!("{damaged}\n")).unwrap();
    let store = JsonlStore::new(&path, "ns", trusted(&v)).unwrap();
    let id = v.record("basic").unwrap().capsule_id;
    assert_eq!(store.get(&id).unwrap_err().code(), Some("invalid"));
    // The line cannot be rewritten, so purge reports it corrupt.
    assert_eq!(store.purge(&id).unwrap_err().code(), Some("corrupt"));
}

#[test]
fn verify_rejects_capsules_that_are_not_strict_json() {
    let v = vectors();
    let basic = prepare(&v.record("basic").unwrap()).unwrap();
    for capsule in [&b"\xff"[..], br#"{"a":1,"a":2}"#, b"[]", b"{} {}"] {
        let mut broken = basic.clone();
        broken.capsule = capsule.to_vec();
        assert_eq!(
            verify(&broken, &[key32(&v)]).unwrap_err().code(),
            Some("invalid")
        );
    }
    let checks = verify(&basic, &[key32(&v)]).unwrap();
    assert!(checks["payload"].verified && !checks["private_note"].bound);
}

#[test]
fn bound_content_that_is_not_strict_json_is_a_digest_mismatch() {
    let v = vectors();
    let basic = v.record("basic").unwrap();
    for content in [&br#"{"n":1.5}"#[..], br#"{"a":1,"a":1}"#, b"\xff"] {
        let mut broken = basic.clone();
        broken.artifacts[0] = Artifact {
            content: Some(content.to_vec()),
            content_sha256: String::new(),
            ..basic.artifacts[0].clone()
        };
        let err = verify(&prepare(&broken).unwrap(), &[key32(&v)]).unwrap_err();
        assert_eq!(err.code(), Some("digest_mismatch"));
    }
}

#[test]
fn jsonl_fails_closed_on_damaged_files() {
    let v = vectors();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.jsonl");
    let store = JsonlStore::new(&path, "ns", trusted(&v)).unwrap();
    assert!(matches!(
        store.put(&v.record("basic").unwrap()),
        Err(ArtifactError::Storage(_))
    ));
    store.init().unwrap();
    store.put(&v.record("basic").unwrap()).unwrap();
    let id = v.record("basic").unwrap().capsule_id;
    let data = std::fs::read(&path).unwrap();
    std::fs::write(&path, &data[..20]).unwrap();
    assert_eq!(store.get(&id).unwrap_err().code(), Some("corrupt"));
    std::fs::write(&path, vec![b'x'; data.len()]).unwrap();
    assert_eq!(store.get(&id).unwrap_err().code(), Some("corrupt"));
    std::fs::write(&path, vec![b'\n'; data.len()]).unwrap();
    assert_eq!(store.purge(&id).unwrap_err().code(), Some("corrupt"));
    assert!(std::fs::read_dir(dir.path()).unwrap().all(|e| !e
        .unwrap()
        .file_name()
        .to_string_lossy()
        .starts_with(".jsonl-rewrite-")));
}

/// Declaring index paths leaves out a line whose capsule_id is readable but
/// which no longer decodes as a record; the readable records are still found,
/// in file order, and a reopened store has no declaration until it declares.
#[test]
fn jsonl_index_skips_damaged_lines_and_is_per_store() {
    let v = vectors();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.jsonl");
    let damaged = format!("{{\"capsule_id\":\"{}\",\"capsule\":1}}\n", "ab".repeat(32));
    std::fs::write(&path, damaged).unwrap();
    let store = JsonlStore::new(&path, "ns", trusted(&v)).unwrap();
    for name in ["index.b", "index.a"] {
        store.put(&v.record(name).unwrap()).unwrap();
    }
    assert_eq!(
        store.find(&Query::default()).unwrap_err().code(),
        Some("invalid")
    );
    store.init_index(&["operator"]).unwrap();
    assert_eq!(
        find_ids(&store, &[]),
        [id(&v, "index.b"), id(&v, "index.a")]
    );
    assert_eq!(
        store.reindex(&["a", "a"]).unwrap_err().code(),
        Some("invalid")
    );
    store.reindex(&["developer"]).unwrap();
    assert_eq!(
        find_ids(&store, &[("developer", "dev-1")]),
        [id(&v, "index.b"), id(&v, "index.a")]
    );
    let reopened = JsonlStore::new(&path, "ns", trusted(&v)).unwrap();
    assert_eq!(
        reopened.find(&Query::default()).unwrap_err().code(),
        Some("invalid")
    );
    reopened.init_index(&["operator"]).unwrap();
    assert_eq!(
        find_ids(&reopened, &[("operator", "op-a")]),
        [id(&v, "index.a")]
    );
}

/// A read error partway through `init_index` leaves no declaration behind, so
/// the retry indexes every record instead of accepting a partial index.
#[cfg(unix)]
#[test]
fn jsonl_index_retry_after_failed_read() {
    use std::os::unix::fs::PermissionsExt;
    let v = vectors();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.jsonl");
    let store = JsonlStore::new(&path, "ns", trusted(&v)).unwrap();
    store.init().unwrap();
    store.put(&v.record("index.a").unwrap()).unwrap();
    let mode = |m| std::fs::set_permissions(&path, std::fs::Permissions::from_mode(m)).unwrap();
    mode(0o200);
    assert!(store.init_index(&["operator"]).is_err());
    assert!(store.reindex(&["operator"]).is_err());
    mode(0o644);
    store.init_index(&["operator"]).unwrap();
    assert_eq!(find_ids(&store, &[]), [id(&v, "index.a")]);
}

#[test]
fn every_builtin_store_reports_the_index_extension() {
    let v = vectors();
    let dir = tempfile::tempdir().unwrap();
    let jsonl = JsonlStore::new(dir.path().join("a.jsonl"), "ns", trusted(&v)).unwrap();
    let memory = capsule_emit_artifact::memory::MemoryStore::new("ns", trusted(&v)).unwrap();
    for store in [&jsonl as &dyn Store, &memory] {
        let index = store.as_index_store().unwrap();
        index.init_index(&[]).unwrap();
        assert_eq!(
            index.find(&Query::default()).unwrap().ids,
            Vec::<String>::new()
        );
        assert_eq!(
            index.init_index(&["a"]).unwrap_err().code(),
            Some("conflict")
        );
    }
}

#[test]
fn jsonl_purge_keeps_damaged_lines_and_permissions() {
    let v = vectors();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.jsonl");
    let store = JsonlStore::new(&path, "ns", trusted(&v)).unwrap();
    store.init().unwrap();
    store.put(&v.record("basic").unwrap()).unwrap();
    let mut data = std::fs::read(&path).unwrap();
    data.extend_from_slice(b"garbage\n\n{\"capsule_id\":");
    std::fs::write(&path, &data).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o640)).unwrap();
    }
    let reopened = JsonlStore::new(&path, "ns", trusted(&v)).unwrap();
    let id = v.record("basic").unwrap().capsule_id;
    reopened.purge(&id).unwrap();
    let after = std::fs::read(&path).unwrap();
    assert!(after.ends_with(b"garbage\n\n{\"capsule_id\":"));
    let purged_line = v.lines.iter().find(|l| l.record == "basic.purged").unwrap();
    assert!(after.starts_with(format!("{}\n", purged_line.line).as_bytes()));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o640
        );
    }
    let got = reopened.get(&id).unwrap();
    assert!(got
        .artifacts
        .iter()
        .all(|a| a.state != RetentionState::Present));
}

#[test]
fn jsonl_reads_lines_written_by_other_languages() {
    let v = vectors();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.jsonl");
    let lines: Vec<&str> = v
        .lines
        .iter()
        .filter(|l| l.record == "basic.stored" || l.record == "blank.stored")
        .map(|l| l.line.as_str())
        .collect();
    std::fs::write(
        &path,
        lines.iter().map(|l| format!("{l}\n")).collect::<String>(),
    )
    .unwrap();
    let store = JsonlStore::new(&path, "ns", trusted(&v)).unwrap();
    let basic: Record = v.record("basic").unwrap();
    assert_eq!(
        diff(
            &v.record("basic.stored").unwrap(),
            &store.get(&basic.capsule_id).unwrap()
        ),
        None
    );
}

#[cfg(feature = "sqlite")]
mod sqlite {
    use super::*;
    use capsule_emit_artifact::sqlite::SqliteStore;
    use capsule_emit_artifact::TxStore;

    fn open(path: &std::path::Path) -> rusqlite::Connection {
        let conn = rusqlite::Connection::open(path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        conn
    }

    #[test]
    fn tampered_rows_are_corrupt() {
        let v = vectors();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.db");
        let store = SqliteStore::new(open(&path), "ns", trusted(&v)).unwrap();
        store.init().unwrap();
        store.put(&v.record("basic").unwrap()).unwrap();
        let id = v.record("basic").unwrap().capsule_id;
        let other = open(&path);
        other
            .execute(
                "UPDATE capsule_store_artifacts SET content_bytes='text' WHERE name='private_note'",
                [],
            )
            .unwrap();
        assert_eq!(store.get(&id).unwrap_err().code(), Some("corrupt"));
        other
            .execute(
                "DELETE FROM capsule_store_artifacts WHERE name='private_note'",
                [],
            )
            .unwrap();
        assert_eq!(store.get(&id).unwrap_err().code(), Some("corrupt"));
    }

    /// The contract's storage checksum over raw rows, with the state as text.
    fn raw_checksum(record: &Record, state_of: impl Fn(&str) -> String) -> String {
        use base64::Engine;
        use sha2::Digest;
        let b64 = |b: &[u8]| base64::engine::general_purpose::STANDARD.encode(b);
        let mut artifacts: Vec<_> = record.artifacts.iter().collect();
        artifacts.sort_by(|a, b| a.name.cmp(&b.name));
        let items: Vec<String> = artifacts
            .iter()
            .map(|a| {
                let mut item = format!(r#"{{"name":"{}""#, a.name);
                if !a.binding.is_empty() {
                    item += &format!(r#","binding":"{}""#, a.binding);
                }
                item += &format!(r#","state":"{}""#, state_of(&a.name));
                if !a.content_sha256.is_empty() {
                    item += &format!(r#","content_sha256":"{}""#, a.content_sha256);
                }
                item + "}"
            })
            .collect();
        let preimage = format!(
            r#"{{"capsule_id":"{}","capsule":"{}","producer_envelope":"{}","artifacts":[{}]}}"#,
            record.capsule_id,
            b64(&record.capsule),
            b64(&record.producer_envelope),
            items.join(",")
        );
        hex::encode(sha2::Sha256::digest(preimage.as_bytes()))
    }

    #[test]
    fn unknown_states_are_checked_after_the_checksum() {
        let v = vectors();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.db");
        let store = SqliteStore::new(open(&path), "ns", trusted(&v)).unwrap();
        store.init().unwrap();
        let stored = prepare(&v.record("basic").unwrap()).unwrap();
        store.put(&stored).unwrap();
        let other = open(&path);
        other
            .execute_batch("PRAGMA ignore_check_constraints=ON;")
            .unwrap();
        other
            .execute(
                "UPDATE capsule_store_artifacts SET retention_state='archived' WHERE name='private_note'",
                [],
            )
            .unwrap();
        // The stored checksum still commits to "present": corrupt comes first.
        assert_eq!(
            store.get(&stored.capsule_id).unwrap_err().code(),
            Some("corrupt")
        );
        let checksum = raw_checksum(&stored, |name| {
            if name == "private_note" {
                "archived"
            } else {
                "present"
            }
            .to_string()
        });
        other
            .execute(
                "UPDATE capsule_store_capsules SET record_sha256=?",
                [&checksum],
            )
            .unwrap();
        // With a checksum over the stored rows, the unknown state is invalid.
        assert_eq!(
            store.get(&stored.capsule_id).unwrap_err().code(),
            Some("invalid")
        );
    }

    #[test]
    fn transactions_join_and_roll_back() {
        let v = vectors();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.db");
        let store = SqliteStore::new(open(&path), "ns", trusted(&v)).unwrap();
        store.init().unwrap();
        let basic = v.record("basic").unwrap();
        let mut conn = open(&path);
        let mut tx = conn.transaction().unwrap();
        store.put_tx(&mut tx, &basic).unwrap();
        assert_eq!(
            store
                .get_tx(&mut tx, &basic.capsule_id, true)
                .unwrap()
                .capsule_id,
            basic.capsule_id
        );
        tx.rollback().unwrap();
        assert_eq!(
            store.get(&basic.capsule_id).unwrap_err().code(),
            Some("not_found")
        );
        let mut tx = conn.transaction().unwrap();
        store.put_tx(&mut tx, &basic).unwrap();
        tx.commit().unwrap();
        assert!(store.get(&basic.capsule_id).is_ok());
    }

    fn count(path: &std::path::Path, table: &str) -> i64 {
        open(path)
            .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))
            .unwrap()
    }

    /// A database provisioned before the index tables existed keeps accepting
    /// put and gains the tables, and its records, on init_index.
    #[test]
    fn index_on_older_schema() {
        let v = vectors();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.db");
        let older = capsule_emit_artifact::sqlite::SCHEMA
            .split("\n\nCREATE TABLE IF NOT EXISTS capsule_store_index_paths")
            .next()
            .unwrap();
        assert!(!older.contains("capsule_store_index"));
        open(&path).execute_batch(older).unwrap();
        let store = SqliteStore::new(open(&path), "older", trusted(&v)).unwrap();
        store.put(&v.record("index.a").unwrap()).unwrap();
        store.put(&v.record("index.b").unwrap()).unwrap();
        assert_eq!(
            store.find(&Query::default()).unwrap_err().code(),
            Some("invalid")
        );
        store.init_index(&["operator"]).unwrap();
        assert_eq!(
            find_ids(&store, &[("operator", "op-a")]),
            [id(&v, "index.a")]
        );
        store.put(&v.record("index.c").unwrap()).unwrap();
        assert_eq!(
            find_ids(&store, &[("operator", "op-a")]),
            [id(&v, "index.a"), id(&v, "index.c")]
        );
        // Another store on the same file sees the persisted declaration.
        let other = SqliteStore::new(open(&path), "older", trusted(&v)).unwrap();
        assert_eq!(other.find(&Query::default()).unwrap().ids.len(), 3);
    }

    /// Backfill leaves out a row whose Capsule bytes no longer decode, a
    /// damaged declaration fails closed as corrupt, and reindex replaces it.
    #[test]
    fn index_damaged_rows() {
        let v = vectors();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.db");
        let store = SqliteStore::new(open(&path), "damaged", trusted(&v)).unwrap();
        store.init().unwrap();
        store.put(&v.record("index.a").unwrap()).unwrap();
        store.put(&v.record("index.b").unwrap()).unwrap();
        open(&path)
            .execute(
                "UPDATE capsule_store_capsules SET capsule_bytes=X'7B' WHERE capsule_id=?",
                [id(&v, "index.a")],
            )
            .unwrap();
        store.init_index(&["operator"]).unwrap();
        assert_eq!(find_ids(&store, &[]), [id(&v, "index.b")]);

        open(&path)
            .execute(
                "UPDATE capsule_store_index_paths SET paths='{' WHERE namespace='damaged'",
                [],
            )
            .unwrap();
        assert_eq!(
            store.find(&Query::default()).unwrap_err().code(),
            Some("corrupt")
        );
        // put fails closed too, and leaves no rows behind.
        assert_eq!(
            store.put(&v.record("index.c").unwrap()).unwrap_err().code(),
            Some("corrupt")
        );
        assert_eq!(count(&path, "capsule_store_capsules"), 2);
        open(&path)
            .execute("UPDATE capsule_store_index_paths SET paths='[\"a\",\"a\"]' WHERE namespace='damaged'", [])
            .unwrap();
        assert_eq!(
            store.find(&Query::default()).unwrap_err().code(),
            Some("corrupt")
        );
        store.reindex(&["developer"]).unwrap();
        assert_eq!(
            find_ids(&store, &[("developer", "dev-1")]),
            [id(&v, "index.b")]
        );
        for result in [store.init_index(&["a", "a"]), store.reindex(&["a", "a"])] {
            assert_eq!(result.unwrap_err().code(), Some("invalid"));
        }
    }

    /// Index rows are written in the caller's transaction: a rollback leaves
    /// none, and a retried put indexes the record once.
    #[test]
    fn index_rows_follow_the_transaction() {
        let v = vectors();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.db");
        let store = SqliteStore::new(open(&path), "ns", trusted(&v)).unwrap();
        store.init_index(&["fixture.tags"]).unwrap();
        let a = v.record("index.a").unwrap();
        let mut conn = open(&path);
        let mut tx = conn.transaction().unwrap();
        store.put_tx(&mut tx, &a).unwrap();
        tx.rollback().unwrap();
        assert_eq!(count(&path, "capsule_store_index_records"), 0);
        assert_eq!(count(&path, "capsule_store_index"), 0);
        store.put(&a).unwrap();
        store.put(&a).unwrap();
        assert_eq!(count(&path, "capsule_store_index_records"), 1);
        assert_eq!(count(&path, "capsule_store_index"), 2);
        let key: String = open(&path)
            .query_row(
                "SELECT value_sha256 FROM capsule_store_index WHERE path='fixture.tags' ORDER BY value_sha256 LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        // SHA-256 of "blue".
        assert_eq!(
            key,
            "16477688c0e00699c6cfa4497a3612d7e83c532062b64b250fed8908128ed548"
        );
        let ts: Option<i64> = open(&path)
            .query_row("SELECT ts FROM capsule_store_index_records", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert!(ts.is_some());
    }

    #[test]
    fn failed_put_leaves_no_rows() {
        let v = vectors();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.db");
        let store = SqliteStore::new(open(&path), "ns", trusted(&v)).unwrap();
        store.init().unwrap();
        open(&path)
            .execute("DROP TABLE capsule_store_artifacts", [])
            .unwrap();
        assert!(matches!(
            store.put(&v.record("basic").unwrap()),
            Err(ArtifactError::Storage(_))
        ));
        let count: i64 = open(&path)
            .query_row("SELECT COUNT(*) FROM capsule_store_capsules", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
    }
}

#[cfg(feature = "mysql")]
#[test]
fn mysql_transactions_and_tampering() {
    use capsule_emit_artifact::mysql::MysqlStore;
    use capsule_emit_artifact::TxStore;
    use mysql::prelude::Queryable;
    let Some(addr) = mysql_addr() else {
        return;
    };
    let v = vectors();
    let pool =
        mysql::Pool::new(format!("mysql://root@{addr}/capsule_storage_test").as_str()).unwrap();
    let store = MysqlStore::new(pool.clone(), "rust-tx", trusted(&v)).unwrap();
    store.init().unwrap();
    let mut conn = pool.get_conn().unwrap();
    for table in ["capsule_store_artifacts", "capsule_store_capsules"] {
        conn.query_drop(format!("DELETE FROM {table} WHERE namespace='rust-tx'"))
            .unwrap();
    }
    let basic = v.record("basic").unwrap();
    let mut tx = conn.start_transaction(mysql::TxOpts::default()).unwrap();
    store.put_tx(&mut tx, &basic).unwrap();
    assert!(store.get_tx(&mut tx, &basic.capsule_id, true).is_ok());
    tx.rollback().unwrap();
    assert_eq!(
        store.get(&basic.capsule_id).unwrap_err().code(),
        Some("not_found")
    );
    store.put(&basic).unwrap();
    conn.query_drop(
        "UPDATE capsule_store_capsules SET record_sha256=REPEAT('0',64) WHERE namespace='rust-tx'",
    )
    .unwrap();
    assert_eq!(
        store.get(&basic.capsule_id).unwrap_err().code(),
        Some("corrupt")
    );
}

#[cfg(feature = "mysql")]
#[test]
fn mysql_locked_read_blocks_purge_and_duplicate_puts_reconcile() {
    use capsule_emit_artifact::mysql::MysqlStore;
    use capsule_emit_artifact::TxStore;
    use mysql::prelude::Queryable;
    use std::sync::{mpsc, Arc};
    use std::time::Duration;
    let Some(addr) = mysql_addr() else {
        return;
    };
    let v = vectors();
    let pool =
        mysql::Pool::new(format!("mysql://root@{addr}/capsule_storage_test").as_str()).unwrap();
    let store = Arc::new(MysqlStore::new(pool.clone(), "rust-lock", trusted(&v)).unwrap());
    store.init().unwrap();
    let mut conn = pool.get_conn().unwrap();
    for table in ["capsule_store_artifacts", "capsule_store_capsules"] {
        conn.query_drop(format!("DELETE FROM {table} WHERE namespace='rust-lock'"))
            .unwrap();
    }

    // Concurrent byte-identical puts all succeed and store one record.
    let effect = v.record("effect").unwrap();
    let handles: Vec<_> = (0..6)
        .map(|_| {
            let (store, record) = (Arc::clone(&store), effect.clone());
            std::thread::spawn(move || store.put(&record))
        })
        .collect();
    for h in handles {
        h.join().unwrap().unwrap();
    }
    assert_eq!(
        diff(
            &v.record("effect.stored").unwrap(),
            &store.get(&effect.capsule_id).unwrap()
        ),
        None
    );

    // A locked read holds off a concurrent purge until its transaction ends.
    let basic = v.record("basic").unwrap();
    store.put(&basic).unwrap();
    let mut tx = conn.start_transaction(mysql::TxOpts::default()).unwrap();
    store.get_tx(&mut tx, &basic.capsule_id, true).unwrap();
    let (done, finished) = mpsc::channel();
    let purger = {
        let (store, id) = (Arc::clone(&store), basic.capsule_id.clone());
        std::thread::spawn(move || done.send(store.purge(&id)).unwrap())
    };
    assert!(
        finished.recv_timeout(Duration::from_millis(500)).is_err(),
        "purge finished while the record was locked"
    );
    let unchanged = store.get_tx(&mut tx, &basic.capsule_id, false).unwrap();
    assert_eq!(diff(&v.record("basic.stored").unwrap(), &unchanged), None);
    tx.commit().unwrap();
    finished
        .recv_timeout(Duration::from_secs(30))
        .unwrap()
        .unwrap();
    purger.join().unwrap();
    assert_eq!(
        diff(
            &v.record("basic.purged").unwrap(),
            &store.get(&basic.capsule_id).unwrap()
        ),
        None
    );
}

/// The disposable MySQL server, or None to skip; under CI a missing variable
/// fails instead, so a renamed variable cannot silently drop MySQL coverage.
/// MySQL: backfill skips undecodable bytes, a damaged declaration fails
/// closed, reindex replaces it, and a rolled-back put_tx leaves no index rows.
#[cfg(feature = "mysql")]
#[test]
fn mysql_index_damaged_rows_and_transactions() {
    use capsule_emit_artifact::mysql::MysqlStore;
    use capsule_emit_artifact::TxStore;
    use mysql::prelude::Queryable;
    let Some(addr) = mysql_addr() else {
        return;
    };
    let v = vectors();
    let pool =
        mysql::Pool::new(format!("mysql://root@{addr}/capsule_storage_test").as_str()).unwrap();
    let store = MysqlStore::new(pool.clone(), "rust-index", trusted(&v)).unwrap();
    store.init().unwrap();
    let mut conn = pool.get_conn().unwrap();
    for table in [
        "capsule_store_artifacts",
        "capsule_store_capsules",
        "capsule_store_index",
        "capsule_store_index_records",
        "capsule_store_index_paths",
    ] {
        conn.query_drop(format!("DELETE FROM {table} WHERE namespace='rust-index'"))
            .unwrap();
    }
    store.put(&v.record("index.a").unwrap()).unwrap();
    store.put(&v.record("index.b").unwrap()).unwrap();
    conn.exec_drop(
        "UPDATE capsule_store_capsules SET capsule_bytes=X'7B' WHERE namespace='rust-index' AND capsule_id=?",
        (id(&v, "index.a"),),
    )
    .unwrap();
    store.init_index(&["operator"]).unwrap();
    assert_eq!(find_ids(&store, &[]), [id(&v, "index.b")]);

    let c = v.record("index.c").unwrap();
    let mut tx = conn.start_transaction(mysql::TxOpts::default()).unwrap();
    store.put_tx(&mut tx, &c).unwrap();
    tx.rollback().unwrap();
    assert_eq!(find_ids(&store, &[]), [id(&v, "index.b")]);
    store.put(&c).unwrap();
    store.put(&c).unwrap();
    assert_eq!(
        find_ids(&store, &[]),
        [id(&v, "index.b"), id(&v, "index.c")]
    );

    conn.query_drop("UPDATE capsule_store_index_paths SET paths='{' WHERE namespace='rust-index'")
        .unwrap();
    assert_eq!(
        store.find(&Query::default()).unwrap_err().code(),
        Some("corrupt")
    );
    store.reindex(&["developer"]).unwrap();
    assert_eq!(
        find_ids(&store, &[("developer", "dev-2")]),
        [id(&v, "index.c")]
    );
    for result in [store.init_index(&["a", "a"]), store.reindex(&["a", "a"])] {
        assert_eq!(result.unwrap_err().code(), Some("invalid"));
    }
}

#[cfg(feature = "mysql")]
fn mysql_addr() -> Option<String> {
    match std::env::var("CAPSULE_STORAGE_TEST_MYSQL") {
        Ok(addr) => Some(addr),
        Err(_) if std::env::var_os("CI").is_some() => {
            panic!("CI runs the MySQL tests: set CAPSULE_STORAGE_TEST_MYSQL")
        }
        Err(_) => None,
    }
}
