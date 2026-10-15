import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { MySqlContainer } from "@testcontainers/mysql";
import Database from "better-sqlite3";
import mysql from "mysql2/promise";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  artifactConformanceTests,
  conformanceVectors,
  diffRecords,
  runArtifactConformance,
  vectorRecord,
  type ConformanceHarness,
} from "../../src/artifact/conformance.js";
import { isTxStore, storageChecksum } from "../../src/artifact/index.js";
import {
  decodeRecord,
  encodeRecord,
  JsonlArtifactStore,
} from "../../src/artifact/jsonl.js";
import { MemoryArtifactStore } from "../../src/artifact/memory.js";
import { MysqlArtifactStore } from "../../src/artifact/mysql.js";
import { SqliteArtifactStore } from "../../src/artifact/sqlite.js";

const repo = join(import.meta.dirname, "..", "..", "..");

function register<B>(name: string, harness: () => ConformanceHarness<B>): void {
  describe(`${name} conformance`, () => {
    // Test names are fixed by the vectors; the harness is built lazily so an
    // opt-in backend's setup runs only when its tests do.
    for (const test of artifactConformanceTests(harness())) {
      if (test.skip !== undefined)
        it.skip(`${test.name} (${test.skip})`, () => {});
      else
        it(test.name, async () => {
          const real = artifactConformanceTests(harness()).find(
            (t) => t.name === test.name,
          );
          await real?.run();
        });
    }
  });
}

register("memory", () => ({
  newBacking: () => undefined,
  open: (_b: undefined, ns, keys) => new MemoryArtifactStore(ns, keys),
}));

register("jsonl", () => ({
  newBacking: () =>
    join(mkdtempSync(join(tmpdir(), "artifact-jsonl-")), "a.jsonl"),
  open: (path: string, ns, keys) => new JsonlArtifactStore(path, ns, keys),
  durable: true,
}));

register("sqlite", () => ({
  newBacking: () => {
    const db = new Database(
      join(mkdtempSync(join(tmpdir(), "artifact-sqlite-")), "a.db"),
    );
    db.pragma("foreign_keys = ON");
    return db;
  },
  open: (db: Database.Database, ns, keys) =>
    new SqliteArtifactStore(db, ns, keys),
  close: (db: Database.Database) => {
    db.close();
  },
  durable: true,
  sharedBacking: true,
}));

// Opt-in: needs Docker, like mysql.test.ts. Enable with ARTIFACT_MYSQL_TEST=1.
const mysqlEnabled = process.env.ARTIFACT_MYSQL_TEST === "1";
describe.skipIf(!mysqlEnabled)("mysql conformance", () => {
  let container: Awaited<ReturnType<MySqlContainer["start"]>>;
  let pool: mysql.Pool;
  beforeAll(async () => {
    container = await new MySqlContainer("mysql:8.4")
      .withDatabase("capsule")
      .withUsername("capsule")
      .withUserPassword("capsule-password")
      .start();
    pool = mysql.createPool(container.getConnectionUri());
  }, 180000);
  afterAll(async () => {
    await pool?.end();
    await container?.stop();
  });
  it("passes every case", async () => {
    const outcomes = await runArtifactConformance({
      newBacking: async () => {
        await new MysqlArtifactStore(pool, "conformance", [
          new Uint8Array(32),
        ]).init();
        for (const table of [
          "capsule_store_artifacts",
          "capsule_store_capsules",
        ])
          await pool.query(
            `DELETE FROM ${table} WHERE namespace IN ('conformance','alpha','beta')`,
          );
        return pool;
      },
      open: (p: mysql.Pool, ns, keys) => new MysqlArtifactStore(p, ns, keys),
      durable: true,
      sharedBacking: true,
    });
    expect(outcomes.filter((o) => o.status === "fail")).toEqual([]);
    expect(outcomes.some((o) => o.status === "pass")).toBe(true);
  });
});

