import {
  decodeCapsuleJson,
  verifyClass1,
  type VerificationResult,
} from "./aac/index.js";
import { CANONICALIZATION_ID, FORMAT_VERSION } from "./types.js";

export { isV4IrreversibilityClass } from "./aac/index.js";

export class CapsuleVerificationError extends Error {
  public constructor(public readonly result: VerificationResult) {
    super(
      `AAC Class 1 verification failed: ${result.findings.map((finding) => `check=${finding.check ?? "none"} severity=${finding.severity} code=${finding.code} detail=${finding.detail}`).join("; ")}`,
    );
    this.name = "CapsuleVerificationError";
  }
}

export function decodePayload(
  data: Uint8Array | string,
): ReturnType<typeof decodeCapsuleJson> {
  return decodeCapsuleJson(data);
}

export async function verifyCapsule(
  data: Uint8Array | string,
): Promise<VerificationResult> {
  const payload = decodeCapsuleJson(data);
  // spec_version is deliberately not compared: it selects no algorithm, and
  // -04, -05, or an unrecognized revision is never by itself a rejection.
  if (
    payload.format_version !== FORMAT_VERSION ||
    payload.canonicalization_id !== CANONICALIZATION_ID
  )
    throw new TypeError(
      `unsupported Capsule profile: only AAC format 4 with canonicalization_id ${JSON.stringify(CANONICALIZATION_ID)} is supported`,
    );
  const result = await verifyClass1(payload);
  if (!result.ok) throw new CapsuleVerificationError(result);
  return result;
}
