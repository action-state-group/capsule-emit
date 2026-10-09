import type { Pool } from "mysql2/promise";
import { describe, expect, it, vi } from "vitest";
import { MysqlArtifactStore } from "../../src/artifact/mysql.js";
import { prepare, storageChecksum } from "../../src/artifact/record.js";
import { makeRecord, utf8 } from "./fixture.js";

function connection() {
  const calls = {
    beginTransaction: vi.fn(async () => {}),
    query: vi.fn<(sql: string) => Promise<[unknown[], unknown[]]>>(async () => [
      [],
      [],
    ]),
    commit: vi.fn(async () => {}),
    rollback: vi.fn(async () => {}),
    release: vi.fn(),
  };
  const pool = { getConnection: vi.fn(async () => calls) } as unknown as Pool;
  return { calls, pool };
}

describe("MySQL async verification transaction boundary (driver double)", () => {
  it("awaits write rejection, rolls back and releases without insertion or commit", async () => {
    const { record, trusted } = await makeRecord();
    const { pool, calls } = connection();
    const store = new MysqlArtifactStore(pool, "test", [trusted]);
    await expect(
      store.put({ ...record, producerEnvelope: utf8("invalid") }),
    ).rejects.toMatchObject({ code: "invalid" });
    expect(calls.beginTransaction).toHaveBeenCalledOnce();
    expect(calls.query).not.toHaveBeenCalled();
    expect(calls.commit).not.toHaveBeenCalled();
    expect(calls.rollback).toHaveBeenCalledOnce();
    expect(calls.release).toHaveBeenCalledOnce();
  });

  it("commits a valid write only after the promise completes", async () => {
    const { record, trusted } = await makeRecord();
    const { pool, calls } = connection();
    const store = new MysqlArtifactStore(pool, "test", [trusted]);
    const writing = store.put(record);
    expect(calls.commit).not.toHaveBeenCalled();
    await writing;
    expect(calls.query).toHaveBeenCalledTimes(record.artifacts.length + 1);
    expect(calls.commit).toHaveBeenCalledOnce();
    expect(calls.rollback).not.toHaveBeenCalled();
    expect(calls.release).toHaveBeenCalledOnce();
  });

  it("rejects a corrupt stored preimage before committing a read transaction", async () => {
    const { record: raw, trusted } = await makeRecord();
    const record = prepare(raw);
    const { pool, calls } = connection();
    calls.query.mockImplementation(async (sql: string) => {
      if (sql.includes("FROM capsule_store_capsules"))
        return [
          [
            {
              capsule_bytes: Buffer.from(record.capsule),
              producer_envelope: Buffer.from(record.producerEnvelope),
              record_sha256: storageChecksum(record),
            },
          ],
          [],
        ];
      return [
        record.artifacts.map((artifact) => ({
          name: artifact.name,
          digest_field: artifact.binding ?? "",
          content_bytes: Buffer.from(
            artifact.name === "payload"
              ? utf8('{"wrong":true}')
              : artifact.content!,
          ),
          content_sha256: artifact.contentSha256,
          retention_state: artifact.state,
        })),
        [],
      ];
    });
    const store = new MysqlArtifactStore(pool, "test", [trusted]);
    await expect(store.get(record.capsuleId)).rejects.toMatchObject({
      code: "corrupt",
    });
    expect(calls.commit).not.toHaveBeenCalled();
    expect(calls.rollback).toHaveBeenCalledOnce();
    expect(calls.release).toHaveBeenCalledOnce();
  });
});
