import { asJsonObject as object, isHex64, type ParsedJson } from "./json.js";
import type { Finding } from "./index.js";

/** Draft-04 §5.5.5 structural checks. No target or inclusion proof is resolved. */
/** String-typed members of a references[] entry's retention object (§5.5.5). */
const RETENTION_STRING_MEMBERS = [
  "declarant",
  "retained_until",
  "not_retained_after",
] as const;

/**
 * Check-1 findings for a references[] entry's retention (§5.5.5), in order:
 * the object itself, its REQUIRED declarant (absent or null is missing, as for
 * disposition.approver), the type of each member, and at least one bound.
 */
function retentionFindings(
  raw: ParsedJson,
  path: string,
  add: (code: string, detail: string, check: number) => void,
): void {
  const retention = object(raw);
  if (retention === undefined) {
    add(
      "field_not_object",
      `${path} MUST be a JSON object when present (§5.5.5)`,
      1,
    );
    return;
  }
  for (const field of RETENTION_STRING_MEMBERS) {
    const present = Object.hasOwn(retention, field);
    if (field === "declarant" && (!present || retention[field] === null))
      add(
        "missing_required_field",
        `${path}.declarant is REQUIRED (§5.5.5)`,
        1,
      );
    else if (present && typeof retention[field] !== "string")
      add(
        "field_not_string",
        `${path}.${field} MUST be a string when present (§5.5.5)`,
        1,
      );
  }
  if (
    !Object.hasOwn(retention, "retained_until") &&
    !Object.hasOwn(retention, "not_retained_after")
  )
    add(
      "retention_empty",
      `${path} MUST carry retained_until or not_retained_after (§5.5.5)`,
      1,
    );
}

export function referenceFindings(
  capsule: Record<string, ParsedJson>,
  purposes: ReadonlySet<string>,
): Finding[] {
  if (capsule.format_version !== "4") return []; // Preserve vintage extension handling.
  if (!("references" in capsule)) return [];
  const findings: Finding[] = [];
  const add = (
    code: string,
    detail: string,
    check: number,
    severity: Finding["severity"] = "error",
  ) => {
    findings.push({ code, detail, check, severity });
  };
  if (!Array.isArray(capsule.references)) {
    add("references_malformed", "references MUST be an array (§5.5.5)", 1);
    return findings;
  }
  const parent = object(capsule.chain)?.parent_capsule_id;
  for (const [i, raw] of capsule.references.entries()) {
    const path = `references[${i}]`;
    const ref = object(raw);
    if (ref === undefined) {
      add("reference_malformed", `${path} MUST be an object (§5.5.5)`, 1);
      continue;
    }
    for (const field of ["type", "digest_alg", "digest"]) {
      if (typeof ref[field] !== "string" || ref[field] === "") {
        add(
          "reference_malformed",
          `${path}.${field} MUST be a non-empty string (§5.5.5)`,
          1,
        );
      }
    }
    // CPB digest contexts include artifact type and algorithm. Hex equality
    // alone does not identify the same target across different contexts.

    if (ref.type === "agent-action-capsule" && ref.digest_alg === "SHA-256") {
      if (
        typeof ref.digest === "string" &&
        ref.digest !== "" &&
        !isHex64(ref.digest)
      ) {
        add(
          "reference_malformed",
          `${path}.digest MUST be an AAC Capsule ID for agent-action-capsule/SHA-256 (§5.5.5)`,
          1,
        );
      }
      if (
        typeof parent === "string" &&
        parent !== "" &&
        ref.digest === parent
      ) {
        add(
          "reference_duplicates_chain_parent",
          `${path} duplicates chain.parent_capsule_id (§5.5.5)`,
          6,
        );
      }
    }
    if ("citation_purpose" in ref) {
      if (
        typeof ref.citation_purpose !== "string" ||
        ref.citation_purpose === ""
      ) {
        add(
          "reference_malformed",
          `${path}.citation_purpose MUST be a non-empty string (§5.5.5)`,
          1,
        );
      } else if (!purposes.has(ref.citation_purpose)) {
        add(
          "unknown_registry_value",
          `${path}.citation_purpose is not seeded; informational, not rejected (§12)`,
          8,
          "info",
        );
      }
    }
    if (ref.retention !== undefined)
      retentionFindings(ref.retention, `${path}.retention`, add);
    if ("log_coordinates" in ref) {
      const coordinates = object(ref.log_coordinates);
      if (coordinates === undefined) {
        add(
          "reference_log_coordinates_malformed",
          `${path}.log_coordinates MUST be an object (§5.5.5)`,
          1,
        );
        continue;
      }
      for (const field of ["log_id", "leaf_index", "inclusion_proof"]) {
        if (!(field in coordinates) || coordinates[field] === null) {
          add(
            "reference_log_coordinates_malformed",
            `${path}.log_coordinates requires ${field} (§5.5.5)`,
            1,
          );
        }
      }
    }
  }
  return findings;
}
