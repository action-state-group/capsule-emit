import { generateKeyPairSync } from "node:crypto";
import { seal } from "../../src/build.js";
import { createEd25519Identity } from "../../src/envelope.js";
import {
  AGENT_OUTPUT_DIGEST,
  PAYLOAD_DIGEST,
  type Record,
} from "../../src/artifact/index.js";
export function utf8(value: string): Uint8Array {
  return new Uint8Array(Buffer.from(value, "utf8"));
}
/** A locally signed record with a bound payload, bound output, and an attachment. */
export async function makeRecord(actionId = "store-fixture"): Promise<{
  record: Record;
  trusted: Uint8Array;
}> {
  const identity = createEd25519Identity(
    generateKeyPairSync("ed25519").privateKey,
  );
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
  return {
    trusted: identity.publicKey,
    record: {
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
    },
  };
}
