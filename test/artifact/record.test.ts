import { generateKeyPairSync } from "node:crypto";
import { describe, expect, it } from "vitest";
import { createEd25519Identity } from "../../src/envelope.js";
import { digestJSON, seal } from "../../src/build.js";
import {
  AGENT_OUTPUT_DIGEST,
  ArtifactError,
  EFFECT_REQUEST_DIGEST,
  EFFECT_RESPONSE_DIGEST,
  PAYLOAD_DIGEST,
  prepare,
  storageChecksum,
  verify,
  type Artifact,
  type Record,
} from "../../src/artifact/index.js";
function utf8(value: string): Uint8Array {
  return new Uint8Array(Buffer.from(value, "utf8"));
}
async function fixture(actionId = "identity-regression"): Promise<{
  record: Record;
  trusted: Uint8Array;
}> {
  const { privateKey } = generateKeyPairSync("ed25519");
  const identity = createEd25519Identity(privateKey);
  const payload = { question: "where next?", n: 2 };
  const output = { next_action: "inspect logs" };
  const sealed = await seal({
    capsule: {
      actionId,
      actionType: "fyi",
      operator: "test-operator",
      developer: "test-developer",
      timestamp: new Date(Date.UTC(2026, 8, 8, 1, 2, 3)),
    },
    payload,
    agentOutput: output,
    identity,
  });
  const record: Record = {
    capsuleId: sealed.capsuleId,
    capsule: sealed.payload,
    producerEnvelope: sealed.envelope,
    artifacts: [
      {
        name: "payload",
        binding: PAYLOAD_DIGEST,
        content: utf8(JSON.stringify(payload)),
        state: "present",
      },
      {
        name: "agent_output",
        binding: AGENT_OUTPUT_DIGEST,
        content: utf8(JSON.stringify(output)),
        state: "present",
      },
      {
        name: "private_note",
        content: utf8("not a producer-authenticated assertion"),
        state: "present",
      },
    ],
  };
  return { record, trusted: identity.publicKey };
}
describe("storageChecksum", () => {
  it("reproduces the Go golden value byte-for-byte", () => {
    // Same synthetic record as capsule-emit-go artifact/regression_test.go.
    const record = prepare({
      capsuleId: "a".repeat(64),
      capsule: utf8("{}"),
      producerEnvelope: new Uint8Array([0, 1, 2, 255]),
      artifacts: [
        {
          name: "payload",
          binding: PAYLOAD_DIGEST,
          content: utf8("{}\n"),
          state: "present",
        },
        {
          name: "agent_output",
          binding: AGENT_OUTPUT_DIGEST,
          state: "never_retained",
        },
      ],
    });
    const golden =
      "ac78a7aa2b9206e55c1932931afa35f3f5bee9ec836724e925c8e5256cf1ddb9";
    expect(storageChecksum(record)).toBe(golden);
    // Purge normalization must not move the value.
    const purged: Record = {
      ...record,
      artifacts: record.artifacts.map((a) =>
        a.name === "payload"
          ? { ...a, content: undefined, state: "purged" as const }
          : a,
      ),
    };
    expect(storageChecksum(purged)).toBe(golden);
  });
  it("matches Go for an empty inventory (json/v2 encodes it as [])", () => {
    // Authoritative value from capsule-emit-go Record.StorageChecksum() with no
    // artifacts. encoding/json/v2 encodes the empty inventory as `"artifacts":[]`,
    // which JSON.stringify reproduces; the v1 `null` form would diverge.
    const record: Record = {
      capsuleId: "a".repeat(64),
      capsule: utf8("{}"),
      producerEnvelope: new Uint8Array([0, 1, 2, 255]),
      artifacts: [],
    };
    expect(storageChecksum(record)).toBe(
      "33be5d36fbe3fcd89e5ec0a3ac5feb0ea764fa08053c34d0978c99fc2dfb71f4",
    );
  });
});
describe("verify", () => {
  it("authenticates a sealed record and reports bound preimages", async () => {
    const { record, trusted } = await fixture();
    const results = await verify(prepare(record), [trusted]);
    expect(results.get("payload")).toEqual({
      state: "present",
      bound: true,
      verified: true,
    });
    expect(results.get("agent_output")?.verified).toBe(true);
    // An unbound attachment is stored but never authenticated by the Capsule.
    expect(results.get("private_note")).toEqual({
      state: "present",
      bound: false,
      verified: false,
    });
  });
  it("rejects an untrusted signer", async () => {
    const { record } = await fixture();
    const other = createEd25519Identity(
      generateKeyPairSync("ed25519").privateKey,
    );
    await expect(verify(prepare(record), [other.publicKey])).rejects.toThrow(
      expect.objectContaining({ code: "untrusted_signer" }),
    );
  });
  it("detects a tampered bound original", async () => {
    const { record, trusted } = await fixture();
    const prepared = prepare(record);
    const tampered: Record = {
      ...prepared,
      artifacts: prepared.artifacts.map((a) =>
        a.name === "payload"
          ? { ...a, content: utf8('{"different":true}'), contentSha256: "" }
          : a,
      ),
    };
    await expect(verify(prepare(tampered), [trusted])).rejects.toThrow(
      expect.objectContaining({ code: "digest_mismatch" }),
    );
  });
  it("detects a corrupt unbound attachment", async () => {
    const { record, trusted } = await fixture();
    const prepared = prepare(record);
    const corrupt: Record = {
      ...prepared,
      artifacts: prepared.artifacts.map((a) =>
        a.name === "private_note"
          ? { ...a, content: utf8("changed attachment") }
          : a,
      ),
    };
    await expect(verify(corrupt, [trusted])).rejects.toThrow(
      expect.objectContaining({ code: "corrupt" }),
    );
  });
  it("verifies effect request/response bindings", async () => {
    const { privateKey } = generateKeyPairSync("ed25519");
    const identity = createEd25519Identity(privateKey);
    const request = { operation: "synthetic" };
    const response = { ok: true };
    const sealed = await seal({
      capsule: {
        actionId: "effect-bindings",
        actionType: "decide",
        operator: "test",
        developer: "test",
        timestamp: new Date(Date.UTC(2026, 8, 8, 0, 0, 0)),
        disposition: {
          decision: "accept",
          approver: "policy",
          humanDisposed: false,
          verdictClass: "executed",
        },
        effect: {
          type: "test.tool.send",
          status: "confirmed",
          irreversibilityClass: "one-way-consequential",
          effectAttestation: "gate-executed",
          requestDigest: await digestJSON(request),
          responseDigest: await digestJSON(response),
        },
      },
      payload: { synthetic: true },
      identity,
    });
    const artifacts: Artifact[] = [
      {
        name: "request",
        binding: EFFECT_REQUEST_DIGEST,
        content: utf8(JSON.stringify(request)),
        state: "present",
      },
      {
        name: "response",
        binding: EFFECT_RESPONSE_DIGEST,
        content: utf8(JSON.stringify(response)),
        state: "present",
      },
    ];
    const record: Record = {
      capsuleId: sealed.capsuleId,
      capsule: sealed.payload,
      producerEnvelope: sealed.envelope,
      artifacts,
    };
    const checks = await verify(prepare(record), [identity.publicKey]);
    expect(checks.get("request")?.verified).toBe(true);
    expect(checks.get("response")?.verified).toBe(true);
  });
  it("rejects an input record that declares a purged artifact", async () => {
    const { record } = await fixture();
    record.artifacts[0]!.state = "purged";
    expect(() => prepare(record)).toThrow(
      expect.objectContaining({ code: "purged" }),
    );
  });
  it("exposes ArtifactError as the thrown type", async () => {
    await expect(
      verify({ ...(await fixture()).record, capsuleId: "x" }, []),
    ).rejects.toThrow(ArtifactError);
  });
});
