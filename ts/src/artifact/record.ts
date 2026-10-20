// Neutral artifact record model: it persists exact sealed Capsules, Producer
// Envelopes, and associated business originals. It neither seals nor appends to
// a CLL. This module imports no storage driver; the sqlite and mysql backends
// build on it. It is the byte-compatible TypeScript port of the Go module's
// artifact package.
import { createHash } from "node:crypto";

import {
  asJsonObject,
  decodeStrictJson,
  isHex64,
  jsonDigest,
} from "../aac/index.js";
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
 * The `record_type` of a padding record (Evidence Layer -00, "What a Checkpoint
 * Reveals"): a log leaf that is not a Capsule. A store admits it beside
 * Capsules, so `get` can return one; a caller that needs a Capsule checks
 * `record_type`.
 */
export const PADDING_RECORD_TYPE = "padding";

/** A stored padding record's closed member set, as AAC's padding vectors pin it. */
const PADDING_MEMBERS = new Set([
  "capsule_id",
  "record_type",
  "epistemic_type",
  "store_nonce",
]);

/**
 * Authenticates the stored Capsule bytes: an AAC format-4 Capsule through
 * Class 1, or a padding record through its closed shape. Either way the
 * computed ID must equal the record's.
 */
async function verifyCapsuleBytes(record: Record): Promise<void> {
  let decoded: { [member: string]: ParsedJson } | undefined;
  try {
    decoded = asJsonObject(decodeStrictJson(record.capsule));
  } catch {
    // Not strict JSON: verifyCapsule below reports it.
  }
  if (decoded?.record_type === PADDING_RECORD_TYPE)
    return verifyPadding(record.capsuleId, decoded, ownMembers(record.capsule));
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
}

/**
 * The top-level member names of strict-JSON `capsule` bytes, `__proto__`
 * included. AAC's strict decoder assigns members onto a plain object, where a
 * `__proto__` member becomes the prototype or, with a primitive value, is
 * dropped, so `Object.keys` of its result can miss it; `JSON.parse` defines
 * every member as an own property. The bytes already passed strict decoding,
 * so the two read the same members otherwise.
 */
function ownMembers(capsule: Uint8Array): string[] {
  return Object.keys(JSON.parse(new TextDecoder().decode(capsule)) as object);
}

/**
 * Checks a padding record: exactly its four members (`members`, read by
 * {@link ownMembers}), `epistemic_type`
 * `producer_claim`, a 64-lowercase-hex `store_nonce`, and a `capsule_id` that
 * is the JSON-DIGEST of the record without `capsule_id`.
 */
async function verifyPadding(
  id: string,
  padding: { [member: string]: ParsedJson },
  members: readonly string[],
): Promise<void> {
  for (const member of members)
    if (!PADDING_MEMBERS.has(member))
      throw new ArtifactError(
        "invalid",
        `padding record carries ${JSON.stringify(member)}`,
      );
  if (padding.epistemic_type !== "producer_claim")
    throw new ArtifactError("invalid", "padding record epistemic_type");
  if (!isHex64(padding.store_nonce))
    throw new ArtifactError("invalid", "padding record store_nonce");
  const body = Object.fromEntries(
    Object.entries(padding).filter(([member]) => member !== "capsule_id"),
  );
  if (padding.capsule_id !== id || (await jsonDigest(body)) !== id)
    throw new ArtifactError("invalid", "padding record capsule id");
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
  await verifyCapsuleBytes(record);
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

/**
 * The backend-neutral artifact store contract, specified in
 * docs/artifact-store.md and checked by the shared conformance suite
 * (`./artifact/conformance`). Constructors stay per backend because each takes
 * its own storage handle.
 */
export interface Store {
  /** The storage scope the store was constructed with. */
  readonly namespace: string;
  /** Provisions storage. Idempotent; never alters existing data. */
  init(): Promise<void>;
  /** Admits an immutable record, accepting byte-identical retries. */
  put(record: Record): Promise<void>;
  /** Returns a stored record after fail-closed verification. */
  get(id: string): Promise<Record>;
  /** Erases a record's present originals, keeping their commitments. */
  purge(id: string): Promise<void>;
}

/**
 * The optional transactional extension: `put` and `get` joined to a
 * caller-owned transaction `Tx` (a mysql2 `PoolConnection`, a better-sqlite3
 * `Database`). Only the SQL backends implement it; detect it with
 * {@link isTxStore}. `putTx` never commits or retries; on any error the caller
 * rolls back the whole transaction. With `lock`, `getTx` serializes the read
 * with concurrent `putTx` and `purge` on the same record (MySQL `FOR UPDATE`;
 * SQLite already serializes writers).
 */
export interface TxStore<Tx> extends Store {
  putTx(tx: Tx, record: Record): Promise<void>;
  getTx(tx: Tx, id: string, lock?: boolean): Promise<Record>;
}

/** Reports whether a store implements the transactional extension. */
export function isTxStore<Tx = unknown>(store: Store): store is TxStore<Tx> {
  const candidate = store as Partial<TxStore<Tx>>;
  return (
    typeof candidate.putTx === "function" &&
    typeof candidate.getTx === "function"
  );
}
