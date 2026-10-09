import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  computeCapsuleId,
  decodeStrictJson,
  verifyClass1,
  verifyStore,
  type ParsedJson,
} from "../src/aac/index.js";
import { verifyCapsule } from "../src/verify.js";
const root = resolve(
  process.env.AAC_ROOT ?? "../agent-action-capsule",
  "vectors",
  "capsule",
);
const manifest = JSON.parse(
  readFileSync(resolve(root, "vectors.json"), "utf8"),
) as {
  cases: Array<{
    name: string;
    kind: string;
  }>;
};
describe("complete upstream AAC corpus", () => {
  for (const item of manifest.cases)
    it(item.name, async () => {
      const input = decodeStrictJson(
        readFileSync(resolve(root, item.name, "input.json")),
      );
      const expected = JSON.parse(
        readFileSync(resolve(root, item.name, "expected.json"), "utf8"),
      ) as {
        ok?: boolean;
        capsule_id_recomputed?: string;
        exception?: string | null;
        derived?: Record<string, string>;
        findings?: Array<{
          code: string;
        }>;
        results?: Array<{
          ok: boolean;
          capsule_id_recomputed?: string;
          findings: Array<{
            code: string;
          }>;
        }>;
      };
      if (item.kind === "canonical") {
        if (expected.exception !== null)
          await expect(
            computeCapsuleId(input as Record<string, ParsedJson>),
          ).rejects.toThrow();
        else
          expect(
            await computeCapsuleId(input as Record<string, ParsedJson>),
          ).toBe(expected.capsule_id_recomputed);
        return;
      }
      if (item.kind === "store") {
        const ledger = (
          input as {
            ledger: ParsedJson[];
          }
        ).ledger;
        const actual = await verifyStore(ledger);
        expect(actual.map((result) => result.ok)).toEqual(
          expected.results!.map((result) => result.ok),
        );
        expect(actual.map((result) => result.capsuleId)).toEqual(
          expected.results!.map((result) => result.capsule_id_recomputed),
        );
        expect(
          actual.map((result) =>
            result.findings.map((finding) => finding.code),
          ),
        ).toEqual(
          expected.results!.map((result) =>
            result.findings.map((finding) => finding.code),
          ),
        );
        return;
      }
      const actual = await verifyClass1(input);
      expect(actual.ok).toBe(expected.ok);
      expect(actual.capsuleId ?? null).toBe(
        expected.capsule_id_recomputed ?? null,
      );
      expect(actual.assurance).toEqual(expected.derived);
      expect(actual.findings.map((finding) => finding.code)).toEqual(
        (expected.findings ?? []).map((finding) => finding.code),
      );
    });
});
describe("reference parity edge cases", () => {
  it("preserves the complete authoritative finding result on producer rejection", async () => {
    const data = readFileSync(
      resolve(root, "neg-retention-declarant-missing-and-empty", "input.json"),
    );
    const reference = await verifyClass1(decodeStrictJson(data));
    expect(reference.ok).toBe(false);
    await expect(verifyCapsule(data)).rejects.toMatchObject({
      result: reference,
    });
  });
  const fixture = (): Record<string, ParsedJson> =>
    decodeStrictJson(
      readFileSync(resolve(root, "pos-executed-confirmed", "input.json")),
    ) as Record<string, ParsedJson>;
  it("checks effect_attestation presence independently of its type", async () => {
    const capsule = fixture();
    const effect = capsule.effect as Record<string, ParsedJson>;
    effect.effect_attestation = decodeStrictJson("1");
    expect(
      (await verifyClass1(capsule)).findings.some(
        (finding) => finding.code === "effect_attestation_missing",
      ),
    ).toBe(false);
    effect.status = "planned";
    expect(
      (await verifyClass1(capsule)).findings.some(
        (finding) => finding.code === "effect_attestation_present",
      ),
    ).toBe(true);
  });
  it("treats a null effect_attestation as absent like the references", async () => {
    const capsule = fixture();
    const effect = capsule.effect as Record<string, ParsedJson>;
    effect.effect_attestation = null;
    expect(
      (await verifyClass1(capsule)).findings.some(
        (finding) => finding.code === "effect_attestation_missing",
      ),
    ).toBe(true);
    effect.status = "planned";
    expect(
      (await verifyClass1(capsule)).findings.some(
        (finding) => finding.code === "effect_attestation_present",
      ),
    ).toBe(false);
  });
  it("does not attempt ID computation when the identity profile is invalid", async () => {
    for (const name of [
      "neg-float-in-digest-field",
      "neg-unsafe-integer-in-digest-field",
    ]) {
      const capsule = decodeStrictJson(
        readFileSync(resolve(root, name, "input.json")),
      ) as Record<string, ParsedJson>;
      capsule.format_version = "4";
      capsule.canonicalization_id = decodeStrictJson("4");
      const codes = (await verifyClass1(capsule)).findings.map(
        (finding) => finding.code,
      );
      expect(codes).toContain("canonicalization_id_not_string");
      expect(codes).not.toContain("capsule_id_uncomputable");
    }
  });
  it.each([
    ["1", "jcs", "unsupported_format_version"],
    ["2", undefined, "unsupported_format_version"],
    ["2", null, "unsupported_format_version"],
    ["3", "jcs", "unsupported_format_version"],
    ["4", undefined, "canonicalization_id_missing"],
    ["4", null, "canonicalization_id_not_string"],
    ["4", "", "canonicalization_profile_mismatch"],
    ["4", "jcs-n", "canonicalization_profile_mismatch"],
  ] as const)(
    "does not derive an ID finding for format %s and canonicalization %s",
    async (formatVersion, canonicalizationId, expectedCode) => {
      const capsule = fixture();
      capsule.format_version = formatVersion;
      if (canonicalizationId === undefined) delete capsule.canonicalization_id;
      else capsule.canonicalization_id = canonicalizationId;
      const result = await verifyClass1(capsule);
      const codes = result.findings.map((finding) => finding.code);
      expect(codes).toContain(expectedCode);
      expect(codes).not.toContain("capsule_id_uncomputable");
      expect(result.capsuleId).toBeUndefined();
    },
  );
  it("reports assurance overclaims when optional evidence is absent", async () => {
    const capsule = fixture();
    delete capsule.chain;
    delete capsule.cross_party;
    capsule.assurance = {
      attestation_mode: "anchored",
      ledger_mode: "anchored",
      cross_party_rung: "full_bilateral",
    };
    expect(
      (await verifyClass1(capsule)).findings.filter(
        (finding) => finding.code === "assurance_overclaim",
      ),
    ).toHaveLength(3);
  });
  it("matches reference disposition presence and type findings", async () => {
    const capsule = fixture();
    const disposition = capsule.disposition as Record<string, ParsedJson>;
    disposition.decision = decodeStrictJson("5");
    disposition.human_disposed = "not-a-boolean";
    const codes = (await verifyClass1(capsule)).findings.map(
      (finding) => finding.code,
    );
    expect(codes).not.toContain("missing_required_field");
    expect(codes).toContain("field_not_bool");
    delete disposition.human_disposed;
    const missingCodes = (await verifyClass1(capsule)).findings.map(
      (finding) => finding.code,
    );
    expect(missingCodes).toContain("field_not_bool");
    expect(missingCodes).not.toContain("missing_required_field");
  });
});
