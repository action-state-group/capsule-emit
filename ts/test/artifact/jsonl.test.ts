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

  it("starts a new line after a torn last line, so the record survives reopening", async () => {
    const { record, trusted } = await makeRecord("torn-tail");
    const s = await store(trusted);
    fs.appendFileSync(filePath, '{"capsule_id":');
    await s.put(record);
    // The live index points past the inserted newline.
    expect((await s.get(record.capsuleId)).capsuleId).toBe(record.capsuleId);
    await s.purge(record.capsuleId);
    const reopened = new JsonlArtifactStore(filePath, "test", [trusted]);
    const got = await reopened.get(record.capsuleId);
    expect(got.artifacts.every((a) => a.state !== "present")).toBe(true);
    expect(
      fs.readFileSync(filePath, "utf8").startsWith('{"capsule_id":\n'),
    ).toBe(true);
  });

  it("never indexes a torn last line, and purge keeps it byte for byte", async () => {
    const { record, trusted } = await makeRecord("unterminated");
    const { record: other } = await makeRecord("unterminated-other");
    const s = await store(trusted);
    await s.put(record);
    const tail = Buffer.from(
      JSON.stringify({
        capsule_id: other.capsuleId,
        junk: "\u00e9",
      }),
    );
    fs.appendFileSync(filePath, Buffer.concat([tail, Buffer.from([0xff])]));
    const reopened = new JsonlArtifactStore(filePath, "test", [trusted]);
    await expect(reopened.get(other.capsuleId)).rejects.toMatchObject({
      code: "not_found",
    });
    await reopened.purge(record.capsuleId);
    const after = fs.readFileSync(filePath);
    expect(
      after
        .subarray(after.length - tail.length - 2)
        .equals(Buffer.concat([Buffer.from("\n"), tail, Buffer.from([0xff])])),
    ).toBe(true);
  });
});
