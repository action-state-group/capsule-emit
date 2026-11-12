CREATE TABLE IF NOT EXISTS capsule_store_capsules (
 namespace VARCHAR(64) COLLATE "C" NOT NULL,
 capsule_id VARCHAR(64) COLLATE "C" NOT NULL,
 capsule_bytes BYTEA NOT NULL,
 producer_envelope BYTEA NOT NULL,
 record_sha256 VARCHAR(64) COLLATE "C" NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY (namespace, capsule_id)
);

CREATE TABLE IF NOT EXISTS capsule_store_artifacts (
 namespace VARCHAR(64) COLLATE "C" NOT NULL,
 capsule_id VARCHAR(64) COLLATE "C" NOT NULL,
 name VARCHAR(64) COLLATE "C" NOT NULL,
 digest_field VARCHAR(96) COLLATE "C" NOT NULL,
 content_bytes BYTEA NULL,
 content_sha256 VARCHAR(64) COLLATE "C" NOT NULL,
 retention_state TEXT NOT NULL CHECK (retention_state IN ('present', 'purged', 'never_retained')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 purged_at TIMESTAMPTZ NULL,
 PRIMARY KEY (namespace, capsule_id, name),
 CONSTRAINT capsule_store_artifacts_parent FOREIGN KEY (namespace, capsule_id)
   REFERENCES capsule_store_capsules (namespace, capsule_id),
 CONSTRAINT capsule_store_artifacts_content CHECK (
   (retention_state = 'present' AND content_bytes IS NOT NULL AND length(content_sha256) = 64 AND purged_at IS NULL)
   OR (retention_state = 'purged' AND content_bytes IS NULL AND length(content_sha256) = 64 AND purged_at IS NOT NULL)
   OR (retention_state = 'never_retained' AND content_bytes IS NULL AND content_sha256 = '' AND purged_at IS NULL)
 )
);

CREATE TABLE IF NOT EXISTS capsule_store_index_paths (
 namespace VARCHAR(64) COLLATE "C" NOT NULL,
 paths TEXT COLLATE "C" NOT NULL,
 PRIMARY KEY (namespace)
);

CREATE TABLE IF NOT EXISTS capsule_store_index_records (
 seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 namespace VARCHAR(64) COLLATE "C" NOT NULL,
 capsule_id VARCHAR(64) COLLATE "C" NOT NULL,
 ts BIGINT NULL,
 CONSTRAINT capsule_store_index_records_id UNIQUE (namespace, capsule_id)
);
CREATE INDEX IF NOT EXISTS capsule_store_index_records_ts ON capsule_store_index_records (namespace, ts);

CREATE TABLE IF NOT EXISTS capsule_store_index (
 namespace VARCHAR(64) COLLATE "C" NOT NULL,
 path VARCHAR(255) COLLATE "C" NOT NULL,
 value_sha256 VARCHAR(64) COLLATE "C" NOT NULL,
 seq BIGINT NOT NULL,
 PRIMARY KEY (namespace, path, value_sha256, seq)
)
