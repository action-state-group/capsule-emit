import type { KeyObject } from "node:crypto";

/**
 * The draft revision every Capsule this package produces carries. A producer
 * conforming to draft -05 emits -05.
 */
export const SPEC_VERSION = "draft-mih-scitt-agent-action-capsule-05";
/**
 * The revisions a verifier accepts for format 4: -04 and -05. Informational
 * only: `spec_version` selects no digest or verification algorithm (those
 * follow `format_version` and `canonicalization_id`), and an unrecognized
 * value is never by itself a reason to reject, so no verifier here branches
 * on it. Same rule and values as agent-action-capsule's
 * `CURRENT_SPEC_VERSION` / `ACCEPTED_SPEC_VERSIONS`.
 */
export const ACCEPTED_SPEC_VERSIONS: readonly string[] = Object.freeze([
  "draft-mih-scitt-agent-action-capsule-04",
  SPEC_VERSION,
]);
export const FORMAT_VERSION = "4";
export const CANONICALIZATION_ID = "jcs";
export const CONTENT_TYPE = "application/agent-action-capsule-id";

export interface Model {
  readonly provider?: string;
  readonly modelId?: string;
}
export interface ComputeAttestation {
  readonly agentInputDigest?: string;
  readonly agentOutputDigest?: string;
  readonly runtime?: string;
}
export interface Disposition {
  readonly decision: string;
  readonly approver: "human" | "policy" | "counterparty";
  readonly humanDisposed: boolean;
  readonly verdictClass?: string;
  readonly reasonDigest?: string;
}
export interface Effect {
  readonly type: string;
  readonly status:
    | "planned"
    | "dispatched"
    | "confirmed"
    | "failed"
    | "reverted";
  readonly irreversibilityClass: string;
  readonly effectAttestation?: string;
  readonly requestDigest?: string;
  readonly responseDigest?: string;
  readonly externalRef?: string;
}
export interface Chain {
  readonly parentCapsuleId: string;
  readonly relation: string;
}

/** A draft-04 §5.5.5 citation under the referenced artifact's CPB digest context. */
export interface Reference {
  readonly type: string;
  readonly digestAlg: string;
  readonly digest: string;
  readonly citationPurpose?: string;
  /** Opaque log_id, leaf_index and inclusion_proof claims; no proof is authenticated. */
  readonly logCoordinates?: Readonly<Record<string, unknown>>;
}
export interface Input {
  readonly actionId: string;
  readonly actionType: "fyi" | "decide";
  readonly operator: string;
  readonly developer: string;
  readonly timestamp: Date | string;
  readonly epochId?: string;
  readonly domain?: string;
  readonly provenance?: string;
  readonly disposition?: Disposition;
  readonly effect?: Effect;
  readonly chain?: Chain;
  readonly references?: readonly Reference[];
  readonly model?: Model;
  readonly compute?: ComputeAttestation;
}
export interface BuiltPayload {
  readonly capsuleId: string;
  readonly value: Readonly<Record<string, unknown>>;
  readonly json: Uint8Array;
}
export interface Result {
  readonly capsuleId: string;
  readonly payload: Uint8Array;
  readonly envelope: Uint8Array;
}
export interface SigningIdentity {
  readonly privateKey: KeyObject;
  readonly publicKey: Uint8Array;
}
export type Slot = "who" | "can" | "did" | "audit";
export interface SlotMember {
  readonly slot: Slot;
  readonly member: BuiltPayload | Result;
}
export interface SealInput {
  readonly capsule: Input;
  readonly payload?: unknown;
  readonly agentOutput?: unknown;
  readonly model?: Model;
  readonly runtime?: string;
  readonly members?: readonly SlotMember[];
  readonly identity: SigningIdentity;
}
export interface EnvelopeFinding {
  readonly code: string;
  readonly detail: string;
}
export interface EnvelopeVerificationResult {
  readonly ok: boolean;
  readonly findings: readonly EnvelopeFinding[];
  readonly capsuleId: string;
  readonly publicKey?: Uint8Array;
}
