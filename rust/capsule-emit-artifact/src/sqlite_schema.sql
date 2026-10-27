CREATE TABLE IF NOT EXISTS capsule_store_capsules (
 namespace TEXT NOT NULL,
 capsule_id TEXT NOT NULL,
 capsule_bytes BLOB NOT NULL,
 producer_envelope BLOB NOT NULL,
 record_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY (namespace, capsule_id)
);

CREATE TABLE IF NOT EXISTS capsule_store_artifacts (
 namespace TEXT NOT NULL,
 capsule_id TEXT NOT NULL,
 name TEXT NOT NULL,
 digest_field TEXT NOT NULL,
 content_bytes BLOB NULL,
 content_sha256 TEXT NOT NULL,
 retention_state TEXT NOT NULL CHECK (retention_state IN ('present','purged','never_retained')),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 purged_at TEXT NULL,
 PRIMARY KEY (namespace, capsule_id, name),
 FOREIGN KEY (namespace, capsule_id) REFERENCES capsule_store_capsules (namespace, capsule_id),
 CHECK (
   (retention_state = 'present' AND content_bytes IS NOT NULL AND length(content_sha256) = 64 AND purged_at IS NULL)
   OR (retention_state = 'purged' AND content_bytes IS NULL AND length(content_sha256) = 64 AND purged_at IS NOT NULL)
   OR (retention_state = 'never_retained' AND content_bytes IS NULL AND content_sha256 = '' AND purged_at IS NULL)
 )
);

CREATE TABLE IF NOT EXISTS capsule_store_index_paths (
 namespace TEXT NOT NULL PRIMARY KEY,
 paths TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capsule_store_index_records (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 namespace TEXT NOT NULL,
 capsule_id TEXT NOT NULL,
 ts INTEGER NULL,
 UNIQUE (namespace, capsule_id)
);

CREATE INDEX IF NOT EXISTS capsule_store_index_records_ts ON capsule_store_index_records (namespace, ts);

CREATE TABLE IF NOT EXISTS capsule_store_index (
 namespace TEXT NOT NULL,
 path TEXT NOT NULL,
 value_sha256 TEXT NOT NULL,
 seq INTEGER NOT NULL,
 PRIMARY KEY (namespace, path, value_sha256, seq)
);
