import { createHash } from "node:crypto";
import { computeCapsuleId, jcs, jsonDigest } from "./aac/index.js";
import { isHex64 } from "./aac/index.js";
import { sign } from "./envelope.js";
import {
  CANONICALIZATION_ID,
  FORMAT_VERSION,
  SPEC_VERSION,
  type BuiltPayload,
  type Input,
  type Result,
  type SealInput,
  type Slot,
  type SlotMember,
} from "./types.js";
import { verifyCapsule } from "./verify.js";

const slots: readonly Slot[] = ["who", "can", "did", "audit"];
const present = (value: string | undefined): value is string =>
  value !== undefined && value !== "";
const requireText = (name: string, value: string): void => {
  if (value.trim() === "") throw new TypeError(`${name} is required`);
};

/** Format UTC like Python datetime: microseconds when non-zero, no fraction otherwise. */
export function formatTimestamp(value: Date | string): string {
  if (value instanceof Date) {
    if (Number.isNaN(value.valueOf()))
      throw new TypeError("timestamp is invalid");
    const iso = value.toISOString();
    return iso.endsWith(".000Z")
      ? `${iso.slice(0, -5)}Z`
      : `${iso.slice(0, -1)}000Z`;
  }
  const match =
    /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$/u.exec(
      value,
    );
  if (match === null)
    throw new TypeError("timestamp must be RFC 3339 with timezone");
  const fraction = (match[2] ?? "").padEnd(9, "0");
  const milliseconds = fraction.slice(0, 3);
  const date = new Date(`${match[1]}.${milliseconds}${match[3]}`);
  if (Number.isNaN(date.valueOf())) throw new TypeError("timestamp is invalid");
  const microRemainder = fraction.slice(3, 6);
  const utc = date.toISOString();
  const base = utc.slice(0, 19);
  const micros = `${utc.slice(20, 23)}${microRemainder}`;
  return /^0+$/u.test(micros) ? `${base}Z` : `${base}.${micros}Z`;
}

function validate(input: Input): void {
  requireText("action id", input.actionId);
  requireText("operator", input.operator);
  requireText("developer", input.developer);
  if (input.actionType !== "fyi" && input.actionType !== "decide")
    throw new TypeError('action type must be "fyi" or "decide"');
  if (input.actionType === "decide" && input.disposition === undefined)
    throw new TypeError("decide action requires a disposition");
  if (input.disposition !== undefined) {
    requireText("disposition decision", input.disposition.decision);
    if (
      input.disposition.humanDisposed &&
      input.disposition.approver !== "human"
    )
      throw new TypeError("human-disposed decision requires human approver");
    if (
      present(input.disposition.reasonDigest) &&
      !isHex64(input.disposition.reasonDigest)
    )
      throw new TypeError(
        "disposition reason digest must be 64 lowercase hex characters",
      );
  }
  if (input.chain !== undefined) {
    if (!isHex64(input.chain.parentCapsuleId))
      throw new TypeError(
        "chain parent capsule id must be 64 lowercase hex characters",
      );
    requireText("chain relation", input.chain.relation);
    if (input.chain.relation === "epoch_opens" && !present(input.epochId))
      throw new TypeError("epoch_opens chain requires epoch id");
  }
  if (
    input.model !== undefined &&
    !present(input.model.provider) &&
    !present(input.model.modelId)
  )
    throw new TypeError("model must include provider or model id");
  for (const digest of [
    input.compute?.agentInputDigest,
    input.compute?.agentOutputDigest,
  ])
    if (present(digest) && !isHex64(digest))
      throw new TypeError(
        "compute attestation digest must be 64 lowercase hex characters",
      );
  if (input.effect !== undefined) {
    requireText("effect type", input.effect.type);
    requireText(
      "effect irreversibility class",
      input.effect.irreversibilityClass,
    );
    for (const digest of [
      input.effect.requestDigest,
      input.effect.responseDigest,
    ])
      if (present(digest) && !isHex64(digest))
        throw new TypeError(
          "effect digest must be 64 lowercase hex characters",
        );
    if (
      input.effect.status === "planned" &&
      (present(input.effect.requestDigest) ||
        present(input.effect.responseDigest) ||
        present(input.effect.effectAttestation))
    )
      throw new TypeError(
        "planned effect must not carry request, response, or attestation",
      );
    if (
      input.effect.status === "dispatched" &&
      (present(input.effect.responseDigest) ||
        !present(input.effect.effectAttestation))
    )
      throw new TypeError(
        "dispatched effect requires attestation and no response digest",
      );
    if (
      input.effect.status === "confirmed" &&
      (!present(input.effect.responseDigest) ||
        !present(input.effect.effectAttestation))
    )
      throw new TypeError(
        "confirmed effect requires response digest and attestation",
      );
    if (
      (input.effect.status === "failed" ||
        input.effect.status === "reverted") &&
      !present(input.effect.effectAttestation)
    )
      throw new TypeError(
        `${input.effect.status} effect requires effect attestation`,
      );
  }
}

