// MySQL artifact backend on mysql2 (MySQL 8.4/InnoDB). It mirrors the SQLite
// backend's contract: immutable records, byte-identical retry admission, and
// fail-closed reads that verify signer and original digests. The caller owns
// the pool, credentials, TLS, and trusted keys.
import type { Pool, PoolConnection, RowDataPacket } from "mysql2/promise";

import {
  ArtifactError,
  prepare,
  storageChecksum,
  verify,
  type Artifact,
  type Record,
  type RetentionState,
  type Store,
} from "./record.js";

const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u;
const ID_PATTERN = /^[0-9a-f]{64}$/u;

const SCHEMA = `
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
`;

const DUPLICATE_KEY = 1062;
const DEADLOCK = 1213;

function errno(error: unknown): number | undefined {
  return (error as { errno?: number }).errno;
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function toBytes(value: Buffer): Uint8Array {
  return new Uint8Array(value);
}

/**
 * An application-importable SDK, independent of CLI profiles and CLL log IDs.
 * A namespace isolates a collection; it must not implicitly be a log ID.
 */
export class MysqlArtifactStore implements Store {
  private readonly trusted: Uint8Array[];

  public constructor(
    private readonly pool: Pool,
    public readonly namespace: string,
    trusted: readonly Uint8Array[],
  ) {
    if (!NAME_PATTERN.test(namespace) || trusted.length === 0)
      throw new ArtifactError("invalid", "store configuration");
    this.trusted = [];
    for (const key of trusted) {
      if (key.length !== 32)
        throw new ArtifactError("invalid", "public key size");
      this.trusted.push(Uint8Array.from(key));
    }
  }

  /**
   * Creates v1 tables. Run explicitly during provisioning, never within an
   * application transaction: MySQL DDL implicitly commits.
   */
  public async init(): Promise<void> {
    for (const statement of SCHEMA.split(";")) {
      if (statement.trim() === "") continue;
      await this.pool.query(statement);
    }
  }

  /** Persists an immutable record atomically, accepting byte-identical retries. */
  public async put(record: Record): Promise<void> {
    for (let attempt = 0; ; attempt += 1) {
      const connection = await this.pool.getConnection();
      try {
        await connection.beginTransaction();
        await this.putTx(connection, record);
        await connection.commit();
        return;
      } catch (error) {
        await connection.rollback();
        if (errno(error) !== DEADLOCK || attempt >= 4) throw error;
      } finally {
        connection.release();
      }
      // Deadlock: the connection is already released above, so we do not hold
      // a pool slot while waiting. Mirror Go retryDeadlock: exponential backoff
      // before replaying our own fully rolled-back transaction on a fresh
      // connection.
      await sleep((1 << attempt) * 10);
    }
  }

  /**
   * Joins a caller-owned transaction on this store's database. The caller MUST
   * roll the whole transaction back on error; this method never commits.
   */
  public async putTx(
    connection: PoolConnection,
    record: Record,
  ): Promise<void> {
    const prepared = prepare(record);
    await verify(prepared, this.trusted);
    const hash = storageChecksum(prepared);
    try {
      await connection.query(
        `INSERT INTO capsule_store_capsules
         (namespace,capsule_id,capsule_bytes,producer_envelope,record_sha256,created_at) VALUES(?,?,?,?,?,UTC_TIMESTAMP(6))`,
        [
          this.namespace,
          prepared.capsuleId,
          Buffer.from(prepared.capsule),
          Buffer.from(prepared.producerEnvelope),
          hash,
        ],
      );
    } catch (error) {
      if (errno(error) !== DUPLICATE_KEY) throw error;
      const existing = await this.read(connection, prepared.capsuleId, true);
      if (storageChecksum(existing) !== hash)
        throw new ArtifactError("conflict", "immutable record conflict");
      for (const a of existing.artifacts)
        if (a.state === "purged")
          throw new ArtifactError("purged", "originals were purged");
      return;
    }
    for (const a of prepared.artifacts)
      await connection.query(
        `INSERT INTO capsule_store_artifacts
         (namespace,capsule_id,name,digest_field,content_bytes,content_sha256,retention_state,created_at) VALUES(?,?,?,?,?,?,?,UTC_TIMESTAMP(6))`,
        [
          this.namespace,
          prepared.capsuleId,
          a.name,
          a.binding ?? "",
          a.content === undefined ? null : Buffer.from(a.content),
          a.contentSha256 ?? "",
          a.state,
        ],
      );
  }

  /** Loads a coherent snapshot and verifies authenticity and original digests. */
  public async get(id: string): Promise<Record> {
    const connection = await this.pool.getConnection();
    try {
      await connection.beginTransaction();
      const record = await this.read(connection, id, false);
      await connection.commit();
      return record;
    } catch (error) {
      await connection.rollback();
      throw error;
    } finally {
      connection.release();
    }
  }

  /** Reads within a caller transaction, optionally locking the parent row. */
  public async getTx(
    connection: PoolConnection,
    id: string,
    lock = false,
  ): Promise<Record> {
    return this.read(connection, id, lock);
  }

  private async read(
    connection: PoolConnection,
    id: string,
    lock: boolean,
  ): Promise<Record> {
    if (!ID_PATTERN.test(id)) throw new ArtifactError("invalid", "capsule id");
    const forUpdate = lock ? " FOR UPDATE" : "";
    const [capsuleRows] = await connection.query<RowDataPacket[]>(
      `SELECT capsule_bytes,producer_envelope,record_sha256 FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?${forUpdate}`,
      [this.namespace, id],
    );
    const capsule = capsuleRows[0];
    if (capsule === undefined)
      throw new ArtifactError("not_found", "capsule not found");
    const [rows] = await connection.query<RowDataPacket[]>(
      `SELECT name,digest_field,content_bytes,content_sha256,retention_state FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? ORDER BY name${forUpdate}`,
      [this.namespace, id],
    );
    const artifacts: Artifact[] = rows.map((row) => ({
      name: row.name as string,
      binding: row.digest_field as Artifact["binding"],
      content:
        row.content_bytes === null
          ? undefined
          : toBytes(row.content_bytes as Buffer),
      contentSha256: row.content_sha256 as string,
      state: row.retention_state as RetentionState,
    }));
    const record: Record = {
      capsuleId: id,
      capsule: toBytes(capsule.capsule_bytes as Buffer),
      producerEnvelope: toBytes(capsule.producer_envelope as Buffer),
      artifacts,
    };
    if (storageChecksum(record) !== capsule.record_sha256)
      throw new ArtifactError("corrupt", "stored record integrity failure");
    await verify(record, this.trusted);
    return record;
  }

  /**
   * Deletes business originals, retaining the exact Capsule/envelope, inventory
   * and tombstones. Erasure requires only namespace-scoped existence, not
   * integrity or signer verification.
   */
  public async purge(id: string): Promise<void> {
    if (!ID_PATTERN.test(id)) throw new ArtifactError("invalid", "capsule id");
    const connection = await this.pool.getConnection();
    try {
      await connection.beginTransaction();
      const [rows] = await connection.query<RowDataPacket[]>(
        `SELECT capsule_id FROM capsule_store_capsules WHERE namespace=? AND capsule_id=? FOR UPDATE`,
        [this.namespace, id],
      );
      if (rows[0] === undefined)
        throw new ArtifactError("not_found", "capsule not found");
      await connection.query(
        `UPDATE capsule_store_artifacts SET content_bytes=NULL,retention_state='purged',purged_at=UTC_TIMESTAMP(6) WHERE namespace=? AND capsule_id=? AND retention_state='present'`,
        [this.namespace, id],
      );
      await connection.commit();
    } catch (error) {
      await connection.rollback();
      throw error;
    } finally {
      connection.release();
    }
  }
}
