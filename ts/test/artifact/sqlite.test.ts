import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SqliteArtifactStore } from "../../src/artifact/sqlite.js";
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
