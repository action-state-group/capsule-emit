CREATE TABLE IF NOT EXISTS capsule_store_capsules (
 namespace VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 capsule_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 capsule_bytes LONGBLOB NOT NULL,
 producer_envelope BLOB NOT NULL,
 record_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 PRIMARY KEY (namespace, capsule_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS capsule_store_artifacts (
 namespace VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 capsule_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 name VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 digest_field VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 content_bytes LONGBLOB NULL,
 content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 retention_state ENUM('present', 'purged', 'never_retained') NOT NULL,
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 purged_at DATETIME(6) NULL,
 PRIMARY KEY (namespace, capsule_id, name),
 CONSTRAINT capsule_store_artifacts_parent FOREIGN KEY (namespace, capsule_id)
   REFERENCES capsule_store_capsules (namespace, capsule_id),
 CONSTRAINT capsule_store_artifacts_content CHECK (
   (retention_state = 'present' AND content_bytes IS NOT NULL AND CHAR_LENGTH(content_sha256) = 64 AND purged_at IS NULL)
   OR (retention_state = 'purged' AND content_bytes IS NULL AND CHAR_LENGTH(content_sha256) = 64 AND purged_at IS NOT NULL)
   OR (retention_state = 'never_retained' AND content_bytes IS NULL AND content_sha256 = '' AND purged_at IS NULL)
 )
) ENGINE=InnoDB;
