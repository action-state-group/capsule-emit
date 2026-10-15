//! In-memory store, for tests and short-lived tools. It follows the contract
//! but keeps nothing across process restarts and has no transactions.

use std::collections::HashMap;
use std::sync::Mutex;

use crate::error::{ArtifactError, Result};
use crate::record::{
    check_config, has_purged, prepare, purged, storage_checksum, valid_id, verify, Record,
};
use crate::store::Store;

/// Holds prepared records by Capsule ID. Safe to share between threads.
#[derive(Debug)]
pub struct MemoryStore {
    namespace: String,
    trusted: Vec<[u8; 32]>,
    records: Mutex<HashMap<String, Record>>,
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
            records: Mutex::new(HashMap::new()),
        })
    }

    fn records(&self) -> std::sync::MutexGuard<'_, HashMap<String, Record>> {
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
        match records.get(&prepared.capsule_id) {
            None => {
                records.insert(prepared.capsule_id.clone(), prepared);
                Ok(())
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
        let stored = records.get(id).ok_or(ArtifactError::NotFound)?;
        let tombstoned = purged(stored);
        records.insert(id.to_string(), tombstoned);
        Ok(())
    }
}
