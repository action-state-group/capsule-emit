import { MySqlContainer } from "@testcontainers/mysql";
import mysql from "mysql2/promise";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { MysqlArtifactStore } from "../../src/artifact/mysql.js";
import type { Record } from "../../src/artifact/index.js";
import { makeRecord, utf8 } from "./fixture.js";
// Opt-in: needs Docker. Enable with ARTIFACT_MYSQL_TEST=1, mirroring the Go
// backend's DSN-gated integration tests.
const enabled = process.env.ARTIFACT_MYSQL_TEST === "1";
describe.skipIf(!enabled)("MysqlArtifactStore", () => {
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
    await pool.end();
    await container.stop();
  });
  async function store(
    trusted: Uint8Array,
    namespace: string,
  ): Promise<MysqlArtifactStore> {
    const s = new MysqlArtifactStore(pool, namespace, [trusted]);
    await s.init();
    return s;
  }
  it("round-trips an authenticated record", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted, "roundtrip");
    await s.put(record);
    const loaded = await s.get(record.capsuleId);
    expect(loaded.capsuleId).toBe(record.capsuleId);
    expect(loaded.artifacts).toHaveLength(3);
  });
  it("accepts a byte-identical retry and rejects divergence", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted, "retry");
    await s.put(record);
    await expect(s.put(record)).resolves.toBeUndefined();
    const divergent: Record = {
      ...record,
      artifacts: [
        ...record.artifacts,
        { name: "extra", content: utf8("x"), state: "present" },
      ],
    };
    await expect(s.put(divergent)).rejects.toMatchObject({ code: "conflict" });
  });
  it("purges originals and refuses to resurrect them", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted, "purge");
    await s.put(record);
    await s.purge(record.capsuleId);
    const purged = await s.get(record.capsuleId);
    for (const a of purged.artifacts) expect(a.state).toBe("purged");
    await expect(s.put(record)).rejects.toMatchObject({ code: "purged" });
  });
  it("returns not_found for an unknown id", async () => {
    const { trusted } = await makeRecord();
    const s = await store(trusted, "missing");
    await expect(s.get("b".repeat(64))).rejects.toMatchObject({
      code: "not_found",
    });
  });
});
