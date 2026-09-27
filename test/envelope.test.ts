import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { verifyEnvelope } from "../src/index.js";

const root = resolve(
  process.env.AAC_ROOT ?? "../agent-action-capsule",
  "vectors",
  "producer-envelope",
);
const manifest = JSON.parse(
  readFileSync(resolve(root, "vectors.json"), "utf8"),
) as { cases: Array<{ name: string }> };
describe("upstream Producer Envelope corpus", () => {
  for (const item of manifest.cases)
    it(item.name, () => {
      const directory = resolve(root, item.name);
      const expected = JSON.parse(
        readFileSync(resolve(directory, "expected.json"), "utf8"),
      ) as { ok: boolean; findings?: Array<{ code: string }> };
      const result = verifyEnvelope(
        readFileSync(resolve(directory, "capsule_id.txt"), "utf8").trim(),
        readFileSync(resolve(directory, "envelope.cose")),
      );
      expect(result.ok).toBe(expected.ok);
      if (expected.findings !== undefined)
        expect(result.findings.map((finding) => finding.code)).toEqual(
          expected.findings.map((finding) => finding.code),
        );
    });
});
