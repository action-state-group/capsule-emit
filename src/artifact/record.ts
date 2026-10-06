// Neutral artifact record model: it persists exact sealed Capsules, Producer
// Envelopes, and associated business originals. It neither seals nor appends to
// a CLL. This module imports no storage driver; the sqlite and mysql backends
// build on it. It is the byte-compatible TypeScript port of capsule-emit-go's
// artifact package.
import { createHash } from "node:crypto";

import { asJsonObject, decodeStrictJson, jsonDigest } from "../aac/index.js";
import type { ParsedJson } from "../aac/index.js";
import { verifyEnvelope } from "../envelope.js";
import { decodePayload, verifyCapsule } from "../verify.js";

/**
 * A supported AAC format-4 JSON-DIGEST location, not a free-form JSON pointer.
 * The payload commitment's current wire field is `agent_input_digest`.
 */
export type DigestField =
  | "model_attestation.compute_attestation.agent_input_digest"
  | "model_attestation.compute_attestation.agent_output_digest"
  | "effect.request_digest"
  | "effect.response_digest";

export const PAYLOAD_DIGEST: DigestField =
  "model_attestation.compute_attestation.agent_input_digest";
export const AGENT_OUTPUT_DIGEST: DigestField =
  "model_attestation.compute_attestation.agent_output_digest";
export const EFFECT_REQUEST_DIGEST: DigestField = "effect.request_digest";
export const EFFECT_RESPONSE_DIGEST: DigestField = "effect.response_digest";

const DIGEST_FIELDS: ReadonlySet<string> = new Set<DigestField>([
  PAYLOAD_DIGEST,
  AGENT_OUTPUT_DIGEST,
  EFFECT_REQUEST_DIGEST,
  EFFECT_RESPONSE_DIGEST,
]);

/**
 * Availability, not authenticity. Purged originals cannot be resurrected by
 * retrying a write. `never_retained` must be explicit.
 */
export type RetentionState = "present" | "purged" | "never_retained";

export const PRESENT: RetentionState = "present";
export const PURGED: RetentionState = "purged";
export const NEVER_RETAINED: RetentionState = "never_retained";

/** A stable code for each failure the Go package distinguishes with a sentinel. */
export type ArtifactErrorCode =
  | "invalid"
  | "conflict"
  | "not_found"
  | "purged"
  | "digest_mismatch"
  | "untrusted_signer"
  | "corrupt";

export class ArtifactError extends Error {
  public constructor(
    public readonly code: ArtifactErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "ArtifactError";
  }
}

/**
 * An exact original, normally named `payload` or `agent_output`. Additional
 * names support effect preimages and application attachments. An absent (empty)
 * `binding` means NOT authenticated by a Capsule digest. `contentSha256`
 * protects exact-byte storage integrity; it is not a signature claim and is
 * different from the JCS digest referenced by `binding`.
 */
export interface Artifact {
  name: string;
  binding?: DigestField | "" | undefined;
  content?: Uint8Array | undefined;
  state: RetentionState;
  contentSha256?: string | undefined;
}

/**
 * Groups a Capsule with one Producer Envelope and its originals. Content is
 * copied, never resealed or reserialized. Records are immutable except for the
 * explicit purge of originals. An omitted artifact is undeclared, not evidence
 * that it was never retained.
 */
export interface Record {
  capsuleId: string;
  capsule: Uint8Array;
  producerEnvelope: Uint8Array;
  artifacts: Artifact[];
}

/** Separates a verified preimage from an unbound attachment. */
export interface ArtifactVerification {
  state: RetentionState;
  bound: boolean;
  verified: boolean;
}

const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u;
const ID_PATTERN = /^[0-9a-f]{64}$/u;

const MAX_ARTIFACTS = 64;
const MAX_ENVELOPE = 65_535;
const MAX_TOTAL = 8 * 1024 * 1024;

