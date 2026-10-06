import {
  signCapsuleId,
  verifyProducerEnvelope,
} from "@action-state-group/agent-action-capsule/core";
import type {
  BuiltPayload,
  EnvelopeVerificationResult,
  SigningIdentity,
} from "./types.js";
import { verifyCapsule } from "./verify.js";

export {
  createEd25519Identity,
  createSigningIdentity,
  signCapsuleId,
} from "@action-state-group/agent-action-capsule/core";

/** Verify the complete Capsule before signing its matching content identity. */
export async function sign(
  capsule: BuiltPayload,
  identity: SigningIdentity,
): Promise<Uint8Array> {
  const verified = await verifyCapsule(capsule.json);
  if (verified.capsuleId !== capsule.capsuleId)
    throw new TypeError("built Capsule does not match Capsule ID");
  return signCapsuleId(capsule.capsuleId, identity);
}

/** Verify the exact AAC Producer Envelope through the authoritative reference. */
export async function verifyEnvelope(
  capsuleId: string,
  data: Uint8Array,
): Promise<EnvelopeVerificationResult> {
  return verifyProducerEnvelope(capsuleId, data);
}
