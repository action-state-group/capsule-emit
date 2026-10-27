//! In-memory store, for tests and short-lived tools. It follows the contract
//! but keeps nothing across process restarts and has no transactions.

use std::collections::HashMap;
use std::sync::Mutex;

use crate::error::{ArtifactError, Result};
use crate::index::{normalize_paths, IndexStore, MemoryIndex, Page, Query};
use crate::record::{
    check_config, has_purged, prepare, purged, storage_checksum, valid_id, verify, Record,
};
use crate::store::Store;

/// Holds prepared records by Capsule ID. Safe to share between threads.
#[derive(Debug)]
pub struct MemoryStore {
    namespace: String,
    trusted: Vec<[u8; 32]>,
    records: Mutex<Records>,
}

#[derive(Debug, Default)]
struct Records {
    by_id: HashMap<String, Record>,
    /// Capsule IDs in admission order.
    order: Vec<String>,
    lookup: MemoryIndex,
}

impl Records {
    fn index_all(&mut self) -> Result<()> {
        for id in &self.order {
            self.lookup.add(id, &self.by_id[id].capsule)?;
        }
        Ok(())
    }
}

impl MemoryStore {
    /// Validates the namespace and trusted Ed25519 public keys.
    pub fn new<K: AsRef<[u8]>>(
        namespace: &str,
        trusted: impl IntoIterator<Item = K>,
    ) -> Result<Self> {
        Ok(MemoryStore {
            trusted: check_config(namespace, trusted)?,
            namespace: namespace.to_string(),
            records: Mutex::new(Records::default()),
        })
    }

    fn records(&self) -> std::sync::MutexGuard<'_, Records> {
        self.records
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl Store for MemoryStore {
    fn namespace(&self) -> &str {
        &self.namespace
    }

    fn init(&self) -> Result<()> {
        Ok(())
    }

    fn put(&self, record: &Record) -> Result<()> {
        let prepared = prepare(record)?;
        verify(&prepared, &self.trusted)?;
        let checksum = storage_checksum(&prepared);
        let mut records = self.records();
        // An existing record was verified with these same keys when it was
        // admitted, and memory cannot be altered behind the store.
        match records.by_id.get(&prepared.capsule_id) {
            None => {
                let records = &mut *records;
                let id = prepared.capsule_id.clone();
                records.order.push(id.clone());
                let added = records.lookup.add(&id, &prepared.capsule);
                records.by_id.insert(id, prepared);
                added
            }
            Some(existing) if storage_checksum(existing) != checksum => {
                Err(ArtifactError::Conflict)
            }
            Some(existing) if has_purged(existing) => Err(ArtifactError::Purged),
            Some(_) => Ok(()),
        }
    }

    fn get(&self, id: &str) -> Result<Record> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let record = self
            .records()
            .by_id
            .get(id)
            .cloned()
            .ok_or(ArtifactError::NotFound)?;
        verify(&record, &self.trusted)?;
        Ok(record)
    }

    fn purge(&self, id: &str) -> Result<()> {
        if !valid_id(id) {
            return Err(ArtifactError::Invalid("capsule id".into()));
        }
        let mut records = self.records();
        let stored = records.by_id.get(id).ok_or(ArtifactError::NotFound)?;
        let tombstoned = purged(stored);
        records.by_id.insert(id.to_string(), tombstoned);
        Ok(())
    }

    fn as_index_store(&self) -> Option<&dyn IndexStore> {
        Some(self)
    }
}

/// The declaration and the index live in this store only.
impl IndexStore for MemoryStore {
    /// Declares paths and indexes the records already stored.
    fn init_index(&self, paths: &[&str]) -> Result<()> {
        let mut records = self.records();
        if records.lookup.declare(paths)? {
            records.index_all()?;
        }
        Ok(())
    }

    fn reindex(&self, paths: &[&str]) -> Result<()> {
        let normalized = normalize_paths(paths)?;
        let mut records = self.records();
        records.lookup.reset(normalized);
        records.index_all()
    }

    fn find(&self, query: &Query) -> Result<Page> {
        self.records().lookup.find(query)
    }
}
