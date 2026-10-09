import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  build,
  SPEC_VERSION,
  verifyCapsule,
  type Reference,
} from "../src/index.js";
import {
  computeCapsuleId,
  decodeStrictJson,
  jcs,
  verifyClass1,
  type ParsedJson,
} from "../src/aac/index.js";
const root = resolve(
  process.env.AAC_ROOT ?? "../agent-action-capsule",
  "vectors",
  "capsule",
);
// The shared draft-04 cross-record reference vectors now live in the
// upstream capsule corpus as reference-* cases, which test/class1.test.ts
// runs in full; this file keeps the producer-side and extension checks.
const vector = (name: string): Record<string, ParsedJson> =>
  decodeStrictJson(
    readFileSync(resolve(root, `reference-${name}`, "input.json")),
  ) as Record<string, ParsedJson>;
describe("shared draft-04 cross-record reference vectors", () => {
  it("accepts a caller-registered citation purpose without findings", async () => {
    const capsule = vector("future-purpose");
    const custom = await verifyClass1(capsule, new Set(["a".repeat(64)]), {
      citation_purpose: new Set(["example-purpose"]),
    });
    expect(custom.ok).toBe(true);
    expect(custom.findings).toEqual([]);
  });
  it("constructs the same cited Capsule bytes as Python and commits the citation", async () => {
    // The released vector is -04; this producer stamps -05. Its bytes are
    // the vector's with only spec_version (and so capsule_id) changed.
    const expected = vector("external-capsule");
    expected.spec_version = SPEC_VERSION;
    expected.capsule_id = await computeCapsuleId(expected);
    const reference: Reference = {
      type: "agent-action-capsule",
      digestAlg: "SHA-256",
      digest: "b".repeat(64),
      citationPurpose: "responds_to",
    };
    const built = await build({
      actionId: "reference/example",
      actionType: "fyi",
      operator: "example-org",
      developer: "example-agent@v1",
      timestamp: "2026-09-07T12:00:00Z",
      chain: { parentCapsuleId: "a".repeat(64), relation: "confirms" },
      references: [reference],
    });
    expect(new TextDecoder().decode(built.json)).toBe(
      new TextDecoder().decode(jcs(expected)),
    );
    const tampered = JSON.parse(new TextDecoder().decode(built.json));
    tampered.references[0].citation_purpose = "acted_on";
    expect(await computeCapsuleId(tampered)).not.toBe(built.capsuleId);
    await expect(verifyCapsule(jcs(tampered))).rejects.toThrow(
      "capsule_id_mismatch",
    );
  });
  it("rejects a typed citation that duplicates the chain parent", async () => {
    await expect(
      build({
        actionId: "reference/example",
        actionType: "fyi",
        operator: "example",
        developer: "example",
        timestamp: "2026-09-07T12:00:00Z",
        chain: { parentCapsuleId: "a".repeat(64), relation: "confirms" },
        references: [
          {
            type: "agent-action-capsule",
            digestAlg: "SHA-256",
            digest: "a".repeat(64),
          },
        ],
      }),
    ).rejects.toThrow("reference_duplicates_chain_parent");
  });
});
it("records opaque log coordinates and preserves absent versus empty references", async () => {
  const base = {
    actionId: "reference/example",
    actionType: "fyi" as const,
    operator: "example",
    developer: "example",
    timestamp: "2026-09-07T12:00:00Z",
  };
  const reference = {
    type: "agent-action-capsule",
    digestAlg: "SHA-256",
    digest: "b".repeat(64),
    logCoordinates: {
      log_id: "example",
      leaf_index: 0,
      inclusion_proof: "opaque",
    },
  };
  const built = await build({ ...base, references: [reference] });
  expect(built.value.references).toEqual([
    {
      type: reference.type,
      digest_alg: reference.digestAlg,
      digest: reference.digest,
      log_coordinates: reference.logCoordinates,
    },
  ]);
  await expect(
    build({
      ...base,
      references: [
        { ...reference, logCoordinates: { log_id: "example", leaf_index: 0 } },
      ],
    }),
  ).rejects.toThrow("reference_log_coordinates_malformed");
  const absent = await build(base);
  const empty = await build({ ...base, references: [] });
  expect(absent.value).not.toHaveProperty("references");
  expect(empty.value.references).toEqual([]);
  expect(absent.capsuleId).not.toBe(empty.capsuleId);
});
it("treats decoded numbers as scalars in reference object positions", async () => {
  const source = readFileSync(
    resolve(root, "reference-opaque-proof", "input.json"),
    "utf8",
  );
  for (const numericEntry of [true, false]) {
    const capsule = JSON.parse(source);
    if (numericEntry) capsule.references = [1];
    else capsule.references[0].log_coordinates = 7;
    capsule.capsule_id = await computeCapsuleId(capsule);
    const result = await verifyClass1(
      decodeStrictJson(JSON.stringify(capsule)),
      new Set(["a".repeat(64)]),
    );
    expect(result.ok).toBe(false);
    expect(result.findings.map((finding) => finding.code)).toEqual([
      numericEntry
        ? "reference_malformed"
        : "reference_log_coordinates_malformed",
    ]);
  }
});
it("detaches nested log coordinates from caller mutations", async () => {
  const coordinates = {
    log_id: "example",
    leaf_index: 0,
    inclusion_proof: { path: ["original"] },
  };
  const built = await build({
    actionId: "reference/mutation",
    actionType: "fyi",
    operator: "example",
    developer: "example",
    timestamp: "2026-09-07T12:00:00Z",
    references: [
      {
        type: "agent-action-capsule",
        digestAlg: "SHA-256",
        digest: "b".repeat(64),
        logCoordinates: coordinates,
      },
    ],
  });
  coordinates.leaf_index = 1;
  coordinates.inclusion_proof.path[0] = "changed";
  expect(jcs(built.value)).toEqual(built.json);
  expect(
    await computeCapsuleId(
      built.value as Parameters<typeof computeCapsuleId>[0],
    ),
  ).toBe(built.capsuleId);
});
