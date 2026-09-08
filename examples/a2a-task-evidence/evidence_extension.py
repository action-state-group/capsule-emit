# SPDX-License-Identifier: Apache-2.0
"""Independent reference implementation of the A2A ``verifiable-record``
task-evidence extension (v3.1 discussion draft).

Not an official A2A extension. See ``_work/a2a-verifiable-task-evidence-
proposal-v3-2026-09-04.md`` (this repo's ``docs/a2a-extension/README.md``
links the published copy) for the normative prose this code implements --
section references below (``§7``, ``§9``, ``§12``, ...) point there.

This module implements the SERVER-side recording step only: building the
Task Evidence payload (§12), sealing it as an AAC capsule, forcing an
immediate checkpoint + registration (§10) with a configured Transparency
Service, and assembling the ``TaskEvidenceReference`` (§9) plus the
immutable core manifest (§11) the extension attaches to the terminal Task.
``verifier.py`` is the independent reader side.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from capsule_emit import seal
from capsule_emit.bundle import bundle as build_bundle
from capsule_emit.signing import AuthorshipVerdict, verify_capsule_signature_tristate
from capsule_emit.witness import WitnessRequiredError

#: Provisional extension URI (§0). Independent publication under our own
#: URI, per the proposal's abstract -- not an A2A-owned namespace.
EXTENSION_URI = "https://agentactioncapsule.org/extensions/a2a-task-evidence/v1"

#: The one record profile this reference implementation speaks (§6, §7).
RECORD_PROFILE_URI = "https://agentactioncapsule.org/profiles/a2a-task-evidence/aac-cll-scitt/v1"

#: §7 ``requirement`` values.
REQUIREMENT_PREFERRED = "PREFERRED"
REQUIREMENT_REQUIRED = "REQUIRED"

#: §8 terminal ``evidenceStatus`` values. ``PENDING`` is deliberately absent
#: here -- the proposal forbids it on a terminal Task.
EVIDENCE_STATUS_SIGNED_RECORD = "EVIDENCE_STATUS_SIGNED_RECORD"
EVIDENCE_STATUS_EXTERNALLY_REGISTERED = "EVIDENCE_STATUS_EXTERNALLY_REGISTERED"
EVIDENCE_STATUS_CONTINUITY_WITNESSED = "EVIDENCE_STATUS_CONTINUITY_WITNESSED"
EVIDENCE_STATUS_FAILED = "EVIDENCE_STATUS_FAILED"

#: §9 delivery modes.
DELIVERY_MODE_INLINE_METADATA = "INLINE_METADATA"
DELIVERY_MODE_EVIDENCE_ARTIFACT = "EVIDENCE_ARTIFACT"

#: Ordering used to compare an achieved ``evidenceStatus`` against a
#: negotiated ``minimumEvidence`` (§7, §8). ``FAILED`` sorts lowest.
EVIDENCE_LEVEL_ORDER = {
    EVIDENCE_STATUS_FAILED: 0,
    EVIDENCE_STATUS_SIGNED_RECORD: 1,
    EVIDENCE_STATUS_EXTERNALLY_REGISTERED: 2,
    EVIDENCE_STATUS_CONTINUITY_WITNESSED: 3,
}

#: §12 exclusion set is enforced by construction: this dict is built fresh
#: from only these fields, so nothing added later (envelope, checkpoint,
#: receipts, the extension metadata itself) can ever be folded in.
_PAYLOAD_FIELDS = (
    "authority",
    "tenantId",
    "taskId",
    "contextId",
    "transcript",
    "terminalState",
    "nonEvidenceArtifacts",
    "sealTime",
    "capturePolicyId",
)


def canonical_json_digest(value: Any) -> str:
    """SHA-256 hex over a deterministic, sorted-key/compact JSON encoding.

    This reference implementation's declared canonicalization (§12, §11)
    is intentionally simple -- sorted-key, compact-separator JSON, no
    Unicode/number normalization beyond what ``json.dumps`` does. The
    normative profile still has to define float/Unicode/map-ordering
    handling for cross-binding vectors (§12); this is a reference
    implementation of ONE binding (a2a-sdk JSON-RPC over Python objects),
    not that cross-binding conformance suite.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_task_evidence_payload(
    *,
    authority: str,
    task_id: str,
    context_id: str,
    transcript: list[dict[str, Any]],
    terminal_state: str,
    non_evidence_artifacts: list[dict[str, Any]],
    seal_time: str,
    capture_policy_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Build the §12 Task Evidence semantic projection.

    ``transcript`` is a list of accepted-Message records, each already
    reduced to ``{messageId, role, sequence, partCommitments, metadataDigest}``
    -- never ``Task.history`` (§12: A2A does not guarantee that persists).
    ``non_evidence_artifacts`` is every terminal Artifact OTHER than the
    evidence Artifact itself, each reduced to ``{artifactId, name,
    contentCommitment}``.
    """
    payload = {
        "authority": authority,
        "tenantId": tenant_id,
        "taskId": task_id,
        "contextId": context_id,
        "transcript": transcript,
        "terminalState": terminal_state,
        "nonEvidenceArtifacts": non_evidence_artifacts,
        "sealTime": seal_time,
        "capturePolicyId": capture_policy_id,
    }
    assert set(payload) == set(_PAYLOAD_FIELDS), "payload field set drifted from §12 exclusion set"
    return payload


@dataclass
class TaskEvidenceResult:
    """Everything the server-side recorder produced for one terminal Task."""

    evidence_status: str
    task_evidence_payload: dict[str, Any]
    evidence_id: str
    capsule: dict[str, Any] | None
    manifest: dict[str, Any] | None
    core_manifest_digest: str | None
    task_evidence_reference: dict[str, Any]
    failure_reason: str | None = None


def _manifest_from_bundle(*, task_evidence_payload, capsule, bundle_obj, capture_policy_id) -> dict:
    """Assemble the §11 immutable core manifest from the sealed capsule and
    its checkpoint bundle. Excludes ``coreManifestDigest`` itself and any
    ``Task.metadata[extensionUri]`` field -- the digest is carried outside
    its own preimage (§11)."""
    return {
        "taskEvidencePayload": task_evidence_payload,
        "producerEnvelope": {
            "capsuleId": capsule["capsule_id"],
            "keyId": capsule.get("key_id"),
            "signature": capsule.get("signature"),
        },
        "cllCheckpoint": bundle_obj.checkpoint.to_dict(),
        "localInclusionProof": {
            "v": bundle_obj.inclusion_proof.v,
            "kind": bundle_obj.inclusion_proof.kind,
            "size": bundle_obj.inclusion_proof.size,
            "leafIndex": bundle_obj.inclusion_proof.leaf_index,
            "witness": list(bundle_obj.inclusion_proof.witness),
            "peaksLeft": list(bundle_obj.inclusion_proof.peaks_left),
            "peaksRight": list(bundle_obj.inclusion_proof.peaks_right),
        },
        "priorCheckpoint": bundle_obj.prior_checkpoint.to_dict() if bundle_obj.prior_checkpoint else None,
        "consistencyProof": bundle_obj.consistency_proof.__dict__ if bundle_obj.consistency_proof else None,
        "capturePolicyId": capture_policy_id,
    }


def seal_task_evidence(
    task_evidence_payload: dict[str, Any],
    *,
    ledger_path: str,
    ts_url: str,
) -> TaskEvidenceResult:
    """Server-side recording step (§8, §10).

    Freezes ``task_evidence_payload``, seals it as an AAC capsule, and
    forces an IMMEDIATE synchronous checkpoint + registration with
    ``ts_url`` (``require_witness=True`` -- see ``capsule_emit.witness``)
    rather than the library's normal best-effort/cadence-batched default,
    because the proposal requires evidence assembly to complete (or
    typed-fail) before the terminal Task response is emitted (§8).

    Returns a terminal ``evidenceStatus`` per §8's four-state enum --
    ``EVIDENCE_STATUS_PENDING`` never appears here, matching "PENDING is
    forbidden on a terminal Task."
    """
    evidence_id = canonical_json_digest(task_evidence_payload)

    try:
        result = seal(
            task_evidence_payload,
            action="a2a.task_evidence.v1",
            operator="reference-implementation",
            developer="capsule-emit-examples/a2a-task-evidence@v1",
            agent_output={"terminalState": task_evidence_payload["terminalState"]},
            verdict="executed",
            effect={"type": "a2a.task_completed", "status": "confirmed"},
            ledger=ledger_path,
            anchor=False,
            require_witness=True,
            witness_url=ts_url,
        )
    except WitnessRequiredError as exc:
        return TaskEvidenceResult(
            evidence_status=EVIDENCE_STATUS_FAILED,
            task_evidence_payload=task_evidence_payload,
            evidence_id=evidence_id,
            capsule=None,
            manifest=None,
            core_manifest_digest=None,
            task_evidence_reference={
                "extensionVersion": "1",
                "evidenceStatus": EVIDENCE_STATUS_FAILED,
                "failureReason": f"WITNESS_REQUIRED_NOT_CONFIRMED: {exc}",
            },
            failure_reason=str(exc),
        )

    capsule = result.capsule
    bundle_obj = build_bundle(ledger_path, result.capsule_id)

    signature_verdict, _ = verify_capsule_signature_tristate(capsule)
    if signature_verdict is not AuthorshipVerdict.AUTHORED:
        status = EVIDENCE_STATUS_FAILED
    elif result.witness_outcome == "witness_receipt_obtained":
        # Authoritative signal from the just-completed require_witness=True
        # round-trip (capsule_emit.witness) that the configured TS actually
        # confirmed this checkpoint -- not re-derived from an offline stamp
        # check, which would need a trust pin this recorder has no business
        # holding (pinning is the READER's job; see verifier.py's
        # TrustConfig and §11's "the verifier obtains trust anchors from
        # configuration").
        status = EVIDENCE_STATUS_EXTERNALLY_REGISTERED
    else:
        status = EVIDENCE_STATUS_SIGNED_RECORD

    manifest = _manifest_from_bundle(
        task_evidence_payload=task_evidence_payload,
        capsule=capsule,
        bundle_obj=bundle_obj,
        capture_policy_id=task_evidence_payload["capturePolicyId"],
    )
    core_manifest_digest = canonical_json_digest(manifest)

    reference = {
        "extensionVersion": "1",
        "evidenceId": evidence_id,
        "recordProfile": RECORD_PROFILE_URI,
        "coreManifestDigest": core_manifest_digest,
        "evidenceStatus": status,
        "coverageScope": "TASK_TRANSCRIPT",
        "terminalState": task_evidence_payload["terminalState"],
        "deliveryMode": DELIVERY_MODE_EVIDENCE_ARTIFACT,
    }

    return TaskEvidenceResult(
        evidence_status=status,
        task_evidence_payload=task_evidence_payload,
        evidence_id=evidence_id,
        capsule=capsule,
        manifest=manifest,
        core_manifest_digest=core_manifest_digest,
        task_evidence_reference=reference,
    )


def build_evidence_artifact_bundle(evidence_result: TaskEvidenceResult) -> dict[str, Any]:
    """The portable, standalone evidence bundle carried in the A2A Artifact
    (§9 ``EVIDENCE_ARTIFACT`` delivery mode) -- everything ``verifier.py``
    needs, and nothing it should trust without independent checking."""
    return {
        "extensionVersion": "1",
        "recordProfile": RECORD_PROFILE_URI,
        "taskEvidencePayload": evidence_result.task_evidence_payload,
        "evidenceId": evidence_result.evidence_id,
        "capsule": evidence_result.capsule,
        "manifest": evidence_result.manifest,
        "coreManifestDigest": evidence_result.core_manifest_digest,
        "taskEvidenceReference": evidence_result.task_evidence_reference,
    }
