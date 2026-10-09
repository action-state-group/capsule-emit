// SQLite artifact backend on better-sqlite3. It mirrors the MySQL backend's
// contract: immutable records, byte-identical retry admission, and fail-closed
// reads that verify signer and original digests. The same database file may
// also hold a cll-ts SQLite log in separate tables.
import type { Database } from "better-sqlite3";

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
`;

interface CapsuleRow {
  capsule_bytes: Buffer;
  producer_envelope: Buffer;
  record_sha256: string;
}

interface ArtifactRow {
  name: string;
  digest_field: string;
  content_bytes: Buffer | null;
  content_sha256: string;
  retention_state: string;
}

function toBytes(value: Buffer): Uint8Array {
  return new Uint8Array(value);
}

function isConstraint(error: unknown): boolean {
  return String((error as { code?: string }).code).startsWith(
    "SQLITE_CONSTRAINT",
  );
}

/**
 * An application-importable SDK, independent of CLI profiles and CLL log IDs.
 * The caller owns the better-sqlite3 handle (opened with `PRAGMA
 * foreign_keys=ON` and at most one in-flight transaction) and the trusted
 * keys. A namespace isolates a collection; it must not implicitly be a log ID.
 */
export class SqliteArtifactStore implements Store {
  private readonly trusted: Uint8Array[];

  public constructor(
    private readonly db: Database,
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

  /** Creates v1 tables. Run explicitly during provisioning; no existing data is altered. */
  public async init(): Promise<void> {
    this.db.exec(SCHEMA);
  }

  /** Persists an immutable record atomically, accepting byte-identical retries. */
  public async put(record: Record): Promise<void> {
    this.db.exec("BEGIN");
    try {
      await this.putTx(record);
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  /**
   * Runs the immutable-insert logic on the current connection without managing
   * the transaction. The caller MUST have an open transaction and MUST roll the
   * whole transaction back on error; this method never commits. Await this
   * operation before committing; better-sqlite3 transaction callbacks cannot
   * own an async operation.
   */
  public async putTx(record: Record): Promise<void> {
    const prepared = prepare(record);
    await verify(prepared, this.trusted);
    const hash = storageChecksum(prepared);
    try {
      this.db
        .prepare(
          `INSERT INTO capsule_store_capsules
           (namespace,capsule_id,capsule_bytes,producer_envelope,record_sha256) VALUES(?,?,?,?,?)`,
        )
        .run(
          this.namespace,
          prepared.capsuleId,
          Buffer.from(prepared.capsule),
          Buffer.from(prepared.producerEnvelope),
          hash,
        );
    } catch (error) {
      if (!isConstraint(error)) throw error;
      const existing = await this.readOnConnection(prepared.capsuleId);
      if (storageChecksum(existing) !== hash)
        throw new ArtifactError("conflict", "immutable record conflict");
      for (const a of existing.artifacts)
        if (a.state === "purged")
          throw new ArtifactError("purged", "originals were purged");
      return;
    }
    const insert = this.db.prepare(
      `INSERT INTO capsule_store_artifacts
       (namespace,capsule_id,name,digest_field,content_bytes,content_sha256,retention_state) VALUES(?,?,?,?,?,?,?)`,
    );
    for (const a of prepared.artifacts)
      insert.run(
        this.namespace,
        prepared.capsuleId,
        a.name,
        a.binding ?? "",
        a.content === undefined ? null : Buffer.from(a.content),
        a.contentSha256 ?? "",
        a.state,
      );
  }

  /** Loads a coherent snapshot and verifies authenticity and original digests. */
  public async get(id: string): Promise<Record> {
    this.db.exec("BEGIN");
    try {
      const record = await this.readOnConnection(id);
      this.db.exec("COMMIT");
      return record;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  /** Reads within a caller-managed transaction. SQLite serializes writers. */
  public async getTx(id: string): Promise<Record> {
    return this.readOnConnection(id);
  }

  private async readOnConnection(id: string): Promise<Record> {
    if (!ID_PATTERN.test(id)) throw new ArtifactError("invalid", "capsule id");
    const capsule = this.db
      .prepare(
        `SELECT capsule_bytes,producer_envelope,record_sha256 FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?`,
      )
      .get(this.namespace, id) as CapsuleRow | undefined;
    if (capsule === undefined)
      throw new ArtifactError("not_found", "capsule not found");
    const rows = this.db
      .prepare(
        `SELECT name,digest_field,content_bytes,content_sha256,retention_state FROM capsule_store_artifacts WHERE namespace=? AND capsule_id=? ORDER BY name`,
      )
      .all(this.namespace, id) as ArtifactRow[];
    const artifacts: Artifact[] = rows.map((row) => ({
      name: row.name,
      binding: row.digest_field as Artifact["binding"],
      content:
        row.content_bytes === null ? undefined : toBytes(row.content_bytes),
      contentSha256: row.content_sha256,
      state: row.retention_state as RetentionState,
    }));
    const record: Record = {
      capsuleId: id,
      capsule: toBytes(capsule.capsule_bytes),
      producerEnvelope: toBytes(capsule.producer_envelope),
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
    this.db.exec("BEGIN");
    try {
      const existing = this.db
        .prepare(
          `SELECT capsule_id FROM capsule_store_capsules WHERE namespace=? AND capsule_id=?`,
        )
        .get(this.namespace, id) as { capsule_id: string } | undefined;
      if (existing === undefined)
        throw new ArtifactError("not_found", "capsule not found");
      this.db
        .prepare(
          `UPDATE capsule_store_artifacts SET content_bytes=NULL, retention_state='purged', purged_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE namespace=? AND capsule_id=? AND retention_state='present'`,
        )
        .run(this.namespace, id);
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }
}
