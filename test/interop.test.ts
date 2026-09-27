import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  audit,
  buildComposition,
  can,
  createEd25519Identity,
  did,
  received,
  seal,
  sign,
  verifyEnvelope,
  who,
  type Input,
  type Result,
  verifyCapsule,
} from "../src/index.js";

const goRoot = resolve(
  process.env.CAPSULE_EMIT_GO_ROOT ?? "../capsule-emit-go",
);
// capsule-emit-go keeps the released -04 pack in format4-interop/ and its -05
// twin beside it in format4-interop-v05/. This producer stamps -05, so it
// replays the -05 pack; until that pack exists upstream, the single pack
// (regenerated live from Python main in CI) is the one to replay. Every pack
// present is also verified, whichever spec_version it carries.
const packRoots = [
  resolve(goRoot, "testdata/capsule-emit/format4-interop"),
  resolve(goRoot, "testdata/capsule-emit/format4-interop-v05"),
].filter((root) => existsSync(resolve(root, "vectors.json")));
const vectorRoot = packRoots.at(-1)!;
const spec = JSON.parse(
  readFileSync(resolve(vectorRoot, "input.json"), "utf8"),
) as {
  seed_hex: string;
  timestamp: string;
  operator: string;
  developer: string;
  disposition: {
    decision: string;
    approver: "policy";
    human_disposed: boolean;
  };
  records: Array<Record<string, unknown>>;
};
const identity = createEd25519Identity(Buffer.from(spec.seed_hex, "hex"));
const base = (record: Record<string, unknown>): Input => ({
  actionId: record.action_id as string,
  actionType: record.action_type as "decide",
  operator: spec.operator,
  developer: spec.developer,
  timestamp: spec.timestamp,
  disposition: {
    decision: spec.disposition.decision,
    approver: spec.disposition.approver,
    humanDisposed: spec.disposition.human_disposed,
    verdictClass: record.verdict as string,
  },
});

describe("Go/Python format-4 frozen vectors", () => {
  const results = new Map<string, Result>();
  for (const record of spec.records) {
    it(`${String(record.name)} replays byte for byte`, () => {
      let result: Result;
      if (record.operation === "seal")
        result = seal({
          capsule: base(record),
          payload: record.payload,
          ...(record.agent_output === undefined
            ? {}
            : { agentOutput: record.agent_output }),
          ...(record.model === undefined
            ? {}
            : {
                model: {
                  provider: String(
                    (record.model as Record<string, string>).provider,
                  ),
                  modelId: String(
                    (record.model as Record<string, string>).model_id,
                  ),
                },
              }),
          ...(record.runtime === undefined
            ? {}
            : { runtime: record.runtime as string }),
          identity,
        });
      else if (record.operation === "received") {
        const built = received(
          base(record),
          Buffer.from(record.artifact_utf8 as string),
          record.artifact_type as string,
        );
        result = {
          capsuleId: built.capsuleId,
          payload: built.json,
          envelope: sign(built, identity),
        };
      } else {
        const members = (
          record.members as Array<{ slot: string; record: string }>
        ).map((item) =>
          ({ who, can, did, audit })[item.slot]!(results.get(item.record)!),
        );
        const built = buildComposition(base(record), members);
        result = {
          capsuleId: built.capsuleId,
          payload: built.json,
          envelope: sign(built, identity),
        };
      }
      const directory = resolve(vectorRoot, "valid", record.name as string);
      results.set(record.name as string, result);
      expect(
        Buffer.from(result.payload).equals(
          readFileSync(resolve(directory, "capsule.detached.jcs")),
        ),
      ).toBe(true);
      expect(
        Buffer.from(result.envelope).equals(
          readFileSync(resolve(directory, "envelope.cose")),
        ),
      ).toBe(true);
      expect(verifyEnvelope(result.capsuleId, result.envelope).ok).toBe(true);
    });
  }
});

describe("Go/Python format-4 packs verify whatever spec_version they carry", () => {
  for (const root of packRoots) {
    const manifest = JSON.parse(
      readFileSync(resolve(root, "vectors.json"), "utf8"),
    ) as { profile: string; cases: Array<{ name: string; path: string }> };
    for (const item of manifest.cases)
      it(`${manifest.profile} ${item.name} verifies with its envelope`, () => {
        const directory = resolve(root, item.path);
        const expected = JSON.parse(
          readFileSync(resolve(directory, "expected.json"), "utf8"),
        ) as { capsule_id: string };
        const result = verifyCapsule(
          readFileSync(resolve(directory, "capsule.detached.jcs")),
        );
        expect(result.capsuleId).toBe(expected.capsule_id);
        expect(
          verifyEnvelope(
            expected.capsule_id,
            readFileSync(resolve(directory, "envelope.cose")),
          ).ok,
        ).toBe(true);
      });
  }
});
