// Compatibility entry: record verification and canonicalization remain owned by AAC.
export {
  asJsonObject,
  computeCapsuleId,
  decodeCapsuleJson,
  decodeStrictJson,
  isHex64,
  jcs,
  JcsFloatError,
  JcsUnsafeIntegerError,
  JsonNumber,
  jsonDigest,
  sha256Hex,
  verifyClass1,
  verifyStore,
} from "@action-state-group/agent-action-capsule/core";
export type {
  Finding,
  VerificationResult,
  JsonValue,
  ParsedJson,
} from "@action-state-group/agent-action-capsule/core";
import { registries } from "@action-state-group/agent-action-capsule/core";

/** Report membership in AAC's irreversibility registry. */
export function isV4IrreversibilityClass(value: string): boolean {
  return registries.irreversibility_class.has(value);
}
