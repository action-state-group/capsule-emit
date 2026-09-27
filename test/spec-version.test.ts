// The -05 wire: this package stamps -05, and verifies -04, -05 and
// unrecognized spec_version values alike.
//
// Draft -05: a producer conforming to -05 emits spec_version -05; a verifier
// accepts -04 and -05 and never rejects solely for either; spec_version
// selects no algorithm, and an unrecognized value is informational, never by
// itself a rejection. The released -04 upstream vectors stay untouched; the
// -05 twins used here sit beside them in agent-action-capsule.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { describe, expect, it } from "vitest";
import {
  ACCEPTED_SPEC_VERSIONS,
  build,
  buildComposition,
  createEd25519Identity,
  did,
  received,
  seal,
  SPEC_VERSION,
  verifyCapsule,
  verifyEnvelope,
  who,
  type Input,
} from "../src/index.js";
import {
  computeCapsuleId,
  decodeStrictJson,
  jcs,
  verifyClass1,
  type ParsedJson,
} from "../src/aac/index.js";

const SPEC_04 = "draft-mih-scitt-agent-action-capsule-04";
const SPEC_05 = "draft-mih-scitt-agent-action-capsule-05";
const UNRECOGNIZED = "draft-mih-scitt-agent-action-capsule-99";

const aacRoot = resolve(process.env.AAC_ROOT ?? "../agent-action-capsule");
const readJson = (...path: string[]): Record<string, ParsedJson> =>
  decodeStrictJson(readFileSync(resolve(aacRoot, ...path))) as Record<
    string,
    ParsedJson
  >;

const input: Input = {
  actionId: "spec-version/example",
  actionType: "decide",
  operator: "example-org",
  developer: "example-agent@v1",
  timestamp: "2026-09-26T00:00:00Z",
  disposition: {
    decision: "accept",
    approver: "policy",
    humanDisposed: false,
    verdictClass: "executed",
  },
};

/** Re-stamp a record's spec_version and recompute its Capsule ID. */
const restamp = (
  record: Record<string, ParsedJson>,
  specVersion: string,
): Record<string, ParsedJson> => {
  const copy: Record<string, ParsedJson> = {
    ...record,
    spec_version: specVersion,
  };
  copy.capsule_id = computeCapsuleId(copy);
  return copy;
};

describe("producer: what this package stamps", () => {
  it("SPEC_VERSION is -05 and ACCEPTED_SPEC_VERSIONS is -04 then -05", () => {
    expect(SPEC_VERSION).toBe(SPEC_05);
    expect(ACCEPTED_SPEC_VERSIONS).toEqual([SPEC_04, SPEC_05]);
  });

  it("matches the constants of the pinned agent-action-capsule TypeScript reference", async () => {
    // Fails if the AAC checkout CI uses does not carry the -05 wire.
    const model = (await import(
      pathToFileURL(resolve(aacRoot, "ts/src/model.ts")).href
    )) as {
      CURRENT_SPEC_VERSION: string;
      ACCEPTED_SPEC_VERSIONS: readonly string[];
    };
    expect(SPEC_VERSION).toBe(model.CURRENT_SPEC_VERSION);
    expect(ACCEPTED_SPEC_VERSIONS).toEqual(model.ACCEPTED_SPEC_VERSIONS);
  });

  it("build, received, composition and seal all stamp spec_version -05", () => {
    const identity = createEd25519Identity(Buffer.alloc(32, 7));
    const built = build(input);
    const carried = received(input, new Uint8Array([1, 2]), "foreign-artifact");
    const composed = buildComposition(input, [who(built), did(carried)]);
    const sealed = seal({ capsule: input, payload: { task: 1 }, identity });
    for (const value of [built.value, carried.value, composed.value])
      expect(value.spec_version).toBe(SPEC_05);
    expect(verifyCapsule(sealed.payload).capsuleId).toBe(sealed.capsuleId);
    expect(
      (
        JSON.parse(new TextDecoder().decode(sealed.payload)) as {
          spec_version: string;
        }
      ).spec_version,
    ).toBe(SPEC_05);
  });
});

describe("verifier: -04 and -05 both verify", () => {
  it("the released -04 cross-language seal input and its -05 twin both verify and differ only by spec_version", () => {
    const v04 = readJson("vectors", "cross-language", "seal-input.json");
    const v05 = readJson("vectors", "cross-language", "seal-input-v05.json");
    expect([v04.spec_version, v05.spec_version]).toEqual([SPEC_04, SPEC_05]);
    const { spec_version: _a, ...rest04 } = v04;
    const { spec_version: _b, ...rest05 } = v05;
    expect(jcs(rest04)).toEqual(jcs(rest05));

    const ids = [v04, v05].map((body) => {
      const sealed = { ...body, capsule_id: computeCapsuleId(body) };
      const result = verifyCapsule(jcs(sealed));
      expect(result.ok).toBe(true);
      return result.capsuleId;
    });
    // spec_version participates in capsule_id but selects no algorithm.
    expect(ids[0]).not.toBe(ids[1]);
  });

  it("the released -04 chain-committed corpus case and its -05 twin both pass Class 1", () => {
    for (const [name, spec] of [
      ["pos-v4-jcs-chain-committed", SPEC_04],
      ["pos-v05-spec-version-chain-committed", SPEC_05],
    ] as const) {
      const capsule = readJson("vectors", "capsule", name, "input.json");
      expect(capsule.spec_version).toBe(spec);
      expect(verifyClass1(capsule).ok).toBe(true);
      expect(verifyCapsule(jcs(capsule)).capsuleId).toBe(capsule.capsule_id);
    }
  });

  it("a record this package builds re-stamped -04 still verifies, before and after signing", () => {
    const identity = createEd25519Identity(Buffer.alloc(32, 9));
    const sealed = seal({ capsule: input, payload: { task: 2 }, identity });
    const v05 = decodeStrictJson(sealed.payload) as Record<string, ParsedJson>;
    const v04 = restamp(v05, SPEC_04);
    expect(verifyCapsule(jcs(v04)).capsuleId).toBe(v04.capsule_id);
    expect(verifyEnvelope(sealed.capsuleId, sealed.envelope).ok).toBe(true);
  });
});

describe("verifier: an unrecognized spec_version is not a rejection", () => {
  it("verifyCapsule accepts a record carrying an unrecognized spec_version", () => {
    const record = restamp(
      decodeStrictJson(build(input).json) as Record<string, ParsedJson>,
      UNRECOGNIZED,
    );
    const result = verifyCapsule(jcs(record));
    expect(result.ok).toBe(true);
    expect(result.capsuleId).toBe(record.capsule_id);
    expect(
      result.findings.filter((finding) => finding.severity === "error"),
    ).toEqual([]);
  });

  it("a Class 1 finding never names spec_version for -04, -05 or an unrecognized value", () => {
    const base = decodeStrictJson(build(input).json) as Record<
      string,
      ParsedJson
    >;
    for (const spec of [SPEC_04, SPEC_05, UNRECOGNIZED]) {
      const result = verifyClass1(restamp(base, spec));
      expect(result.ok).toBe(true);
      expect(
        result.findings.filter((finding) =>
          finding.detail.includes("spec_version"),
        ),
      ).toEqual([]);
    }
  });

  it("verifyCapsule still rejects an unsupported format_version, which does select the algorithm", () => {
    const record = decodeStrictJson(build(input).json) as Record<
      string,
      ParsedJson
    >;
    record.format_version = "3";
    expect(() => verifyCapsule(jcs(record))).toThrow(
      "unsupported Capsule profile",
    );
  });
});