describe("conformance runner", () => {
  it("bundles a byte-identical copy of the shared vectors", () => {
    expect(
      readFileSync(
        join(
          import.meta.dirname,
          "../../src/artifact/conformance-vectors.json",
        ),
        "utf8",
      ),
    ).toBe(
      readFileSync(
        join(repo, "test-vectors/artifact-store/vectors.json"),
        "utf8",
      ),
    );
  });

  it("reports failures from a store that breaks the contract", async () => {
    const outcomes = await runArtifactConformance({
      newBacking: () => undefined,
      open: (_b: undefined, ns, keys) => {
        const store = new MemoryArtifactStore(ns, keys);
        store.put = async () => {};
        return store;
      },
    });
    expect(outcomes.some((o) => o.status === "fail")).toBe(true);
    expect(outcomes.some((o) => o.status === "skip")).toBe(true);
  });

  it("fails a store whose get resolves without a record", async () => {
    const outcomes = await runArtifactConformance({
      newBacking: () => undefined,
      open: (_b: undefined, ns, keys) => {
        const store = new MemoryArtifactStore(ns, keys);
        const get = store.get.bind(store);
        store.get = async (id) => {
          await get(id); // keep every expected error
          return undefined as never;
        };
        return store;
      },
    });
    const roundtrip = outcomes.find((o) => o.name === "put_get_roundtrip");
    expect(roundtrip?.status).toBe("fail");
    expect(roundtrip?.detail).toContain("without a record");
  });

  it("diffRecords notices every difference", () => {
    const stored = vectorRecord("basic.stored");
    expect(diffRecords(stored, stored)).toBe("");
    expect(diffRecords(stored, vectorRecord("basic.purged"))).not.toBe("");
    expect(diffRecords(stored, vectorRecord("empty.stored"))).not.toBe("");
    const blank = vectorRecord("blank.stored");
    const absent = {
      ...blank,
      artifacts: blank.artifacts.map((a) => ({ ...a, content: undefined })),
    };
    expect(diffRecords(blank, absent)).toContain("presence");
  });
});

describe("cross-language goldens", () => {
  it.each(conformanceVectors.checksums)("storage checksum of $record", (v) => {
    expect(storageChecksum(vectorRecord(v.record))).toBe(v.storage_checksum);
  });

  it.each(conformanceVectors.lines)("jsonl line of $record", (v) => {
    expect(encodeRecord(vectorRecord(v.record))).toBe(v.line);
    expect(diffRecords(vectorRecord(v.record), decodeRecord(v.line))).toBe("");
  });
});

describe("transactional extension", () => {
  it("is detected on the SQL backends only", () => {
    const key = new Uint8Array(32);
    expect(isTxStore(new MemoryArtifactStore("ns", [key]))).toBe(false);
    expect(
      isTxStore(new SqliteArtifactStore(new Database(":memory:"), "ns", [key])),
    ).toBe(true);
  });

  it("SQLite putTx/getTx take the caller's handle, and keep the old forms", async () => {
    const db = new Database(":memory:");
    db.pragma("foreign_keys = ON");
    const trusted = Buffer.from(conformanceVectors.keys.trusted ?? "", "hex");
    const store = new SqliteArtifactStore(db, "ns", [trusted]);
    await store.init();
    const basic = vectorRecord("basic");
    db.exec("BEGIN");
    await store.putTx(db, basic);
    expect((await store.getTx(db, basic.capsuleId, true)).capsuleId).toBe(
      basic.capsuleId,
    );
    db.exec("ROLLBACK");
    await expect(store.get(basic.capsuleId)).rejects.toMatchObject({
      code: "not_found",
    });
    db.exec("BEGIN");
    await store.putTx(basic);
    expect((await store.getTx(basic.capsuleId)).capsuleId).toBe(
      basic.capsuleId,
    );
    db.exec("COMMIT");
    expect((await store.get(basic.capsuleId)).capsuleId).toBe(basic.capsuleId);
  });

  it("rejects a malformed ID before touching a closed database", async () => {
    const db = new Database(":memory:");
    const store = new SqliteArtifactStore(db, "ns", [new Uint8Array(32)]);
    db.close();
    await expect(store.get("bad")).rejects.toMatchObject({ code: "invalid" });
    await expect(store.purge("bad")).rejects.toMatchObject({ code: "invalid" });
  });
});
