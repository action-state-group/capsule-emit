//! The shared conformance suite on every backend, plus the cross-language
//! codec, checksum and DDL goldens.

use std::path::{Path, PathBuf};

use capsule_emit_artifact::conformance::{self, Harness, Status};
use capsule_emit_artifact::jsonl::JsonlStore;
use capsule_emit_artifact::memory::MemoryStore;
use capsule_emit_artifact::{decode_record, encode_record, storage_checksum, Store};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn assert_conformant<B>(backend: &str, harness: &Harness<B>) {
    let outcomes = conformance::run(harness);
    let failures: Vec<_> = outcomes
        .iter()
        .filter(|o| o.status == Status::Fail)
        .map(|o| format!("{}: {}", o.name, o.detail))
        .collect();
    assert!(failures.is_empty(), "{backend}:\n{}", failures.join("\n"));
    assert!(outcomes.iter().any(|o| o.status == Status::Pass));
}

#[test]
fn memory_conforms() {
    assert_conformant(
        "memory",
        &Harness {
            new_backing: Box::new(|| ()),
            open: Box::new(|_: &(), ns: &str, keys: &[Vec<u8>]| {
                Ok(Box::new(MemoryStore::new(ns, keys)?) as Box<dyn Store>)
            }),
            durable: false,
            shared_backing: false,
        },
    );
}

#[test]
fn jsonl_conforms() {
    assert_conformant(
        "jsonl",
        &Harness {
            new_backing: Box::new(|| {
                let dir = tempfile::tempdir().unwrap();
                let path = dir.path().join("artifacts.jsonl");
                (dir, path)
            }),
            open: Box::new(
                |b: &(tempfile::TempDir, PathBuf), ns: &str, keys: &[Vec<u8>]| {
                    Ok(Box::new(JsonlStore::new(&b.1, ns, keys)?) as Box<dyn Store>)
                },
            ),
            durable: true,
            shared_backing: false,
        },
    );
}

#[cfg(feature = "sqlite")]
#[test]
fn sqlite_conforms() {
    use capsule_emit_artifact::sqlite::SqliteStore;
    assert_conformant(
        "sqlite",
        &Harness {
            new_backing: Box::new(|| {
                let dir = tempfile::tempdir().unwrap();
                let path = dir.path().join("artifacts.db");
                (dir, path)
            }),
            open: Box::new(
                |b: &(tempfile::TempDir, PathBuf), ns: &str, keys: &[Vec<u8>]| {
                    let conn = rusqlite::Connection::open(&b.1).unwrap();
                    conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;")
                        .unwrap();
                    Ok(Box::new(SqliteStore::new(conn, ns, keys)?) as Box<dyn Store>)
                },
            ),
            durable: true,
            shared_backing: true,
        },
    );
}

/// Opt in with `CAPSULE_STORAGE_TEST_MYSQL=127.0.0.1:3306` (a disposable
/// MySQL 8.4 with database `capsule_storage_test` and passwordless root).
#[cfg(feature = "mysql")]
#[test]
fn mysql_conforms() {
    use capsule_emit_artifact::mysql::MysqlStore;
    use mysql::prelude::Queryable;
    let addr = match std::env::var("CAPSULE_STORAGE_TEST_MYSQL") {
        Ok(addr) => addr,
        // Under CI a missing variable fails, so MySQL coverage cannot vanish.
        Err(_) if std::env::var_os("CI").is_some() => {
            panic!("CI runs the MySQL tests: set CAPSULE_STORAGE_TEST_MYSQL")
        }
        Err(_) => {
            eprintln!("skipped: set CAPSULE_STORAGE_TEST_MYSQL for MySQL integration tests");
            return;
        }
    };
    assert!(
        addr.starts_with("127.0.0.1:"),
        "local disposable server only"
    );
    let pool =
        mysql::Pool::new(format!("mysql://root@{addr}/capsule_storage_test").as_str()).unwrap();
    assert_conformant(
        "mysql",
        &Harness {
            new_backing: Box::new(move || {
                let store = MysqlStore::new(pool.clone(), "conformance", [[0u8; 32]]).unwrap();
                store.init().unwrap();
                let mut conn = pool.get_conn().unwrap();
                for table in ["capsule_store_artifacts", "capsule_store_capsules"] {
                    conn.query_drop(format!(
                        "DELETE FROM {table} WHERE namespace IN ('conformance','alpha','beta')"
                    ))
                    .unwrap();
                }
                pool.clone()
            }),
            open: Box::new(|pool: &mysql::Pool, ns: &str, keys: &[Vec<u8>]| {
                Ok(Box::new(MysqlStore::new(pool.clone(), ns, keys)?) as Box<dyn Store>)
            }),
            durable: true,
            shared_backing: true,
        },
    );
}

#[test]
fn vectors_copy_is_byte_identical() {
    let canonical =
        std::fs::read_to_string(repo().join("test-vectors/artifact-store/vectors.json")).unwrap();
    assert_eq!(conformance::VECTORS_JSON, canonical);
}

#[test]
fn schemas_match_go() {
    for (ours, go) in [
        (
            include_str!("../src/sqlite_schema.sql"),
            "go/artifact/sqlite/schema.sql",
        ),
        (
            include_str!("../src/mysql_schema.sql"),
            "go/artifact/mysql/schema.sql",
        ),
    ] {
        assert_eq!(
            ours,
            std::fs::read_to_string(repo().join(go)).unwrap(),
            "{go}"
        );
    }
}

#[test]
fn checksum_and_line_goldens() {
    let v = conformance::vectors();
    for c in &v.checksums {
        assert_eq!(
            storage_checksum(&v.record(&c.record).unwrap()),
            c.storage_checksum,
            "{}",
            c.record
        );
    }
    let synthetic = storage_checksum(&v.record("synthetic.stored").unwrap());
    assert_eq!(
        synthetic,
        "ac78a7aa2b9206e55c1932931afa35f3f5bee9ec836724e925c8e5256cf1ddb9"
    );
    let empty = storage_checksum(&v.record("synthetic.empty").unwrap());
    assert_eq!(
        empty,
        "33be5d36fbe3fcd89e5ec0a3ac5feb0ea764fa08053c34d0978c99fc2dfb71f4"
    );
    for l in &v.lines {
        let record = v.record(&l.record).unwrap();
        assert_eq!(encode_record(&record), l.line, "{}", l.record);
        assert_eq!(
            decode_record(l.line.as_bytes()).unwrap(),
            record,
            "{}",
            l.record
        );
    }
}

#[test]
fn jsonl_put_after_torn_tail_starts_a_new_line() {
    let v = conformance::vectors();
    let keys = vec![hex::decode(&v.keys["trusted"]).unwrap()];
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.jsonl");
    let store = JsonlStore::new(&path, "ns", &keys).unwrap();
    store.init().unwrap();
    store.put(&v.record("basic").unwrap()).unwrap();
    let mut data = std::fs::read(&path).unwrap();
    data.extend_from_slice(br#"{"capsule_id":"#);
    std::fs::write(&path, &data).unwrap();
    let store = JsonlStore::new(&path, "ns", &keys).unwrap();
    store.put(&v.record("empty").unwrap()).unwrap();
    let reopened = JsonlStore::new(&path, "ns", &keys).unwrap();
    let empty = v.record("empty").unwrap();
    let got = reopened.get(&empty.capsule_id).unwrap();
    assert_eq!(
        conformance::diff(&v.record("empty.stored").unwrap(), &got),
        None
    );
}
