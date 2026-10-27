// The index extension's SQL, shared by the SQLite and MySQL backends. Each
// backend adapts its driver to {@link SqlTx}. It imports no storage driver.
import { ArtifactError } from "./record.js";
import {
  check,
  cursor,
  decodePaths,
  encodePaths,
  extract,
  normalizePaths,
  valueKey,
  type Page,
  type Query,
} from "./lookup.js";

/** One statement runner inside an open transaction. */
export interface SqlTx {
  all(
    sql: string,
    params: readonly unknown[],
  ): Promise<{ [column: string]: unknown }[]>;
  run(
    sql: string,
    params: readonly unknown[],
  ): Promise<{ changes: number; lastId: bigint }>;
}

/** The SQL that differs between the backends. */
export interface Dialect {
  /** Counts the index tables; 3 once they exist. */
  tablesExist: string;
  /** Ends a SELECT that must see and hold the latest committed rows (MySQL); empty for SQLite. */
  lockShared: string;
  lockUpdate: string;
  /** Starts an insert that skips rows already present. */
  insertIgnore: string;
}

/** The DDL both SQL backends append to their v1 schema, per dialect. */
export const SQLITE_INDEX_SCHEMA = `
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
`;

export const MYSQL_INDEX_SCHEMA = `
CREATE TABLE IF NOT EXISTS capsule_store_index_paths (
 namespace VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 paths TEXT CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 PRIMARY KEY (namespace)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS capsule_store_index_records (
 seq BIGINT NOT NULL AUTO_INCREMENT,
 namespace VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 capsule_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 ts BIGINT NULL,
 PRIMARY KEY (seq),
 UNIQUE KEY capsule_store_index_records_id (namespace, capsule_id),
 KEY capsule_store_index_records_ts (namespace, ts)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS capsule_store_index (
 namespace VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 path VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 value_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 seq BIGINT NOT NULL,
 PRIMARY KEY (namespace, path, value_sha256, seq)
) ENGINE=InnoDB;
`;

/** One SQL backend's view of the index tables. */
export class SqlIndex {
  private exist = false;

  public constructor(
    private readonly namespace: string,
    private readonly dialect: Dialect,
  ) {}

  // Databases provisioned before the index tables existed have no declaration
  // until init runs again.
  private async declared(
    tx: SqlTx,
    lock: string,
  ): Promise<string[] | undefined> {
    if (!this.exist) {
      const [row] = await tx.all(this.dialect.tablesExist, []);
      if (!(Number(Object.values(row ?? {})[0]) >= 3)) return undefined;
      this.exist = true;
    }
    const [row] = await tx.all(
      `SELECT paths FROM capsule_store_index_paths WHERE namespace=?${lock}`,
      [this.namespace],
    );
    if (row === undefined) return undefined;
    return decodePaths(String(row.paths));
  }

  /** Indexes a record putTx has just inserted, when the namespace declares paths. */
  public async admit(
    tx: SqlTx,
    id: string,
    capsule: Uint8Array,
  ): Promise<void> {
    const paths = await this.declared(tx, this.dialect.lockShared);
    if (paths !== undefined) await this.add(tx, paths, id, capsule);
  }

  private async add(
    tx: SqlTx,
    paths: readonly string[],
    id: string,
    capsule: Uint8Array,
  ): Promise<void> {
    const extracted = extract(capsule, paths);
    const result = await tx.run(
      `${this.dialect.insertIgnore} capsule_store_index_records (namespace,capsule_id,ts) VALUES(?,?,?)`,
      [this.namespace, id, extracted.ts ?? null],
    );
    if (result.changes === 0) return; // already indexed
    for (const { path, value } of extracted.entries)
      await tx.run(
        `INSERT INTO capsule_store_index (namespace,path,value_sha256,seq) VALUES(?,?,?,?)`,
        [this.namespace, path, valueKey(value), result.lastId],
      );
  }

