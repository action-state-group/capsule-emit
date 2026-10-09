import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, it } from "vitest";
import {
  decodeStrictJson,
  verifyClass1,
  type ParsedJson,
} from "../src/aac/index.js";
const root = resolve(process.env.AAC_ROOT ?? "../agent-action-capsule");
const vectors = JSON.parse(
  readFileSync(resolve(root, "go/verify/testdata/vocabulary.json"), "utf8"),
) as {
  cases: {
    name: string;
    capsule: ParsedJson;
    ok: boolean;
    assurance: Record<string, string>;
    findings: {
      code: string;
      severity: string;
    }[];
  }[];
};
for (const vector of vectors.cases) {
  it(`matches Python vocabulary diagnostics and assurance: ${vector.name}`, async () => {
    const result = await verifyClass1(
      decodeStrictJson(JSON.stringify(vector.capsule)),
    );
    expect(result.ok).toBe(vector.ok);
    expect(result.assurance).toEqual(vector.assurance);
    expect(
      result.findings.map(({ code, severity }) => ({ code, severity })),
    ).toEqual(vector.findings);
  });
}
