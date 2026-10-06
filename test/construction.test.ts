import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  build,
  received,
  buildComposition,
  who,
  did,
  verifyCapsule,
  createEd25519Identity,
  sign,
  type Input,
} from "../src/index.js";
const input: Input = {
  actionId: "construction-test",
  actionType: "fyi",
  operator: "operator",
  developer: "developer",
  timestamp: "2026-09-09T00:00:00Z",
};
describe("shared construction boundary", () => {
  it("preserves ordinary compute fields and carried runtime/model metadata", async () => {
    const ordinary = await build({
      ...input,
      compute: { agentInputDigest: "a".repeat(64), runtime: "runtime" },
    });
    expect(ordinary.value.model_attestation).toMatchObject({
      compute_attestation: {
        agent_input_digest: "a".repeat(64),
        runtime: "runtime",
      },
    });
    const carried = await received(
      {
        ...input,
        model: { provider: "provider" },
        compute: { runtime: "runtime" },
      },
      new Uint8Array([1, 2]),
      "foreign-artifact",
    );
    expect(carried.value.model_attestation).toMatchObject({
      provider: "provider",
      compute_attestation: {
        runtime: "runtime",
        carried_artifact: { type: "foreign-artifact" },
      },
    });
    expect((await verifyCapsule(carried.json)).capsuleId).toBe(
      carried.capsuleId,
    );
  });
  it.each(["agentInputDigest", "agentOutputDigest"] as const)(
    "rejects mixed %s on both internal binding paths",
    async (field) => {
      const mixed = { ...input, compute: { [field]: "a".repeat(64) } };
      const member = await build({ ...input, actionId: "member" });
      await expect(
        received(mixed, new Uint8Array([1]), "foreign-artifact"),
      ).rejects.toThrow("must not include agent input or output digest");
      await expect(buildComposition(mixed, [who(member)])).rejects.toThrow(
        "must not include agent input or output digest",
      );
    },
  );
  it("validates input and composition members before returning a Capsule", async () => {
    const member = await build({ ...input, actionId: "member" });
    await expect(
      received(
        { ...input, operator: "" },
        new Uint8Array([1]),
        "foreign-artifact",
      ),
    ).rejects.toThrow();
    await expect(
      buildComposition({ ...input, operator: "" }, [who(member)]),
    ).rejects.toThrow();
    await expect(
      buildComposition(input, [who({ ...member, capsuleId: "f".repeat(64) })]),
    ).rejects.toThrow("matching verified");
    await expect(
      buildComposition(input, [who(member), did(member)]),
    ).rejects.toThrow("duplicates Capsule ID");
    const composed = await buildComposition(input, [who(member)]);
    expect((await verifyCapsule(composed.json)).capsuleId).toBe(
      composed.capsuleId,
    );
  });

  it("rejects an AAC malformed member before composition or signing", async () => {
    const json = readFileSync(
      resolve(
        process.env.AAC_ROOT ?? "../agent-action-capsule",
        "vectors/capsule/neg-retention-declarant-missing-and-empty/input.json",
      ),
    );
    const value = JSON.parse(json.toString()) as Record<string, unknown>;
    const member = { value, json, capsuleId: String(value.capsule_id) };
    const key = createEd25519Identity(new Uint8Array(32));
    let signingKeyReads = 0;
    const identity = {
      publicKey: key.publicKey,
      get privateKey() {
        signingKeyReads += 1;
        return key.privateKey;
      },
    };
    await expect(sign(member, identity)).rejects.toMatchObject({
      name: "CapsuleVerificationError",
    });
    expect(signingKeyReads).toBe(0);
    await expect(buildComposition(input, [who(member)])).rejects.toMatchObject({
      name: "CapsuleVerificationError",
    });
  });
});
