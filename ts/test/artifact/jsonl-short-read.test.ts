// A put whose last-byte read comes back short (the file shrank under the
// store) fails as corrupt instead of appending at a stale offset.
import * as os from "node:os";
import * as path from "node:path";

import { describe, expect, it, vi } from "vitest";

const control = vi.hoisted(() => ({ shortRead: false, closed: 0 }));

vi.mock("node:fs", async (importOriginal) => {
  const real = await importOriginal<typeof import("node:fs")>();
  return {
    ...real,
    readSync: ((...args: Parameters<typeof real.readSync>) =>
      control.shortRead ? 0 : real.readSync(...args)) as typeof real.readSync,
    closeSync: (fd: number) => {
      control.closed += 1;
      real.closeSync(fd);
    },
  };
});

const fs = await import("node:fs");
const { JsonlArtifactStore } = await import("../../src/artifact/jsonl.js");
const { makeRecord } = await import("./fixture.js");

describe("JsonlArtifactStore short read", () => {
  it("fails as corrupt and writes nothing", async () => {
    const filePath = path.join(
      fs.mkdtempSync(path.join(os.tmpdir(), "artifact-short-")),
      "a.jsonl",
    );
    const { record, trusted } = await makeRecord("short-read");
    const { record: second, trusted: secondKey } =
      await makeRecord("short-read-2");
    const store = new JsonlArtifactStore(filePath, "test", [
      trusted,
      secondKey,
    ]);
    await store.init();
    await store.put(record);
    const before = fs.readFileSync(filePath);
    control.shortRead = true;
    const closedBefore = control.closed;
    await expect(store.put(second)).rejects.toMatchObject({ code: "corrupt" });
    control.shortRead = false;
    expect(control.closed).toBeGreaterThan(closedBefore);
    expect(fs.readFileSync(filePath).equals(before)).toBe(true);
    await expect(store.get(second.capsuleId)).rejects.toMatchObject({
      code: "not_found",
    });
  });
});
