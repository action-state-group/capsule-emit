import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { JsonlArtifactStore } from "../../src/artifact/jsonl.js";
import type { Record } from "../../src/artifact/index.js";
import { makeRecord, utf8 } from "./fixture.js";
describe("JsonlArtifactStore", () => {
  let filePath: string;
  beforeEach(() => {
    filePath = path.join(
      os.tmpdir(),
      `capsule-test-${process.pid.toString()}-${Date.now().toString()}.jsonl`,
    );
  });
  afterEach(() => {
    try {
      fs.unlinkSync(filePath);
    } catch {
      // ignore — file may not exist if init() was never called
    }
  });
  async function store(
    trusted: Uint8Array,
    namespace = "test",
    fp = filePath,
  ): Promise<JsonlArtifactStore> {
    const s = new JsonlArtifactStore(fp, namespace, [trusted]);
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
  it("rejects a record signed by an untrusted key on put", async () => {
    const { record } = await makeRecord();
    const { trusted: differentKey } = await makeRecord("other");
    const s = await store(differentKey);
    await expect(s.put(record)).rejects.toMatchObject({
      code: "untrusted_signer",
    });
  });
  it("rejects a stored record with an untrusted key on get (fails closed)", async () => {
    // Store with trusted, then open with a different key.
    const { record, trusted } = await makeRecord();
    const s1 = await store(trusted);
    await s1.put(record);
    const { trusted: differentKey } = await makeRecord("other");
    const s2 = new JsonlArtifactStore(filePath, "test", [differentKey]);
    await expect(s2.get(record.capsuleId)).rejects.toMatchObject({
      code: "untrusted_signer",
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
  it("purge keeps inventory and the record still gets and verifies", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    await s.purge(record.capsuleId);
    const purged = await s.get(record.capsuleId);
    expect(purged.capsuleId).toBe(record.capsuleId);
    expect(purged.artifacts).toHaveLength(3);
    for (const a of purged.artifacts) {
      expect(a.content).toBeUndefined();
      expect(a.state).toBe("purged");
      expect(a.contentSha256).toMatch(/^[0-9a-f]{64}$/u);
    }
  });
  it("purge is idempotent", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    await s.purge(record.capsuleId);
    await expect(s.purge(record.capsuleId)).resolves.toBeUndefined();
  });
  it("returns not_found for purge of an unknown Capsule ID", async () => {
    const { trusted } = await makeRecord();
    const s = await store(trusted);
    await expect(s.purge("a".repeat(64))).rejects.toMatchObject({
      code: "not_found",
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
  it("fails closed on a stored line with tampered content bytes", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    // Read the written line, tamper with private_note's content while
    // leaving contentSha256 intact so the checksum mismatch is detectable.
    const raw = fs.readFileSync(filePath, "utf8").trimEnd();
    const parsed = JSON.parse(raw) as {
      capsuleId: string;
      capsule: string;
      producerEnvelope: string;
      artifacts: Array<{
        name: string;
        content?: string;
        state: string;
        contentSha256?: string;
        binding?: string;
      }>;
    };
    const note = parsed.artifacts.find((a) => a.name === "private_note");
    if (note !== undefined) {
      note.content = Buffer.from("tampered").toString("base64");
      // contentSha256 is intentionally left at the original hash so it
      // diverges from the tampered content — verify() catches this.
    }
    fs.writeFileSync(filePath, JSON.stringify(parsed) + "\n");
    // Rebuild store so the index reflects the rewritten line.
    const s2 = new JsonlArtifactStore(filePath, "test", [trusted]);
    await expect(s2.get(record.capsuleId)).rejects.toMatchObject({
      code: "corrupt",
    });
  });
  it("fails closed on a corrupt (unparseable JSON) line after index was built", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    await s.put(record);
    // Overwrite the file after the index was built in the same store instance
    // so the in-memory index still points to offset 0 but the file now holds
    // garbage. readSpan will return the garbage bytes and decodeRecord throws.
    fs.writeFileSync(filePath, "not-valid-json\n");
    await expect(s.get(record.capsuleId)).rejects.toMatchObject({
      code: "corrupt",
    });
  });
  it("requires the file to exist before put (does not auto-create)", async () => {
    const { record, trusted } = await makeRecord();
    // Construct without init(): the file does not exist yet.
    const s = new JsonlArtifactStore(filePath, "test", [trusted]);
    await expect(s.put(record)).rejects.toThrow();
    expect(fs.existsSync(filePath)).toBe(false);
  });
  it("preserves the store file mode across purge", async () => {
    const { record, trusted } = await makeRecord();
    const s = await store(trusted);
    fs.chmodSync(filePath, 0o640);
    await s.put(record);
    await s.purge(record.capsuleId);
    expect(fs.statSync(filePath).mode & 0o777).toBe(0o640);
  });
  it("rebuilds the index across multiple records on reopen", async () => {
    const recs = await Promise.all(
      Array.from(
        { length: 6 },
        async (_, i) => await makeRecord(`rec-${i.toString()}`),
      ),
    );
    const keys = recs.map((r) => r.trusted);
    const s = new JsonlArtifactStore(filePath, "test", keys);
    await s.init();
    for (const { record } of recs) await s.put(record);
    // Reopen forces scanIndex over a multi-line file.
    const s2 = new JsonlArtifactStore(filePath, "test", keys);
    for (const { record } of recs) {
      expect((await s2.get(record.capsuleId)).capsuleId).toBe(record.capsuleId);
    }
  });
  it("indexes a line larger than the scan chunk (carry across chunks)", async () => {
    const { record, trusted } = await makeRecord();
    // Enlarge an UNBOUND artifact so the JSON line exceeds the 64 KiB scan
    // chunk; unbound content is not authenticated, so the record still verifies.
    const big: Record = {
      ...record,
      artifacts: record.artifacts.map((a) =>
        a.name === "private_note"
          ? { ...a, content: utf8("x".repeat(80 * 1024)) }
          : a,
      ),
    };
    const s = await store(trusted);
    await s.put(big);
    const s2 = new JsonlArtifactStore(filePath, "test", [trusted]);
    const got = await s2.get(big.capsuleId);
    expect(got.capsuleId).toBe(big.capsuleId);
    const note = got.artifacts.find((a) => a.name === "private_note");
    expect(note?.content?.length).toBe(80 * 1024);
  });
});
