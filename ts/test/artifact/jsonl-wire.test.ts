import { describe, expect, it } from "vitest";

import {
  AGENT_OUTPUT_DIGEST,
  ArtifactError,
  PAYLOAD_DIGEST,
  prepare,
  type Record,
} from "../../src/artifact/index.js";
import { decodeRecord, encodeRecord } from "../../src/artifact/jsonl.js";

import { utf8 } from "./fixture.js";

// Shared cross-language golden line. The identical bytes are pinned in
// capsule-emit-go/artifact/jsonl/wire_golden_test.go over the same synthetic
// record, proving the Go and TypeScript JSONL stores agree on one byte-identical
// wire format. A drift in either encoder (key name, key order, base64, or
// omitempty behavior) fails both suites. The synthetic record uses opaque bytes
// so producer-format changes do not move the golden.
const GOLDEN =
  '{"capsule_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","capsule":"e30=","producer_envelope":"AAEC/w==","artifacts":[{"name":"payload","binding":"model_attestation.compute_attestation.agent_input_digest","content":"e30K","state":"present","content_sha256":"ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"},{"name":"agent_output","binding":"model_attestation.compute_attestation.agent_output_digest","state":"never_retained"}]}';

function synthetic(): Record {
  return prepare({
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
}

describe("jsonl wire format (cross-language golden)", () => {
  it("encodes the synthetic record to the Go golden byte-for-byte", () => {
    expect(encodeRecord(synthetic())).toBe(GOLDEN);
  });

  it("decodes the golden line back to the record", () => {
    expect(decodeRecord(GOLDEN)).toEqual(synthetic());
  });

  it("throws ArtifactError('corrupt') on null or non-object lines, not a raw TypeError", () => {
    const malformed = [
      "null",
      "123",
      '"a string"',
      "[1,2,3]",
      JSON.stringify({
        capsule_id: "a".repeat(64),
        capsule: "e30=",
        producer_envelope: "AAEC/w==",
        artifacts: [null],
      }),
    ];
    for (const line of malformed) {
      let err: unknown;
      try {
        decodeRecord(line);
      } catch (e) {
        err = e;
      }
      expect(err).toBeInstanceOf(ArtifactError);
      expect((err as ArtifactError).code).toBe("corrupt");
    }
  });
});
