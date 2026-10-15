//! Backend-specific behavior the shared suite cannot express: damaged storage,
//! transactions, strict decoding and error codes.

use capsule_emit_artifact::conformance::{self, diff};
use capsule_emit_artifact::jsonl::JsonlStore;
use capsule_emit_artifact::{
    decode_record, prepare, verify, Artifact, ArtifactError, Record, RetentionState, Store,
};

fn vectors() -> conformance::Vectors {
    conformance::vectors()
}

fn trusted(v: &conformance::Vectors) -> Vec<Vec<u8>> {
    vec![hex::decode(&v.keys["trusted"]).unwrap()]
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
