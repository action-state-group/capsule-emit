# SPDX-License-Identifier: Apache-2.0
"""The §13 structured verification result -- ten independent properties,
each one of ``PASS`` / ``FAIL`` / ``NOT_PRESENT`` / ``NOT_CHECKED`` /
``INCONCLUSIVE``, with diagnostic detail. There is deliberately no
aggregate ``trusted: true`` (§13): a caller reads the properties it cares
about and applies its own policy.

Independent of the recorder in ``evidence_extension.py`` -- this module
never calls ``seal()`` or touches a ledger; it takes a portable evidence
bundle (as produced by ``evidence_extension.build_evidence_artifact_bundle``
and carried in the A2A Artifact) plus the verifier's OWN observed Task
context and trust configuration, and reports what independently checks out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cll.checkpoint as cll_checkpoint
from cll.checkpoint import core as mmr_core
from cll.checkpoint.emit import StampVerdict, verify_witness_stamp_tristate
from evidence_extension import canonical_json_digest

from capsule_emit.signing import AuthorshipVerdict, verify_capsule_signature_tristate

PASS = "PASS"
FAIL = "FAIL"
NOT_PRESENT = "NOT_PRESENT"
NOT_CHECKED = "NOT_CHECKED"
INCONCLUSIVE = "INCONCLUSIVE"

#: The ten §13 property names, in table order.
PROPERTIES = (
    "contentBinding",
    "producerSignature",
    "taskBinding",
    "localInclusion",
    "checkpointSignature",
    "externalRegistration",
    "continuity",
    "identityAuthorityBinding",
    "captureCoverage",
    "outcomeCorroboration",
)


@dataclass(frozen=True)
class PropertyResult:
    state: str
    detail: str


@dataclass(frozen=True)
class VerificationResult:
    properties: dict[str, PropertyResult] = field(default_factory=dict)

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {name: {"state": r.state, "detail": r.detail} for name, r in self.properties.items()}

    def state(self, name: str) -> str:
        return self.properties[name].state


@dataclass(frozen=True)
class ObservedTaskContext:
    """What the VERIFIER independently observed via ``GetTask`` -- never
    taken from inside the bundle it is checking. ``taskBinding`` compares
    the bundle's self-reported fields against these."""

    authority: str
    task_id: str
    context_id: str
    terminal_state: str
    non_evidence_artifact_ids: frozenset[str]


@dataclass(frozen=True)
class TrustConfig:
    """Trust anchors come from configuration, never from the bundle (§11:
    "Bundle-carried keys are cryptographicKeyMaterial, not trust anchors.
    The verifier obtains trust anchors from configuration...")."""

    ts_pubkeys: dict[str, bytes] = field(default_factory=dict)
    authorized_signers: dict[str, frozenset[str]] = field(default_factory=dict)


