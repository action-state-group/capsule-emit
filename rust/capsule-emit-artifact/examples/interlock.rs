//! The Rust adapter of the cross-language artifact store interlock
//! (`.github/scripts/artifact-store-interlock.sh`). It runs one phase of
//! `test-vectors/artifact-store/interlock.json` against one store and prints
//! one report line per step; every language's adapter must print the same
//! bytes.
//!
//! ```text
//! cargo run -p capsule-emit-artifact --all-features --example interlock -- \
//!     <phase> <jsonl|sqlite|mysql> <target> <namespace> <vectors-dir>
//! ```
//!
//! `target` is a file path for jsonl and sqlite, and `host:port/database` for
//! mysql.

use std::io::Write;
use std::path::Path;

use capsule_emit_artifact::jsonl::JsonlStore;
use capsule_emit_artifact::mysql::MysqlStore;
use capsule_emit_artifact::sqlite::SqliteStore;
use capsule_emit_artifact::{encode_record, storage_checksum, Query, Record, Result, Store};
use serde_json::Value;

fn outcome<T>(result: Result<T>) -> (String, Option<T>) {
    match result {
        Ok(value) => ("ok".into(), Some(value)),
        Err(e) => match e.code() {
            Some(code) => (code.into(), None),
            None => {
                eprintln!("storage error: {e}");
                ("storage_error".into(), None)
            }
        },
    }
}

fn open(format: &str, target: &str, namespace: &str, keys: &[Vec<u8>]) -> Result<Box<dyn Store>> {
    Ok(match format {
        "jsonl" => Box::new(JsonlStore::new(target, namespace, keys)?),
        "sqlite" => {
            let conn = rusqlite::Connection::open(target).expect("open sqlite");
            conn.execute_batch("PRAGMA foreign_keys=ON;")
                .expect("pragma");
            Box::new(SqliteStore::new(conn, namespace, keys)?)
        }
        "mysql" => {
            let url = format!("mysql://root@{target}");
            let pool = mysql::Pool::new(url.as_str()).expect("mysql pool");
            Box::new(MysqlStore::new(pool, namespace, keys)?)
        }
        other => panic!("unknown format {other}"),
    })
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [phase, format, target, namespace, dir] = args.as_slice() else {
        eprintln!(
            "usage: interlock <phase> <jsonl|sqlite|mysql> <target> <namespace> <vectors-dir>"
        );
        std::process::exit(2);
    };
    let read = |name: &str| -> Value {
        serde_json::from_str(&std::fs::read_to_string(Path::new(dir).join(name)).expect(name))
            .expect(name)
    };
    let plan = read("interlock.json");
    let vectors = read("vectors.json");
    let steps = plan["phases"][phase.as_str()]
        .as_array()
        .unwrap_or_else(|| panic!("unknown phase {phase}"));
    let trusted = plan["trusted"]
        .get(phase.as_str())
        .unwrap_or(&plan["trusted"]["default"]);
    let keys: Vec<Vec<u8>> = trusted
        .as_array()
        .expect("trusted")
        .iter()
        .map(|k| hex::decode(vectors["keys"][k.as_str().unwrap()].as_str().unwrap()).unwrap())
        .collect();
    let store = open(format, target, namespace, &keys).expect("store configuration");
    if phase == "write" {
        store.init().expect("init");
    }
    let mut out = std::io::stdout().lock();
    for (i, step) in steps.iter().enumerate() {
        let op = step["op"].as_str().unwrap();
        let (id, subject) = match step.get("id") {
            Some(id) => (
                id.as_str().unwrap().to_string(),
                serde_json::to_string(id).unwrap(),
            ),
            None => {
                let name = step["record"].as_str().unwrap_or("");
                let id = vectors["records"][name]["capsule_id"]
                    .as_str()
                    .unwrap_or("");
                (id.to_string(), name.to_string())
            }
        };
        // Index steps name their paths or query in compact JSON.
        let subject = match op {
            "init_index" => serde_json::to_string(&step["paths"]).unwrap(),
            "find" => serde_json::to_string(&step["where"]).unwrap(),
            _ => subject,
        };
        let mut line = format!("{i}\t{op} {subject}\t");
        match op {
            "put" => {
                let name = step["record"].as_str().unwrap();
                // A record the codec rejects (an unknown state) is invalid input.
                line += &match serde_json::from_value::<Record>(vectors["records"][name].clone()) {
                    Ok(record) => outcome(store.put(&record)).0,
                    Err(_) => "invalid".into(),
                };
            }
            "purge" => line += &outcome(store.purge(&id)).0,
            "init_index" => {
                let paths: Vec<&str> = step["paths"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|p| p.as_str().unwrap())
                    .collect();
                let index = store.as_index_store().expect("index extension");
                line += &outcome(index.init_index(&paths)).0;
            }
            "find" => {
                // The interlock's finds fit one page; IDs print comma-separated.
                let mut query = Query {
                    limit: 1000,
                    ..Query::default()
                };
                for (path, value) in step["where"].as_object().unwrap() {
                    query
                        .r#where
                        .insert(path.clone(), value.as_str().unwrap().to_string());
                }
                let index = store.as_index_store().expect("index extension");
                let (code, page) = outcome(index.find(&query));
                line += &code;
                if let Some(page) = page.filter(|p| !p.ids.is_empty()) {
                    line += &format!("\t{}", page.ids.join(","));
                }
            }
            "get" => {
                let (code, got) = outcome(store.get(&id));
                line += &code;
                if let Some(mut got) = got {
                    let checksum = storage_checksum(&got);
                    got.artifacts
                        .sort_by(|a, b| a.name.as_bytes().cmp(b.name.as_bytes()));
                    line += &format!("\t{checksum}\t{}", encode_record(&got));
                }
            }
            other => panic!("unknown op {other}"),
        }
        writeln!(out, "{line}").unwrap();
    }
}