  /** Declares paths inside tx, after the caller provisioned the tables. */
  public async declare(tx: SqlTx, paths: readonly string[]): Promise<void> {
    const normalized = normalizePaths(paths);
    const current = await this.declared(tx, this.dialect.lockUpdate);
    if (current !== undefined) {
      if (encodePaths(current) !== encodePaths(normalized))
        throw new ArtifactError(
          "conflict",
          `namespace already indexes ${encodePaths(current)}`,
        );
      return;
    }
    await tx.run(
      `INSERT INTO capsule_store_index_paths (namespace,paths) VALUES(?,?)`,
      [this.namespace, encodePaths(normalized)],
    );
    await this.backfill(tx, normalized);
  }

  /** Replaces the declaration and rebuilds the index inside tx. */
  public async reindex(tx: SqlTx, paths: readonly string[]): Promise<void> {
    const normalized = normalizePaths(paths);
    // Lock the declaration first, so a concurrent put waits for the rebuild.
    try {
      await this.declared(tx, this.dialect.lockUpdate);
    } catch (error) {
      if (!(error instanceof ArtifactError && error.code === "corrupt"))
        throw error;
    }
    for (const table of [
      "capsule_store_index",
      "capsule_store_index_records",
      "capsule_store_index_paths",
    ])
      await tx.run(`DELETE FROM ${table} WHERE namespace=?`, [this.namespace]);
    await tx.run(
      `INSERT INTO capsule_store_index_paths (namespace,paths) VALUES(?,?)`,
      [this.namespace, encodePaths(normalized)],
    );
    await this.backfill(tx, normalized);
  }

  // Indexes the stored records in creation order. A record whose Capsule bytes
  // no longer decode is left out; get reports it as corrupt.
  private async backfill(tx: SqlTx, paths: readonly string[]): Promise<void> {
    const rows = await tx.all(
      `SELECT capsule_id,capsule_bytes FROM capsule_store_capsules WHERE namespace=? ORDER BY created_at,capsule_id${this.dialect.lockShared}`,
      [this.namespace],
    );
    for (const row of rows) {
      try {
        await this.add(
          tx,
          paths,
          String(row.capsule_id),
          new Uint8Array(row.capsule_bytes as Buffer),
        );
      } catch (error) {
        if (!(error instanceof ArtifactError && error.code === "corrupt"))
          throw error;
      }
    }
  }

  /** Answers a query inside tx. */
  public async find(tx: SqlTx, query: Query = {}): Promise<Page> {
    const paths = await this.declared(tx, "");
    if (paths === undefined)
      throw new ArtifactError("invalid", "no index paths declared");
    const c = check(query, paths);
    const [first, ...rest] = c.where;
    let sql: string;
    const params: unknown[] = [];
    if (first === undefined) {
      sql = `SELECT r.seq,r.capsule_id FROM capsule_store_index_records r WHERE r.namespace=? AND r.seq>?`;
      params.push(this.namespace, c.after);
    } else {
      sql =
        `SELECT r.seq,r.capsule_id FROM capsule_store_index i0 JOIN capsule_store_index_records r ON r.seq=i0.seq` +
        ` WHERE i0.namespace=? AND i0.path=? AND i0.value_sha256=? AND i0.seq>?`;
      params.push(this.namespace, first.path, valueKey(first.value), c.after);
      for (const w of rest) {
        sql += ` AND EXISTS (SELECT 1 FROM capsule_store_index i WHERE i.namespace=r.namespace AND i.path=? AND i.value_sha256=? AND i.seq=r.seq)`;
        params.push(w.path, valueKey(w.value));
      }
    }
    if (c.since !== undefined) {
      sql += ` AND r.ts>=?`;
      params.push(c.since);
    }
    sql += ` ORDER BY r.seq LIMIT ?`;
    params.push(c.limit + 1);
    const rows = await tx.all(sql, params);
    const page: Page = { ids: [], next: "" };
    let last = 0n;
    for (const row of rows) {
      if (page.ids.length === c.limit) {
        page.next = cursor(last);
        break;
      }
      page.ids.push(String(row.capsule_id));
      last = BigInt(row.seq as number | bigint | string);
    }
    return page;
  }
}