function rawDigest(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

function equalBytes(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
  return true;
}

function digestAt(capsule: ParsedJson, field: DigestField): string {
  if (!DIGEST_FIELDS.has(field))
    throw new ArtifactError("invalid", `unsupported digest field ${field}`);
  let value: ParsedJson | undefined = capsule;
  for (const key of field.split(".")) {
    const object = asJsonObject(value);
    if (object === undefined)
      throw new ArtifactError("invalid", `missing ${field}`);
    value = object[key];
    if (value === undefined)
      throw new ArtifactError("invalid", `missing ${field}`);
  }
  if (typeof value !== "string" || !ID_PATTERN.test(value))
    throw new ArtifactError("invalid", `malformed ${field}`);
  return value;
}

/**
 * Authenticates the Capsule, authorizes its signer against caller-owned trusted
 * keys, and verifies every available bound original using emit's JCS. It does
 * not verify CLL inclusion, external references, or business correctness.
 */
export async function verify(
  record: Record,
  trusted: readonly Uint8Array[],
): Promise<Map<string, ArtifactVerification>> {
  if (!ID_PATTERN.test(record.capsuleId))
    throw new ArtifactError("invalid", "capsule id");
  let capsuleId: string | undefined;
  try {
    capsuleId = (await verifyCapsule(record.capsule)).capsuleId;
  } catch (error) {
    throw new ArtifactError(
      "invalid",
      `capsule verification: ${String(error)}`,
    );
  }
  if (capsuleId !== record.capsuleId)
    throw new ArtifactError("invalid", "capsule verification");
  const envelope = await verifyEnvelope(
    record.capsuleId,
    record.producerEnvelope,
  );
  if (!envelope.ok || envelope.publicKey === undefined)
    throw new ArtifactError(
      "invalid",
      `envelope: ${envelope.findings.map((f) => f.code).join(",")}`,
    );
  const signer = envelope.publicKey;
  let authorized = false;
  for (const key of trusted)
    if (key.length === 32 && equalBytes(key, signer)) authorized = true;
  if (!authorized)
    throw new ArtifactError(
      "untrusted_signer",
      "producer signer is not trusted",
    );

  const capsule = decodePayload(record.capsule);
  const results = new Map<string, ArtifactVerification>();
  for (const a of record.artifacts) {
    if (!NAME_PATTERN.test(a.name))
      throw new ArtifactError("invalid", "artifact name");
    if (results.has(a.name))
      throw new ArtifactError("invalid", `duplicate artifact ${a.name}`);
    let committed = "";
    if (a.binding) committed = digestAt(capsule, a.binding);
    const check: ArtifactVerification = {
      state: a.state,
      bound: Boolean(a.binding),
      verified: false,
    };
    switch (a.state) {
      case "present": {
        if (a.content === undefined)
          throw new ArtifactError("invalid", `missing content for ${a.name}`);
        if (a.contentSha256 && a.contentSha256 !== rawDigest(a.content))
          throw new ArtifactError("corrupt", a.name);
        if (a.binding) {
          let digest: string;
          try {
            digest = await jsonDigest(decodeStrictJson(a.content));
          } catch {
            throw new ArtifactError(
              "digest_mismatch",
              `${a.name} (${a.binding})`,
            );
          }
          if (digest !== committed)
            throw new ArtifactError(
              "digest_mismatch",
              `${a.name} (${a.binding})`,
            );
          check.verified = true;
        }
        break;
      }
      case "purged": {
        if (a.content !== undefined || !ID_PATTERN.test(a.contentSha256 ?? ""))
          throw new ArtifactError("invalid", "invalid purge tombstone");
        break;
      }
      case "never_retained": {
        if (a.content !== undefined || (a.contentSha256 ?? "") !== "")
          throw new ArtifactError("invalid", "never-retained content");
        break;
      }
      default:
        throw new ArtifactError("invalid", "retention state");
    }
    results.set(a.name, check);
  }
  return results;
}

/**
 * Commits the exact envelope/Capsule and complete artifact inventory, including
 * original byte hashes. It survives intentional purge while detecting missing
 * rows, changed bindings, or accidental corruption. It is byte-identical to the
 * Go `StorageChecksum` so a shared database is portable across languages: it is
 * `sha256` over Go `encoding/json` of the normalized record (declaration-order
 * fields, standard-base64 byte fields, `omitempty` semantics). It is NOT an
 * additional producer authentication claim.
 */
export function storageChecksum(record: Record): string {
  const artifacts = record.artifacts.map((a) => ({ ...a }));
  for (const a of artifacts) {
    a.content = undefined;
    if (a.state === "purged") a.state = "present";
  }
  artifacts.sort((x, y) => (x.name < y.name ? -1 : x.name > y.name ? 1 : 0));
  // Field insertion order below mirrors the Go struct declaration order, and
  // JSON.stringify emits no whitespace, so the bytes match encoding/json for
  // this store's constrained hex/base64/enum values.
  const encodedArtifacts = artifacts.map((a) => {
    // Assign in Go struct declaration order (name, binding, state,
    // content_sha256) so JSON.stringify's insertion order matches.
    const object: {
      name: string;
      binding?: string;
      state?: string;
      content_sha256?: string;
    } = { name: a.name };
    if (a.binding) object.binding = a.binding;
    object.state = a.state;
    if (a.contentSha256) object.content_sha256 = a.contentSha256;
    return object;
  });
  const encoded = {
    capsule_id: record.capsuleId,
    capsule: Buffer.from(record.capsule).toString("base64"),
    producer_envelope: Buffer.from(record.producerEnvelope).toString("base64"),
    artifacts: encodedArtifacts,
  };
  // Go marshals this shape with encoding/json/v2, which encodes an empty
  // inventory as [] (not v1's null); JSON.stringify agrees, so the checksum
  // matches the Go store byte-for-byte over a shared database.
  return createHash("sha256").update(JSON.stringify(encoded)).digest("hex");
}

/**
 * Copies a caller record's mutable buffers, fills exact-byte checksums, and
 * enforces v1 admission limits. It does not authenticate a producer; backends
 * must also call verify with caller-configured trusted keys before persistence.
 */
export function prepare(record: Record): Record {
  const capsule = Uint8Array.from(record.capsule);
  const producerEnvelope = Uint8Array.from(record.producerEnvelope);
  const artifacts = record.artifacts.map((a) => ({ ...a }));
  let size = capsule.length + producerEnvelope.length;
  if (
    artifacts.length > MAX_ARTIFACTS ||
    producerEnvelope.length > MAX_ENVELOPE
  )
    throw new ArtifactError("invalid", "record limits");
  for (const a of artifacts) {
    if (a.state === "purged")
      throw new ArtifactError("purged", "originals were purged");
    const content = a.content ?? new Uint8Array();
    size += content.length;
    if (a.content !== undefined) a.content = Uint8Array.from(a.content);
    if (a.state === "present") {
      const digest = rawDigest(content);
      if (a.contentSha256 && a.contentSha256 !== digest)
        throw new ArtifactError("corrupt", "content checksum");
      a.contentSha256 = digest;
    }
  }
  if (size > MAX_TOTAL)
    throw new ArtifactError("invalid", "record exceeds 8 MiB");
  return { capsuleId: record.capsuleId, capsule, producerEnvelope, artifacts };
}

/** The backend-neutral artifact lifecycle. Backends expose transactions separately. */
export interface Store {
  put(record: Record): Promise<void>;
  get(id: string): Promise<Record>;
  purge(id: string): Promise<void>;
}
