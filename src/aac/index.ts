import { referenceFindings } from "./references.js";
import { provisionalClass } from "./provisional.js";
import { registries as known } from "./registries.js";
import {
  asJsonObject,
  isHex64,
  decodeStrictJson,
  JcsFloatError,
  JcsUnsafeIntegerError,
  jcs,
  JsonNumber,
  sha256Hex,
  type ParsedJson,
} from "./json.js";

export {
  asJsonObject,
  decodeStrictJson,
  jcs,
  JcsFloatError,
  JcsUnsafeIntegerError,
  JsonNumber,
  jsonDigest,
  sha256Hex,
} from "./json.js";
export type { JsonValue, ParsedJson } from "./json.js";

export interface Finding {
  readonly code: string;
  readonly detail: string;
  readonly severity: "error" | "warning" | "info";
  readonly check?: number;
}
export interface VerificationResult {
  readonly ok: boolean;
  readonly findings: readonly Finding[];
  readonly assurance: Readonly<Record<string, string>>;
  readonly capsuleId?: string;
}

type RecordValue = Record<string, ParsedJson>;

const object = asJsonObject;

/** Decode a Capsule as a strict JSON object. */
export function decodeCapsuleJson(data: Uint8Array | string): RecordValue {
  const value = decodeStrictJson(data);
  const result = object(value);
  if (result === undefined) throw new TypeError("Capsule is not a JSON object");
  return result;
}

/** Recompute a format-4 signer-independent Capsule ID. */
export function computeCapsuleId(capsule: RecordValue): string {
  if (capsule.format_version !== "4")
    throw new TypeError('format_version must be "4"');
  if (!("canonicalization_id" in capsule))
    throw new TypeError("canonicalization_id is required");
  const copy: RecordValue = {};
  const declared = capsule.canonicalization_id;
  if (declared !== undefined && typeof declared !== "string")
    throw new TypeError("canonicalization_id must be a string");
  if (declared !== undefined && declared !== "jcs")
    throw new TypeError(
      `unsupported canonicalization_id ${JSON.stringify(declared)}`,
    );
  for (const [key, value] of Object.entries(capsule)) {
    if (key === "capsule_id" || key === "signature" || key === "key_id")
      continue;
    copy[key] = value;
  }
  return sha256Hex(jcs(copy));
}

function pathFind(
  value: ParsedJson,
  predicate: (value: JsonNumber) => boolean,
  path = "",
): string[] {
  if (value instanceof JsonNumber)
    return predicate(value) ? [path || "<root>"] : [];
  if (Array.isArray(value))
    return value.flatMap((child, index) =>
      pathFind(child, predicate, `${path}[${index}]`),
    );
  const record = object(value);
  if (record === undefined) return [];
  return Object.keys(record)
    .sort()
    .flatMap((key) =>
      pathFind(record[key]!, predicate, path === "" ? key : `${path}.${key}`),
    );
}

const v4IrreversibilityClasses = known.irreversibility_class;

/** Report membership in the AAC draft-04 irreversibility seed registry. */
export function isV4IrreversibilityClass(value: string): boolean {
  return v4IrreversibilityClasses.has(value);
}

/**
 * String-typed block members, in check-1 emission order. The top-level
 * epoch_id is checked the same way, just before these. disposition.authority
 * is not here: §5.4 types it only as "an opaque reference", not as a string.
 */
const STRING_MEMBERS = [
  ["disposition", "decision"],
  ["disposition", "verdict_class"],
  ["effect", "status"],
  ["effect", "type"],
  ["effect", "irreversibility_class"],
  ["effect", "effect_attestation"],
  ["effect", "external_ref"],
  ["chain", "relation"],
  ["cross_party", "correlator"],
  ["assurance", "effect_mode"],
  ["assurance", "attestation_mode"],
  ["assurance", "ledger_mode"],
  ["assurance", "cross_party_rung"],
  ["provenance_mode", "source_asserted_at"],
  ["provenance_mode", "import_batch"],
  ["provenance_mode", "imported_at"],
] as const;

