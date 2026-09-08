import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [emitRoot, cllRoot, mode, prefix, checkpointPath] = process.argv.slice(2);
const emit = await import(pathToFileURL(resolve(emitRoot, "dist/index.js")));
const cll = await import(pathToFileURL(resolve(cllRoot, "dist/index.js")));
const seed = Uint8Array.from({ length: 32 }, (_, i) => i);
const now = new Date("2026-09-07T12:00:00Z");
const identity = emit.createEd25519Identity(seed);
if (mode === "produce") {
  const result = emit.seal({
    capsule: {
      actionId: "producer-cll/example",
      actionType: "fyi",
      operator: "example-org",
      developer: "example-agent@v1",
      timestamp: now,
    },
    identity,
  });
  await writeFile(`${prefix}.json`, result.payload);
  await writeFile(`${prefix}.cose`, result.envelope);
} else if (mode === "checkpoint") {
  const payload = await readFile(`${prefix}.json`);
  const verified = emit.verifyCapsule(payload);
  const author = emit.verifyEnvelope(
    verified.capsuleId,
    await readFile(`${prefix}.cose`),
  );
  assert.equal(author.ok, true);
  assert.deepEqual(author.publicKey, identity.publicKey);
  const store = new cll.MemoryStore();
  try {
    for (const outcome of ["inserted", "idempotent"]) {
      const result = await store.append({
        value: Buffer.from(verified.capsuleId, "hex"),
        appendedAt: now,
      });
      assert.equal(result.outcome, outcome);
      assert.equal(result.entry.seq, 1n);
    }
    const runner = new cll.CheckpointRunner(store, {
      logId: "producer-cll",
      identity: cll.createCheckpointIdentity(seed),
      entryCadence: 1,
      clock: () => now,
    });
    const checkpoint = await runner.runOnce();
    assert.ok(checkpoint);
    assert.equal(checkpoint.mmrSize, 1n);
    assert.equal(cll.verifyCheckpoint(checkpoint.cose), true);
    await writeFile(checkpointPath, checkpoint.cose);
  } finally {
    await store.close();
  }
} else {
  throw new Error("expected produce or checkpoint");
}
