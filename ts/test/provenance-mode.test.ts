import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { CapsuleVerificationError, verifyCapsule } from "../src/verify.js";

// Class 1 check 9 (provenance_mode) arrived with agent-action-capsule 0.4.0,
// so verifyCapsule now refuses a Capsule it accepted under 0.1.0. These are
// the Go module's copies of AAC's provenance-mode vectors, replayed as
// go/provenance_mode_test.go does.
const root = resolve(
  process.env.CAPSULE_EMIT_GO_ROOT ?? "../go",
  "testdata",
  "provenance-mode-vectors",
);

describe("provenance_mode vectors (Class 1 check 9)", () => {
  it.each([
    "pos-provenance-mode-backfilled",
    "neg-provenance-mode-time-rung-overclaim",
    "neg-provenance-mode-backfilled-missing-fields",
    "neg-provenance-mode-time-laundering",
  ])("%s verifies as committed", async (name) => {
    const input = readFileSync(resolve(root, name, "input.json"));
    const expected = JSON.parse(
      readFileSync(resolve(root, name, "expected.json"), "utf8"),
    ) as { ok: boolean; findings: Array<{ code: string }> };
    const wantCodes = expected.findings.map((finding) => finding.code);
    if (expected.ok) {
      const result = await verifyCapsule(input);
      expect(result.findings.map((finding) => finding.code)).toEqual(wantCodes);
      return;
    }
    const error: unknown = await verifyCapsule(input).catch((e) => e);
    expect(error).toBeInstanceOf(CapsuleVerificationError);
    expect(
      (error as CapsuleVerificationError).result.findings.map(
        (finding) => finding.code,
      ),
    ).toEqual(wantCodes);
  });
});