/** AAC Class 1 verification. It always returns a structured result. */
export function verifyClass1(
  capsule: ParsedJson,
  store?: readonly (ParsedJson | string)[] | Set<string>,
  extensions: Readonly<Record<string, ReadonlySet<string>>> = {},
): VerificationResult {
  const findings: Finding[] = [];
  const add = (
    code: string,
    detail: string,
    check?: number,
    severity: Finding["severity"] = "error",
  ): void => {
    findings.push(
      check === undefined
        ? { code, detail, severity }
        : { code, detail, severity, check },
    );
  };
  const top = object(capsule);
  if (top === undefined)
    return {
      ok: false,
      findings: [
        {
          code: "not_an_object",
          detail: "Capsule is not a JSON object",
          severity: "error",
          check: 1,
        },
      ],
      assurance: {},
    };
  // Keep external-reference diagnostics within their existing check groups.
  const references = referenceFindings(
    top,
    new Set([
      ...known.citation_purpose,
      ...(extensions.citation_purpose ?? []),
    ]),
  );
  for (const field of [
    "spec_version",
    "format_version",
    "capsule_id",
    "action_id",
    "action_type",
    "operator",
    "developer",
    "timestamp",
  ]) {
    if (!(field in top))
      add("missing_required_field", `${field} is REQUIRED (§5.1)`, 1);
    else if (typeof top[field] !== "string")
      add("field_not_string", `${field} MUST be a string (§5.1)`, 1);
  }
  const carriedId =
    typeof top.capsule_id === "string" && isHex64(top.capsule_id)
      ? top.capsule_id
      : undefined;
  if (typeof top.capsule_id === "string" && carriedId === undefined)
    add(
      "capsule_id_malformed",
      "capsule_id MUST be 64 lowercase hex (§5.1)",
      1,
    );
  if (
    typeof top.action_type === "string" &&
    top.action_type !== "fyi" &&
    top.action_type !== "decide"
  )
    add(
      "action_type_invalid",
      "action_type MUST be 'fyi' or 'decide' (§5.1)",
      1,
    );
  if (typeof top.format_version === "string") {
    if (top.format_version === "4" && top.canonicalization_id !== "jcs")
      add(
        !("canonicalization_id" in top)
          ? "canonicalization_id_missing"
          : typeof top.canonicalization_id === "string"
            ? "canonicalization_profile_mismatch"
            : "canonicalization_id_not_string",
        'format_version "4" REQUIRES canonicalization_id="jcs" (§5.1)',
        1,
      );
    else if (top.format_version !== "4")
      add(
        "unsupported_format_version",
        `format_version ${JSON.stringify(top.format_version)} is not supported; expected "4" (§5.1)`,
        1,
      );
  }
  for (const field of [
    "effect",
    "assurance",
    "disposition",
    "chain",
    "cross_party",
  ])
    if (field in top && object(top[field]) === undefined)
      add("block_not_object", `${field} MUST be a JSON object when present`, 1);
  if ("constraints" in top && !Array.isArray(top.constraints))
    add(
      "constraints_not_array",
      "constraints MUST be an array when present (§8.1)",
      1,
    );
  for (const path of pathFind(top, (n) => /[.eE]/u.test(n.raw)))
    add(
      "float_in_digest_field",
      `floating-point value at ${path}; §5.1 forbids it`,
      1,
    );
  for (const path of pathFind(
    top,
    (n) =>
      !/[.eE]/u.test(n.raw) &&
      (BigInt(n.raw) > 9007199254740991n || BigInt(n.raw) < -9007199254740991n),
  ))
    add(
      "unsafe_integer_in_digest_field",
      `integer outside the JS-safe range (+/-9007199254740991) at ${path}`,
      1,
    );
  const disposition = object(top.disposition);
  if (disposition !== undefined) {
    if (disposition.approver === undefined || disposition.approver === null)
      add(
        "missing_required_field",
        "disposition.approver is REQUIRED (§5.4)",
        1,
      );
    else if (typeof disposition.approver !== "string")
      add(
        "field_not_string",
        "disposition.approver MUST be a string (§5.4)",
        1,
      );
    else if (
      !["human", "policy", "counterparty"].includes(disposition.approver)
    )
      add(
        "approver_invalid",
        "disposition.approver has an invalid value (§5.4)",
        1,
      );
    if (!("decision" in disposition))
      add(
        "missing_required_field",
        "disposition.decision is REQUIRED (§5.4)",
        1,
      );
    if (typeof disposition.human_disposed !== "boolean")
      add(
        "field_not_bool",
        "disposition.human_disposed MUST be boolean (§5.4)",
        1,
      );
    if (disposition.human_disposed === true && disposition.approver !== "human")
      add(
        "dishonest_human_disposed",
        "human_disposed=true with non-human approver (§5.4)",
        undefined,
        "warning",
      );
  }
  // A string-typed member holding any other JSON type (a number, a boolean,
  // null, a list, an object) fails check 1 ("REQUIRED fields present and
  // typed", §6). The never-reject rule for unregistered values (§4, §12)
  // covers well-typed strings only: a list where a string belongs is a type
  // error, not an unknown value.
  if (Object.hasOwn(top, "epoch_id") && typeof top.epoch_id !== "string")
    add("field_not_string", "epoch_id MUST be a string when present (§5.1)", 1);
  for (const [block, member] of STRING_MEMBERS) {
    const members = object(top[block]);
    if (
      members !== undefined &&
      Object.hasOwn(members, member) &&
      typeof members[member] !== "string"
    )
      add(
        "field_not_string",
        `${block}.${member} MUST be a string when present (§6 check 1)`,
        1,
      );
  }
  findings.push(...references.filter((finding) => finding.check === 1));

  let recomputed: string | undefined;
  const identityProfileValid =
    top.format_version === "4" && top.canonicalization_id === "jcs";
  // Profile validation precedes digest computation. An absent capsuleId in the
  // result already records that no computation was attempted, so do not add a
  // derived capsule_id_uncomputable finding for the same primary cause.
  if (carriedId !== undefined && identityProfileValid) {
    try {
      recomputed = computeCapsuleId(top);
      if (recomputed !== carriedId)
        add(
          "capsule_id_mismatch",
          `recomputed ${recomputed} != carried ${carriedId}`,
          2,
        );
    } catch (error) {
      if (
        !(error instanceof JcsFloatError) &&
        !(error instanceof JcsUnsafeIntegerError)
      )
        add("capsule_id_uncomputable", String(error), 2);
    }
  }
  const effect = object(top.effect);
  const status = typeof effect?.status === "string" ? effect.status : "";
  if (
    status === "confirmed" &&
    !(
      typeof effect?.response_digest === "string" &&
      isHex64(effect.response_digest)
    )
  )
    add(
      "confirmed_without_response",
      "effect.status 'confirmed' requires 64-hex response_digest (§5.2)",
      3,
    );
  const effectMode =
    effect === undefined || status === "planned"
      ? "not_applicable"
      : status === "confirmed" &&
          typeof effect.response_digest === "string" &&
          isHex64(effect.response_digest)
        ? "confirmed"
        : "dispatched_unconfirmed";
  const verdict =
    typeof disposition?.verdict_class === "string"
      ? disposition.verdict_class
      : "";
  if (
    new Set([
      "blocked",
      "hitl_dispatched",
      "denied",
      "engine_failure",
      "deferred",
      "needs_decision",
      "expired",
      "escalated",
      "resolved",
    ]).has(verdict) &&
    effectMode !== "not_applicable"
  )
    add(
      "verdict_effect_conflict",
      `verdict_class ${JSON.stringify(verdict)} requires effect_mode "not_applicable" (§5.4.2)`,
      4,
    );
  if (
    effectMode === "not_applicable" &&
    effect?.effect_attestation !== undefined &&
    effect.effect_attestation !== null
  )
    add(
      "effect_attestation_present",
      "effect_attestation MUST be absent for effect_mode 'not_applicable' (§5.2)",
      5,
    );
  if (
    effect !== undefined &&
    status !== "planned" &&
    (effect.effect_attestation === undefined ||
      effect.effect_attestation === null)
  )
    add(
      "effect_attestation_missing",
      "dispatched effect requires effect_attestation (§5.2)",
      5,
    );
  const chain = object(top.chain);
  if (chain !== undefined) {
    if (
      !(
        typeof chain.parent_capsule_id === "string" &&
        isHex64(chain.parent_capsule_id)
      )
    )
      add(
        "chain_parent_malformed",
        "chain.parent_capsule_id MUST be 64-hex capsule_id (§5.4.4)",
        6,
      );
    if (!("relation" in chain))
      add(
        "missing_required_field",
        "chain.relation is REQUIRED when chain block is present (§5.4.4)",
        6,
      );
    if (store === undefined)
      add(
        "chain_check_store_level",
        "chain parent-existence and concurrent-supersedes are store-level checks (§6); not run without store",
        6,
        "info",
      );
    else {
      const ids =
        store instanceof Set
          ? store
          : new Set(
              store
                .map((item) =>
                  typeof item === "string" ? item : object(item)?.capsule_id,
                )
                .filter((id): id is string => typeof id === "string"),
            );
      if (
        typeof chain.parent_capsule_id === "string" &&
        !ids.has(chain.parent_capsule_id)
      )
        add(
          "chain_parent_missing",
          `chain parent ${chain.parent_capsule_id} not found in store (§6)`,
          6,
        );
    }
  }
  findings.push(...references.filter((finding) => finding.check === 6));

  const crossParty = object(top.cross_party);
  let crossPartyRung: string | undefined;
  if (crossParty !== undefined)
    crossPartyRung =
      typeof crossParty.counterparty_ref === "string" &&
      isHex64(crossParty.counterparty_ref) &&
      typeof crossParty.correlator === "string" &&
      crossParty.correlator !== ""
        ? crossParty.substantive === true
          ? "full_bilateral"
          : "acknowledged_receipt"
        : "unilateral_fallback";
  const assurance: Record<string, string> = {
    effect_mode: effectMode,
    attestation_mode: "self_attested",
    ledger_mode: chain === undefined ? "standalone" : "chained",
    ...(crossPartyRung === undefined
      ? {}
      : { cross_party_rung: crossPartyRung }),
  };
  const stated = object(top.assurance);
  const rank = {
    not_applicable: 0,
    dispatched_unconfirmed: 0,
    confirmed: 1,
  } as Record<string, number>;
  if (
    typeof stated?.effect_mode === "string" &&
    (rank[stated.effect_mode] ?? -1) > rank[effectMode]!
  )
    add(
      "assurance_overclaim",
      `claimed effect_mode ${JSON.stringify(stated.effect_mode)} but verifier derived ${JSON.stringify(effectMode)} (§5.3)`,
      7,
    );
  const attestationRank: Record<string, number> = {
    self_attested: 0,
    anchored: 1,
  };
  if (
    typeof stated?.attestation_mode === "string" &&
    (attestationRank[stated.attestation_mode] ?? -1) >
      attestationRank.self_attested!
  )
    add(
      "assurance_overclaim",
      `claimed attestation_mode ${JSON.stringify(stated.attestation_mode)} but verifier derived "self_attested" (§5.3)`,
      7,
      "info",
    );
  const ledgerRank: Record<string, number> = {
    standalone: 0,
    chained: 1,
    anchored: 2,
  };
  const ledgerMode = chain === undefined ? "standalone" : "chained";
  if (
    typeof stated?.ledger_mode === "string" &&
    (ledgerRank[stated.ledger_mode] ?? -1) > ledgerRank[ledgerMode]!
  )
    add(
      "assurance_overclaim",
      `claimed ledger_mode ${JSON.stringify(stated.ledger_mode)} but verifier derived ${JSON.stringify(ledgerMode)} (§5.3)`,
      7,
      "info",
    );
  const crossRank: Record<string, number> = {
    unilateral_fallback: 0,
    acknowledged_receipt: 1,
    full_bilateral: 2,
  };
  if (
    typeof stated?.cross_party_rung === "string" &&
    (crossRank[stated.cross_party_rung] ?? -1) >
      crossRank[crossPartyRung ?? "unilateral_fallback"]!
  )
    add(
      "assurance_overclaim",
      `claimed cross_party_rung ${JSON.stringify(stated.cross_party_rung)} but verifier derived ${JSON.stringify(crossPartyRung ?? "unilateral_fallback")} (§5.3 Cross-party assurance)`,
      7,
      "info",
    );
  const fields = [
    ["verdict_class", disposition, "verdict_class"],
    ["disposition.decision", disposition, "decision"],
    ["effect.type", effect, "type"],
    ["irreversibility_class", effect, "irreversibility_class"],
    ["effect_attestation", effect, "effect_attestation"],
    ["chain.relation", chain, "relation"],
  ] as const;
  for (const [registry, block, member] of fields) {
    const value = block?.[member];
    const accepted = new Set([
      ...(known[registry] ?? []),
      ...(extensions[registry] ?? []),
    ]);
    if (typeof value === "string" && !accepted.has(value)) {
      const provisional = provisionalClass(registry, value);
      if (provisional !== undefined) {
        add(
          "known_provisional_registry_value",
          `${member}=${JSON.stringify(value)} resolves known status 'provisional' via vendored CPB registry (payload class ${JSON.stringify(provisional)}); informational, not rejected (§12)`,
          8,
          "info",
        );
        continue;
      }
      add(
        "unknown_registry_value",
        `${member}=${JSON.stringify(value)} is not a seeded ${registry} value; informational, not rejected (§12)`,
        8,
        "info",
      );
      if (registry === "effect_attestation")
        add(
          "effect_attestation_graded_floor",
          "unknown effect_attestation is graded no stronger than 'runtime_claimed' (§5.2)",
          8,
          "info",
        );
    }
  }
  findings.push(...references.filter((finding) => finding.check === 8));

  return {
    ok: !findings.some((finding) => finding.severity === "error"),
    findings,
    assurance,
    ...(recomputed === undefined ? {} : { capsuleId: recomputed }),
  };
}

export function verifyStore(
  capsules: readonly ParsedJson[],
  extensions: Readonly<Record<string, ReadonlySet<string>>> = {},
): VerificationResult[] {
  const ids = new Set(
    capsules
      .map((capsule) => object(capsule)?.capsule_id)
      .filter((id): id is string => typeof id === "string"),
  );
  const results = capsules.map((capsule) =>
    verifyClass1(capsule, ids, extensions),
  );
  const seen = new Set<string>();
  capsules.forEach((capsule, index) => {
    const chain = object(object(capsule)?.chain);
    if (
      chain?.relation !== "supersedes" ||
      typeof chain.parent_capsule_id !== "string"
    )
      return;
    if (seen.has(chain.parent_capsule_id)) {
      const finding: Finding = {
        code: "concurrent_supersedes",
        detail: `later supersedes for parent ${chain.parent_capsule_id} is non-authoritative`,
        severity: "info",
        check: 6,
      };
      results[index] = {
        ...results[index]!,
        findings: [...results[index]!.findings, finding],
      };
    }
    seen.add(chain.parent_capsule_id);
  });
  return results;
}
