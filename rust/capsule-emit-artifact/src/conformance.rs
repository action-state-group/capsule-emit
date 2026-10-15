//! The shared, language-neutral conformance suite (`test-vectors/artifact-store`,
//! bundled as a byte-identical copy). Run it against any [`Store`], built in or
//! your own:
//!
//! ```
//! use capsule_emit_artifact::{conformance, memory::MemoryStore, Store};
//!
//! let harness = conformance::Harness {
//!     new_backing: Box::new(|| ()),
//!     open: Box::new(|_: &(), namespace: &str, trusted: &[Vec<u8>]| {
//!         Ok(Box::new(MemoryStore::new(namespace, trusted)?) as Box<dyn Store>)
//!     }),
//!     durable: false,
//!     shared_backing: false,
//! };
//! let failures: Vec<_> = conformance::run(&harness)
//!     .into_iter()
//!     .filter(|o| o.status == conformance::Status::Fail)
//!     .collect();
//! assert!(failures.is_empty(), "{failures:?}");
//! ```

use std::collections::BTreeMap;

use serde::Deserialize;

use crate::error::Result;
use crate::record::Record;
use crate::store::Store;

/// The bundled copy of `test-vectors/artifact-store/vectors.json`.
pub const VECTORS_JSON: &str = include_str!("conformance_vectors.json");

/// Opens a store over a backing; must not call `init`, and returns the
/// constructor's error so the suite can check configuration validation.
pub type Open<B> = Box<dyn Fn(&B, &str, &[Vec<u8>]) -> Result<Box<dyn Store>>>;

/// Adapts one backend to the suite. `B` is the storage handle (a path, a pool).
pub struct Harness<B> {
    /// Fresh, empty storage for one case.
    pub new_backing: Box<dyn Fn() -> B>,
    /// Constructs a store over a backing.
    pub open: Open<B>,
    /// A store reopened over the same backing sees earlier writes.
    pub durable: bool,
    /// Stores with different namespaces can share one backing.
    pub shared_backing: bool,
}

/// A record transformation a [`Limits`] applies around `put` and `get`.
pub type Adapt = Box<dyn Fn(Record) -> Record>;

/// Declares a store's capability limits for [`run_limited`]. The default is
/// a store without limits, which is what [`run`] uses.
pub struct Limits {
    /// The store keeps originals. Without it, cases that put a record
    /// declaring artifacts are skipped.
    pub originals: bool,
    /// The store purges. Without it, cases that purge with any expected
    /// outcome but `invalid` are skipped.
    pub purge: bool,
    /// Applied to each record before `put`, for a store that needs an
    /// attachment of its own (the legacy ledger's signed statement).
    pub before_put: Option<Adapt>,
    /// Applied to each record `get` returns before it is compared.
    pub after_get: Option<Adapt>,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            originals: true,
            purge: true,
            before_put: None,
            after_get: None,
        }
    }
}

/// The decoded vector file. See `test-vectors/artifact-store/README.md`.
#[derive(Debug, Deserialize)]
pub struct Vectors {
    pub format: String,
    pub keys: BTreeMap<String, String>,
    pub records: BTreeMap<String, serde_json::Value>,
    pub checksums: Vec<ChecksumVector>,
    pub lines: Vec<LineVector>,
    pub open: Vec<OpenVector>,
    pub cases: Vec<Case>,
}

#[derive(Debug, Deserialize)]
pub struct ChecksumVector {
    pub record: String,
    pub storage_checksum: String,
}

#[derive(Debug, Deserialize)]
pub struct LineVector {
    pub record: String,
    pub line: String,
}

#[derive(Debug, Deserialize)]
pub struct OpenVector {
    pub name: String,
    pub namespace: String,
    pub trusted: Vec<String>,
    pub expect: String,
}

