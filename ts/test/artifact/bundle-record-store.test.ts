import { describe, expect, it } from "vitest";
import type { RecordStore } from "@action-state-group/agent-action-capsule/bundle";
import { MemoryArtifactStore } from "../../src/artifact/memory.js";
import type { Store } from "../../src/artifact/index.js";
import { makeRecord } from "./fixture.js";

// AAC's proofSourceFromCll(backend, records) takes any RecordStore; an
// artifact Store is one, so capsule-emit adds no Evidence Bundle adapter.
describe("artifact Store as AAC's bundle RecordStore", () => {
  it("is assignable to RecordStore and reports a missing record as not_found", async () => {
    const { record, trusted } = await makeRecord();
    const store: Store = new MemoryArtifactStore("bundle", [trusted]);
    const records: RecordStore = store;
    await store.put(record);
    const got = await records.get(record.capsuleId);
    expect(got.capsule).toEqual(record.capsule);
    expect(got.producerEnvelope).toEqual(record.producerEnvelope);
    await expect(records.get("c".repeat(64))).rejects.toMatchObject({
      code: "not_found",
    });
  });
});
