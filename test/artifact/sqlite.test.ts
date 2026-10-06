import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SqliteArtifactStore } from "../../src/artifact/sqlite.js";
import type { Record } from "../../src/artifact/index.js";
import { makeRecord, utf8 } from "./fixture.js";
describe("SqliteArtifactStore", () => {
  let db: Database.Database;
  beforeEach(() => {
    db = new Database(":memory:");
    db.pragma("foreign_keys = ON");
  });
  afterEach(() => {
    db.close();
  });
  async function store(
    trusted: Uint8Array,
    namespace = "test",
  ): Promise<SqliteArtifactStore> {
    const s = new SqliteArtifactStore(db, namespace, [trusted]);
    await s.init();
    return s;
  }
  it("round-trips an authenticated record", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    const loaded = await s.get(record.capsuleId);
    expect(loaded.capsuleId).toBe(record.capsuleId);
    expect(loaded.artifacts.map((a) => a.name).sort()).toEqual([
      "agent_output",
      "payload",
      "private_note",
    ]);
    const payload = loaded.artifacts.find((a) => a.name === "payload");
    expect(payload?.content).toBeDefined();
  });
  it("accepts a byte-identical retry idempotently", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    await expect(s.put(record)).resolves.toBeUndefined();
  });
  it("rejects a divergent record for the same Capsule ID with a conflict", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    const divergent: Record = {
      ...record,
      artifacts: [
        ...record.artifacts,
        { name: "extra", content: utf8("x"), state: "present" },
      ],
    };
    await expect(s.put(divergent)).rejects.toMatchObject({ code: "conflict" });
  });
  it("returns not_found for an unknown Capsule ID", async () => {
    const { trusted } = await makeRecord();
    const s = await store(trusted);
    await expect(s.get("a".repeat(64))).rejects.toMatchObject({
      code: "not_found",
    });
  });
  it("purges originals into tombstones and refuses a re-put of the originals", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    await s.purge(record.capsuleId);
    const purged = await s.get(record.capsuleId);
    for (const a of purged.artifacts) {
      expect(a.state).toBe("purged");
      expect(a.content).toBeUndefined();
    }
    // A retry of the original present record can no longer resurrect content.
    await expect(s.put(record)).rejects.toMatchObject({ code: "purged" });
  });
  it("fails closed on a corrupted stored original", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    // Corrupt the stored bytes of an unbound attachment directly.
    db.prepare(
      "UPDATE capsule_store_artifacts SET content_bytes=? WHERE name='private_note'",
    ).run(Buffer.from("tampered"));
    await expect(s.get(record.capsuleId)).rejects.toMatchObject({
      code: "corrupt",
    });
  });
  it("round-trips a record with no artifacts", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    const empty: Record = { ...record, artifacts: [] };
    await s.put(empty);
    const loaded = await s.get(record.capsuleId);
    expect(loaded.artifacts).toEqual([]);
  });
  it("isolates records by namespace", async () => {
    const { record, trusted } = await makeRecord();
    const one = await store(trusted, "one");
    const two = await store(trusted, "two");
    await one.put(record);
    await expect(two.get(record.capsuleId)).rejects.toMatchObject({
      code: "not_found",
    });
  });

  it("rolls back a write after asynchronous verification rejects", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    const invalid = {
      ...record,
      artifacts: record.artifacts.map((a) =>
        a.name === "payload" ? { ...a, content: utf8('{"wrong":true}') } : a,
      ),
    };
    await expect(s.put(invalid)).rejects.toMatchObject({
      code: "digest_mismatch",
    });
    expect(db.inTransaction).toBe(false);
    expect(
      db.prepare("SELECT count(*) AS count FROM capsule_store_capsules").get(),
    ).toEqual({ count: 0 });
    await s.put(record);
    expect((await s.get(record.capsuleId)).capsuleId).toBe(record.capsuleId);
  });

  it("joins a caller transaction only through awaited operations", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    db.exec("CREATE TABLE application_rows (id TEXT PRIMARY KEY)");
    db.exec("BEGIN");
    try {
      db.prepare("INSERT INTO application_rows VALUES (?)").run("business-row");
      await s.putTx(record);
      expect((await s.getTx(record.capsuleId)).capsuleId).toBe(
        record.capsuleId,
      );
      await s.putTx({ ...record, producerEnvelope: utf8("invalid") });
      throw new Error("invalid envelope was admitted");
    } catch (error) {
      db.exec("ROLLBACK");
      expect(error).toMatchObject({ code: "invalid" });
    }
    expect(
      db.prepare("SELECT count(*) AS count FROM application_rows").get(),
    ).toEqual({ count: 0 });
    expect(
      db.prepare("SELECT count(*) AS count FROM capsule_store_capsules").get(),
    ).toEqual({ count: 0 });
    expect(db.inTransaction).toBe(false);
  });
});