#[derive(Debug, Deserialize)]
pub struct StoreSpec {
    pub namespace: String,
    pub trusted: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct Case {
    pub name: String,
    #[serde(default)]
    pub requires: Vec<String>,
    pub stores: BTreeMap<String, StoreSpec>,
    pub steps: Vec<Step>,
}

#[derive(Debug, Deserialize)]
pub struct Step {
    pub op: String,
    #[serde(default)]
    pub store: Option<String>,
    #[serde(default)]
    pub record: Option<String>,
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub trusted: Option<Vec<String>>,
    pub expect: String,
    #[serde(default)]
    pub result: Option<String>,
}

/// Decodes the bundled vectors.
pub fn vectors() -> Vectors {
    serde_json::from_str(VECTORS_JSON).expect("bundled vectors decode")
}

impl Vectors {
    /// A named record, decoded with the JSONL wire codec. A record the codec
    /// rejects (an unknown state) is an `invalid` input.
    pub fn record(&self, name: &str) -> std::result::Result<Record, String> {
        let value = self
            .records
            .get(name)
            .ok_or_else(|| format!("unknown record {name}"))?;
        serde_json::from_value(value.clone()).map_err(|e| format!("codec: {e}"))
    }

    fn keys(&self, names: &[String]) -> Vec<Vec<u8>> {
        names
            .iter()
            .map(|n| hex::decode(&self.keys[n]).expect("vector key is hex"))
            .collect()
    }
}

/// Whether a test passed, failed or was skipped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    Pass,
    Fail,
    Skip,
}

/// The outcome of one constructor vector (`open/<name>`) or case.
#[derive(Debug, Clone)]
pub struct Outcome {
    pub name: String,
    pub status: Status,
    pub detail: String,
}

fn code<T>(result: &Result<T>) -> &'static str {
    match result {
        Ok(_) => "ok",
        Err(e) => e.code().unwrap_or("storage"),
    }
}

fn expect<T>(label: &str, want: &str, got: &Result<T>) -> std::result::Result<(), String> {
    if code(got) == want {
        return Ok(());
    }
    let detail = match got {
        Ok(_) => "ok".to_string(),
        Err(e) => e.to_string(),
    };
    Err(format!(
        "{label}: want {want}, got {} ({detail})",
        code(got)
    ))
}

/// Compares records as the contract defines equality: artifact order is
/// ignored, and absent content differs from zero-length content. Returns
/// `None` when they are equal.
pub fn diff(want: &Record, got: &Record) -> Option<String> {
    if want.capsule_id != got.capsule_id {
        return Some(format!(
            "capsule_id {} != {}",
            got.capsule_id, want.capsule_id
        ));
    }
    if want.capsule != got.capsule {
        return Some("capsule bytes differ".into());
    }
    if want.producer_envelope != got.producer_envelope {
        return Some("producer_envelope bytes differ".into());
    }
    if want.artifacts.len() != got.artifacts.len() {
        return Some(format!(
            "{} artifacts, want {}",
            got.artifacts.len(),
            want.artifacts.len()
        ));
    }
    for w in &want.artifacts {
        match got.artifacts.iter().find(|g| g.name == w.name) {
            None => return Some(format!("missing artifact {}", w.name)),
            Some(g) if g != w => return Some(format!("artifact {} differs: {g:?}", w.name)),
            Some(_) => {}
        }
    }
    None
}

fn run_open<B>(h: &Harness<B>, v: &OpenVector) -> std::result::Result<(), String> {
    let keys: Vec<Vec<u8>> = v
        .trusted
        .iter()
        .map(|k| hex::decode(k).map_err(|e| e.to_string()))
        .collect::<std::result::Result<_, _>>()?;
    let backing = (h.new_backing)();
    let opened = (h.open)(&backing, &v.namespace, &keys);
    expect("open", &v.expect, &opened)?;
    match opened {
        Ok(store) if store.namespace() != v.namespace => Err(format!(
            "namespace {:?}, want {:?}",
            store.namespace(),
            v.namespace
        )),
        _ => Ok(()),
    }
}

