import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { CapsuleVerificationError, verifyCapsule } from "../src/verify.js";

// Class 1 check 9 (provenance_mode) arrived with agent-action-capsule 0.4.0,
// so verifyCapsule now refuses Capsules it accepted under 0.1.0. This replays
// AAC's provenance-mode corpus at AAC_COMMIT. Store cases (`{"ledger": [...]}`)
// exercise the duplicates-collapse pass, which AAC TS's verifyStore does not
// implement (AAC's own ts/test/conformance.test.ts skips them too).
const root = resolve(
  process.env.AAC_ROOT ?? "../../agent-action-capsule",
  "provenance-mode-vectors",
);
const manifest = JSON.parse(
  readFileSync(resolve(root, "vectors.json"), "utf8"),
) as { cases: Array<{ name: string; kind: string }> };

interface Expected {
  ok: boolean;
  findings: Array<{ code: string }>;
}
const codes = (findings: ReadonlyArray<{ code: string }>): string[] =>
  findings.map((finding) => finding.code);

describe("provenance_mode corpus (Class 1 check 9)", () => {
  const bare = manifest.cases.filter((item) => item.kind !== "store");
  it("has the bare-Capsule cases", () => expect(bare.length).toBe(23));
  for (const item of bare)
    it(item.name, async () => {
      const input = readFileSync(resolve(root, item.name, "input.json"));
      const expected = JSON.parse(
        readFileSync(resolve(root, item.name, "expected.json"), "utf8"),
      ) as Expected;
      if (expected.ok) {
        const result = await verifyCapsule(input);
        expect(codes(result.findings)).toEqual(codes(expected.findings));
        return;
      }
      const error: unknown = await verifyCapsule(input).catch((e) => e);
      expect(error).toBeInstanceOf(CapsuleVerificationError);
      expect(
        codes((error as CapsuleVerificationError).result.findings),
      ).toEqual(codes(expected.findings));
    });
});