function compact(
  entries: readonly (readonly [string, unknown])[],
): Record<string, unknown> {
  return Object.fromEntries(
    entries.filter(([, value]) => value !== undefined && value !== ""),
  );
}

export async function build(input: Input): Promise<BuiltPayload> {
  return buildWithInternalCompute(input);
}

export async function received(
  input: Input,
  artifact: Uint8Array,
  artifactType: string,
): Promise<BuiltPayload> {
  if (artifact.length === 0)
    throw new TypeError("received artifact must not be empty");
  requireText("received artifact type", artifactType);
  const digest = createHash("sha256").update(artifact).digest("hex");
  return buildWithInternalCompute(input, {
    carried_artifact: { type: artifactType, digest_alg: "SHA-256", digest },
    carried_input_digest: digest,
  });
}
export async function carry(
  input: Input,
  artifact: Uint8Array,
): Promise<BuiltPayload> {
  return received(input, artifact, "foreign-artifact");
}

async function buildWithInternalCompute(
  input: Input,
  internal?: Record<string, unknown>,
): Promise<BuiltPayload> {
  if (
    internal !== undefined &&
    (input.compute?.agentInputDigest !== undefined ||
      input.compute?.agentOutputDigest !== undefined)
  )
    throw new TypeError(
      "carried or composed binding must not include agent input or output digest",
    );
  validate(input);
  const effectMode =
    input.effect === undefined || input.effect.status === "planned"
      ? "not_applicable"
      : input.effect.status === "confirmed"
        ? "confirmed"
        : "dispatched_unconfirmed";
  const value: Record<string, unknown> = {
    spec_version: SPEC_VERSION,
    format_version: FORMAT_VERSION,
    canonicalization_id: CANONICALIZATION_ID,
    action_id: input.actionId,
    action_type: input.actionType,
    operator: input.operator,
    developer: input.developer,
    timestamp: formatTimestamp(input.timestamp),
    assurance: {
      effect_mode: effectMode,
      attestation_mode: "self_attested",
      ledger_mode: input.chain === undefined ? "standalone" : "chained",
    },
  };
  if (present(input.epochId)) value.epoch_id = input.epochId;
  if (present(input.domain)) value.domain = input.domain;
  if (present(input.provenance)) value.provenance = input.provenance;
  if (input.disposition !== undefined)
    value.disposition = compact([
      ["decision", input.disposition.decision],
      ["approver", input.disposition.approver],
      ["human_disposed", input.disposition.humanDisposed],
      ["verdict_class", input.disposition.verdictClass],
      ["reason_digest", input.disposition.reasonDigest],
    ]);
  if (input.effect !== undefined)
    value.effect = compact([
      ["type", input.effect.type],
      ["status", input.effect.status],
      ["irreversibility_class", input.effect.irreversibilityClass],
      ["effect_attestation", input.effect.effectAttestation],
      ["request_digest", input.effect.requestDigest],
      ["response_digest", input.effect.responseDigest],
      ["external_ref", input.effect.externalRef],
    ]);
  if (input.chain !== undefined)
    value.chain = {
      parent_capsule_id: input.chain.parentCapsuleId,
      relation: input.chain.relation,
    };

  if (input.references !== undefined)
    value.references = input.references.map((reference) =>
      compact([
        ["type", reference.type],
        ["digest_alg", reference.digestAlg],
        ["digest", reference.digest],
        ["citation_purpose", reference.citationPurpose],
        [
          "log_coordinates",
          reference.logCoordinates === undefined
            ? undefined
            : // Canonical JSON round-trip detaches nested caller-owned claims.
              JSON.parse(
                new TextDecoder().decode(jcs(reference.logCoordinates)),
              ),
        ],
      ]),
    );
  const compute = {
    ...(input.compute === undefined
      ? {}
      : compact([
          ["agent_input_digest", input.compute.agentInputDigest],
          ["agent_output_digest", input.compute.agentOutputDigest],
          ["runtime", input.compute.runtime],
        ])),
    ...internal,
  };
  const attestation = compact([
    ["model_id", input.model?.modelId],
    ["provider", input.model?.provider],
    [
      "compute_attestation",
      Object.keys(compute).length === 0 ? undefined : compute,
    ],
  ]);
  if (Object.keys(attestation).length !== 0)
    value.model_attestation = attestation;
  const capsuleId = await computeCapsuleId(
    value as Parameters<typeof computeCapsuleId>[0],
  );
  value.capsule_id = capsuleId;
  const json = jcs(value);
  await verifyCapsule(json);
  return { capsuleId, value, json };
}