fn run_case<B>(
    h: &Harness<B>,
    limits: &Limits,
    v: &Vectors,
    c: &Case,
) -> std::result::Result<(), String> {
    let backing = (h.new_backing)();
    let mut stores: BTreeMap<String, Box<dyn Store>> = BTreeMap::new();
    let open = |name: &str, trusted: &[String]| -> std::result::Result<Box<dyn Store>, String> {
        let spec = &c.stores[name];
        let store = (h.open)(&backing, &spec.namespace, &v.keys(trusted))
            .map_err(|e| format!("open store {name}: {e}"))?;
        for _ in 0..2 {
            store
                .init()
                .map_err(|e| format!("init store {name}: {e}"))?;
        }
        if store.namespace() != spec.namespace {
            return Err(format!(
                "namespace {:?}, want {:?}",
                store.namespace(),
                spec.namespace
            ));
        }
        Ok(store)
    };
    for (name, spec) in &c.stores {
        stores.insert(name.clone(), open(name, &spec.trusted)?);
    }
    for (i, s) in c.steps.iter().enumerate() {
        let name = s.store.clone().unwrap_or_else(|| "main".to_string());
        let id = s.id.clone().unwrap_or_default();
        let label = format!(
            "step {i} ({} {}{})",
            s.op,
            s.record.as_deref().unwrap_or(""),
            id
        );
        match s.op.as_str() {
            "put" => {
                let record_name = s.record.as_deref().unwrap_or_default();
                match v.record(record_name) {
                    Ok(record) => {
                        let record = match &limits.before_put {
                            Some(adapt) => adapt(record),
                            None => record,
                        };
                        expect(&label, &s.expect, &stores[&name].put(&record))?
                    }
                    Err(codec) if s.expect == "invalid" && codec.starts_with("codec:") => {}
                    Err(e) => return Err(format!("{label}: {e}")),
                }
            }
            "get" => {
                let got = stores[&name].get(&id);
                expect(&label, &s.expect, &got)?;
                if let Ok(record) = got {
                    let record = match &limits.after_get {
                        Some(adapt) => adapt(record),
                        None => record,
                    };
                    let result = s.result.as_deref().unwrap_or_default();
                    let want = v.record(result).map_err(|e| format!("{label}: {e}"))?;
                    if let Some(d) = diff(&want, &record) {
                        return Err(format!("{label}: differs from {result}: {d}"));
                    }
                }
            }
            "purge" => expect(&label, &s.expect, &stores[&name].purge(&id))?,
            "reopen" => {
                let trusted = s
                    .trusted
                    .clone()
                    .unwrap_or_else(|| c.stores[&name].trusted.clone());
                let store = open(&name, &trusted)?;
                stores.insert(name, store);
            }
            other => return Err(format!("{label}: unknown op {other}")),
        }
    }
    Ok(())
}

/// The capabilities `case` needs: its `requires` plus two derived from its
/// steps, `originals` when it puts a record that declares artifacts and
/// `purge` when it purges with any expected outcome but `invalid`.
pub fn needs(v: &Vectors, case: &Case) -> Vec<String> {
    let mut required = case.requires.clone();
    let declares_artifacts = |name: &str| {
        v.records[name]["artifacts"]
            .as_array()
            .is_some_and(|a| !a.is_empty())
    };
    if case
        .steps
        .iter()
        .any(|s| s.op == "put" && declares_artifacts(s.record.as_deref().unwrap_or_default()))
    {
        required.push("originals".into());
    }
    if case
        .steps
        .iter()
        .any(|s| s.op == "purge" && s.expect != "invalid")
    {
        required.push("purge".into());
    }
    required
}

/// Runs every constructor vector and case and returns one outcome each.
pub fn run<B>(h: &Harness<B>) -> Vec<Outcome> {
    run_limited(h, &Limits::default())
}

/// [`run`] for a store with capability limits: cases needing a capability the
/// store lacks are skipped.
pub fn run_limited<B>(h: &Harness<B>, limits: &Limits) -> Vec<Outcome> {
    let v = vectors();
    let mut outcomes = Vec::new();
    for o in &v.open {
        outcomes.push(outcome(format!("open/{}", o.name), run_open(h, o)));
    }
    for c in &v.cases {
        let required = needs(&v, c);
        let missing = required.iter().find(|r| match r.as_str() {
            "durable" => !h.durable,
            "shared_backing" => !h.shared_backing,
            "originals" => !limits.originals,
            "purge" => !limits.purge,
            _ => true,
        });
        if let Some(r) = missing {
            outcomes.push(Outcome {
                name: c.name.clone(),
                status: Status::Skip,
                detail: format!("backend lacks capability {r}"),
            });
            continue;
        }
        outcomes.push(outcome(c.name.clone(), run_case(h, limits, &v, c)));
    }
    outcomes
}

fn outcome(name: String, result: std::result::Result<(), String>) -> Outcome {
    match result {
        Ok(()) => Outcome {
            name,
            status: Status::Pass,
            detail: String::new(),
        },
        Err(detail) => Outcome {
            name,
            status: Status::Fail,
            detail,
        },
    }
}