def verify_evidence_bundle(
    evidence_bundle: dict[str, Any],
    *,
    observed_task: ObservedTaskContext,
    trust: TrustConfig,
) -> VerificationResult:
    props: dict[str, PropertyResult] = {}

    payload = evidence_bundle.get("taskEvidencePayload")
    claimed_evidence_id = evidence_bundle.get("evidenceId")
    capsule = evidence_bundle.get("capsule")

    # -- contentBinding -----------------------------------------------
    if payload is None or claimed_evidence_id is None:
        props["contentBinding"] = PropertyResult(NOT_PRESENT, "no taskEvidencePayload/evidenceId in bundle")
    else:
        recomputed = canonical_json_digest(payload)
        if recomputed == claimed_evidence_id:
            props["contentBinding"] = PropertyResult(PASS, f"recomputed evidenceId matches: {recomputed}")
        else:
            props["contentBinding"] = PropertyResult(
                FAIL,
                f"recomputed evidenceId {recomputed} != claimed {claimed_evidence_id} "
                "-- the presented payload does not derive the referenced evidence ID",
            )

    # -- producerSignature ----------------------------------------------
    if capsule is None:
        props["producerSignature"] = PropertyResult(NOT_PRESENT, "no producer envelope (capsule) in bundle")
    else:
        verdict, messages = verify_capsule_signature_tristate(capsule)
        if verdict is AuthorshipVerdict.AUTHORED:
            props["producerSignature"] = PropertyResult(PASS, "producer key signed the carried capsule_id")
        elif verdict is AuthorshipVerdict.UNCLAIMED:
            props["producerSignature"] = PropertyResult(NOT_PRESENT, "capsule carries no signature/key_id claim")
        else:
            props["producerSignature"] = PropertyResult(FAIL, "; ".join(messages) or "signature invalid")

    # -- taskBinding ------------------------------------------------------
    if payload is None:
        props["taskBinding"] = PropertyResult(NOT_PRESENT, "no payload to bind")
    else:
        mismatches = []
        if payload.get("authority") != observed_task.authority:
            mismatches.append("authority")
        if payload.get("taskId") != observed_task.task_id:
            mismatches.append("taskId")
        if payload.get("contextId") != observed_task.context_id:
            mismatches.append("contextId")
        if payload.get("terminalState") != observed_task.terminal_state:
            mismatches.append("terminalState")
        bundle_artifact_ids = frozenset(
            a.get("artifactId") for a in payload.get("nonEvidenceArtifacts", [])
        )
        if bundle_artifact_ids != observed_task.non_evidence_artifact_ids:
            mismatches.append("nonEvidenceArtifacts")
        if mismatches:
            props["taskBinding"] = PropertyResult(FAIL, f"mismatched fields vs observed Task: {mismatches}")
        else:
            props["taskBinding"] = PropertyResult(PASS, "payload binds the observed authority/task/transcript/artifacts")

    # -- log-integrity properties: localInclusion, checkpointSignature,
    #    externalRegistration -- computed from the manifest's checkpoint
    #    bundle fragment, independent of contentBinding above.
    manifest = evidence_bundle.get("manifest") or {}
    checkpoint_dict = manifest.get("cllCheckpoint")
    inclusion_dict = manifest.get("localInclusionProof")

    if not checkpoint_dict or not inclusion_dict or not capsule:
        props["localInclusion"] = PropertyResult(NOT_PRESENT, "no checkpoint/inclusion proof in bundle")
        props["checkpointSignature"] = PropertyResult(NOT_PRESENT, "no checkpoint in bundle")
        props["externalRegistration"] = PropertyResult(NOT_PRESENT, "no checkpoint in bundle")
    else:
        checkpoint = cll_checkpoint.CheckpointRecord(
            v=checkpoint_dict["v"],
            kind=checkpoint_dict["kind"],
            log_id=checkpoint_dict["log_id"],
            mmr_size=checkpoint_dict["mmr_size"],
            root=checkpoint_dict["root"],
            prev_size=checkpoint_dict["prev_size"],
            prev_root=checkpoint_dict["prev_root"],
            key_id=checkpoint_dict["key_id"],
            timestamp=checkpoint_dict["timestamp"],
            signature=checkpoint_dict["signature"],
            witnesses=[
                cll_checkpoint.WitnessRecord(**w) for w in checkpoint_dict.get("witnesses", [])
            ],
        )
        proof = cll_checkpoint.InclusionProof(
            v=inclusion_dict["v"],
            kind=inclusion_dict["kind"],
            size=inclusion_dict["size"],
            leaf_index=inclusion_dict["leafIndex"],
            witness=inclusion_dict["witness"],
            peaks_left=inclusion_dict["peaksLeft"],
            peaks_right=inclusion_dict["peaksRight"],
        )
        root = bytes.fromhex(checkpoint.root)
        body_digest = bytes.fromhex(capsule["capsule_id"])
        included = mmr_core.verify_inclusion(root, checkpoint.mmr_size, proof.leaf_index, body_digest, proof)
        props["localInclusion"] = PropertyResult(
            PASS if included else FAIL,
            f"leaf_index={proof.leaf_index} against checkpoint root at mmr_size={checkpoint.mmr_size}",
        )

        sig_ok = cll_checkpoint.verify_checkpoint_signature_offline(checkpoint)
        props["checkpointSignature"] = PropertyResult(
            PASS if sig_ok else FAIL,
            f"checkpoint signature under key_id={checkpoint.key_id}",
        )

        if not checkpoint.witnesses:
            props["externalRegistration"] = PropertyResult(NOT_PRESENT, "checkpoint carries no witness stamp")
        else:
            any_witnessed = False
            any_invalid = False
            any_unverified = False
            details = []
            for w in checkpoint.witnesses:
                pin = trust.ts_pubkeys.get(w.ts_url)
                verdict, errors = verify_witness_stamp_tristate(checkpoint, w, ts_pubkey_pem=pin)
                details.append(f"{w.ts_url}: {verdict.value}" + (f" ({'; '.join(errors)})" if errors else ""))
                if verdict is StampVerdict.WITNESSED:
                    any_witnessed = True
                elif verdict is StampVerdict.INVALID:
                    any_invalid = True
                else:
                    any_unverified = True
            if any_witnessed:
                props["externalRegistration"] = PropertyResult(PASS, "; ".join(details))
            elif any_invalid:
                props["externalRegistration"] = PropertyResult(FAIL, "; ".join(details))
            elif any_unverified:
                props["externalRegistration"] = PropertyResult(
                    INCONCLUSIVE, "stamp present but no pinned key configured for its TS: " + "; ".join(details)
                )
            else:
                props["externalRegistration"] = PropertyResult(NOT_PRESENT, "no evaluable witness stamps")

    # -- continuity: OPTIONAL, not implemented by this base profile (§10 item 8).
    props["continuity"] = PropertyResult(NOT_PRESENT, "v1 base profile carries no continuity assertion")

    # -- identityAuthorityBinding -----------------------------------------
    authority = (payload or {}).get("authority")
    key_id = (capsule or {}).get("key_id")
    if not trust.authorized_signers:
        props["identityAuthorityBinding"] = PropertyResult(
            NOT_CHECKED, "no identity/authority policy configured for this verifier"
        )
    elif authority is None or key_id is None:
        props["identityAuthorityBinding"] = PropertyResult(NOT_PRESENT, "no authority/key_id to check")
    elif key_id in trust.authorized_signers.get(authority, frozenset()):
        props["identityAuthorityBinding"] = PropertyResult(PASS, f"key_id={key_id} authorized for authority={authority}")
    else:
        props["identityAuthorityBinding"] = PropertyResult(
            FAIL, f"key_id={key_id} is not an authorized signer for authority={authority}"
        )

    # -- captureCoverage: descriptive, never a completeness proof from one
    #    record (§13: "captureCoverage... remains unestablished" even on an
    #    otherwise-passing bundle). This reference verifier reports what
    #    policy the payload declares and stops there.
    capture_policy_id = (payload or {}).get("capturePolicyId")
    if capture_policy_id:
        props["captureCoverage"] = PropertyResult(
            INCONCLUSIVE,
            f"payload declares capturePolicyId={capture_policy_id}; a single record cannot "
            "establish that every eligible interaction under that policy was captured",
        )
    else:
        props["captureCoverage"] = PropertyResult(NOT_PRESENT, "no capturePolicyId declared")

    # -- outcomeCorroboration: out of scope for v3.1 (§15) -- never
    #    collected by this reference implementation.
    props["outcomeCorroboration"] = PropertyResult(
        NOT_PRESENT, "no independent outcome corroboration collected (out of scope, §15)"
    )

    return VerificationResult(properties=props)