const slot =
  (name: Slot) =>
  (member: BuiltPayload | Result): SlotMember => ({ slot: name, member });
export const who = slot("who");
export const can = slot("can");
export const did = slot("did");
export const audit = slot("audit");

export async function buildComposition(
  input: Input,
  members: readonly SlotMember[],
): Promise<BuiltPayload> {
  if (members.length === 0)
    throw new TypeError("composition requires at least one slot member");
  const bySlot = new Map<Slot, string>();
  const ids = new Set<string>();
  for (const item of members) {
    if (!slots.includes(item.slot) || bySlot.has(item.slot))
      throw new TypeError(
        `composition duplicates or has invalid ${item.slot} slot`,
      );
    const capsuleId = item.member.capsuleId;
    const payload =
      "json" in item.member ? item.member.json : item.member.payload;
    if ((await verifyCapsule(payload)).capsuleId !== capsuleId)
      throw new TypeError(
        `composition ${item.slot} member is not a matching verified format-4 Capsule`,
      );
    if (ids.has(capsuleId))
      throw new TypeError(
        `composition ${item.slot} slot duplicates Capsule ID ${capsuleId}`,
      );
    ids.add(capsuleId);
    bySlot.set(item.slot, capsuleId);
  }
  const composed_members = slots
    .filter((name) => bySlot.has(name))
    .map((name) => ({
      type: "capsule",
      digest_alg: "SHA-256",
      digest: bySlot.get(name),
      slot: name,
    }));
  return buildWithInternalCompute(input, { composed_members });
}

export async function digestJSON(value: unknown): Promise<string> {
  return jsonDigest(value);
}

export async function seal(input: SealInput): Promise<Result> {
  if (input.capsule.model !== undefined || input.capsule.compute !== undefined)
    throw new TypeError(
      "SealInput model and compute metadata must use model and runtime fields",
    );
  const capsule: Input = {
    ...input.capsule,
    ...(input.model === undefined ? {} : { model: input.model }),
    ...(input.runtime === undefined
      ? {}
      : { compute: { runtime: input.runtime } }),
  };
  let built: BuiltPayload;
  if (input.members !== undefined) {
    if (input.payload !== undefined || input.agentOutput !== undefined)
      throw new TypeError(
        "composition seal must not include payload or agent output",
      );
    built = await buildComposition(capsule, input.members);
  } else {
    const compute = compact([
      [
        "agentInputDigest",
        input.payload === undefined
          ? undefined
          : await digestJSON(input.payload),
      ],
      [
        "agentOutputDigest",
        input.agentOutput === undefined
          ? undefined
          : await digestJSON(input.agentOutput),
      ],
      ["runtime", input.runtime],
    ]);
    built = await build({
      ...capsule,
      ...(Object.keys(compute).length === 0
        ? {}
        : { compute: compute as NonNullable<Input["compute"]> }),
    });
  }
  return {
    capsuleId: built.capsuleId,
    payload: Uint8Array.from(built.json),
    envelope: await sign(built, input.identity),
  };
}
