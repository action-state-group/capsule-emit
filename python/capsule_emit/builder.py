# SPDX-License-Identifier: Apache-2.0
"""capsule-emit's Capsule builder: from "an action just occurred" to a sealed
format-4 Capsule.

Producing Capsules is capsule-emit's job (agent-action-capsule#281). This is
the builder agent-action-capsule shipped as ``agent_action_capsule.emit()``
up to 0.8.1, moved here unchanged in behaviour so every Capsule this package
seals stays byte-for-byte the same; agent-action-capsule 0.9.0 deprecates its
copy. It builds the ``model_attestation``, ``assurance``, default
``disposition`` and ``chain`` blocks and seals with
``agent_action_capsule.Capsule.seal()``, AAC's public boundary.
:func:`capsule_emit.core.seal` calls it.

**action_type convention**:
- ``"fyi"`` — passive observation (default).
- ``"decide"`` — consequential; a gate decision or a tool call with effects.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from typing import Any

from agent_action_capsule.canonical import CANONICALIZATION_JCS
from agent_action_capsule.contracts import (
    AssuranceBlock,
    Chain,
    ConstraintRecord,
    Disposition,
    EffectRecord,
    InvariantError,
    ModelAttestation,
    ProvenanceMode,
    ReferenceEntry,
    SelfReportedReasoning,
    derive_effect_mode,
)
from agent_action_capsule.parse import Capsule
from agent_action_capsule.versions import DEFAULT_FORMAT_VERSION, DEFAULT_SPEC_VERSION

__all__ = ["build_capsule"]


def _utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat().replace("+00:00", "Z")


def build_capsule(
    action_id: str | None = None,
    action_type: str = "fyi",
    operator: str = "",
    developer: str = "",
    *,
    model_id: str | None = None,
    provider: str | None = None,
    timestamp: str | None = None,
    compute_attestation: dict[str, Any] | None = None,
    effect: EffectRecord | None = None,
    prior_capsule_id: str | None = None,
    chain_relation: str | None = None,
    disposition: Disposition | None = None,
    constraints: tuple[ConstraintRecord, ...] = (),
    references: tuple[ReferenceEntry, ...] | None = None,
    domain: str | None = None,
    provenance: str | None = None,
    provenance_mode: ProvenanceMode | None = None,
    self_reported_reasoning_digest: str | None = None,
    spec_version: str = DEFAULT_SPEC_VERSION,
    format_version: str = DEFAULT_FORMAT_VERSION,
    # Adapter-tier convenience params: used to build action_id when not given.
    tool_name: str | None = None,
    tool_input: Any = None,  # noqa: ARG001 — carried for future traceability
    tool_output: Any = None,  # noqa: ARG001 — carried for future traceability
) -> dict:
    """Build and seal a Capsule with self-attestation (§5.3).

    Returns the sealed dict with ``capsule_id`` computed over the canonical
    capsule form (§5.1). When ``model_id`` and ``provider`` are supplied, the
    ``model_attestation`` block is included and committed to the ``capsule_id``;
    any post-seal tamper to those fields makes ``verify()`` fail.

    Args:
        action_id: Unique identifier for this action. Defaults to a UUID4
            incorporating ``tool_name`` when not supplied.
        action_type: Action category. ``"fyi"`` (default) for passive
            observation; ``"decide"`` for consequential gate decisions.
        operator: The tenant / scoping party identifier (spec §5.1).
        developer: The agent identifier + version (spec §5.1).
        model_id: Model name/version, e.g. ``"claude-sonnet-4-6"``. When
            supplied together with ``provider``, a ``model_attestation`` block
            is created and committed to the ``capsule_id``.
        provider: Inference provider, e.g. ``"anthropic"``.
        timestamp: ISO-8601 / RFC 3339 UTC string; defaults to now.
        compute_attestation: Best-effort compute context. Committed to
            ``capsule_id`` when ``model_id`` + ``provider`` are also present.
        effect: Optional effect record (rung 2 may/did binding).
        prior_capsule_id: 64-hex capsule_id of the capsule this one follows.
        chain_relation: Registry-governed chain.relation value (default
            ``"follows"``). Ignored when ``prior_capsule_id`` is None.
        disposition: Optional disposition block (§5.4). When omitted on an
            ``"fyi"`` action a sensible default is applied automatically.
        constraints: Constraint records (§8.1); defaults to empty tuple.
        references: Cross-record citations to a record outside this Capsule's
            own ``chain`` scope (§5.5.5, {{xref}}). ``None`` (default) omits
            the ``references`` key; ``()`` emits ``"references": []`` — the two
            are semantically equivalent ("no such citation") but committed as
            distinct bytes, so pass ``()`` explicitly only when the distinction
            matters to a caller.
        domain: Capsule epistemic role — ``"action"`` (default when absent),
            ``"memory"``, or ``"reasoning"``. ``"reasoning"`` marks a STANDALONE
            reasoning/thinking step, not an action-with-reasoning (which stays
            ``"action"``). Registry-governed; unknown values are informational.
        provenance: Dedup rank signal — ``"gate"``(3), ``"runtime"``(2), or
            ``"collector"``(1). Absent implies ``"runtime"``. Higher rank wins
            on dedup. Registry-governed; unknown values are informational.
        provenance_mode: Backfill-vs-contemporaneous mode (§5.3(bis));
            distinct from ``provenance`` above. ``None`` (default) omits the
            block, implying ``mode: "contemporaneous"``. Pass a
            ``ProvenanceMode(mode="backfilled", ...)`` to mark an imported
            historical record; its constructor enforces the four companion
            fields the spec REQUIRES for that mode.
        self_reported_reasoning_digest: 64-hex SHA-256 digest of the CoT
            or reasoning content that produced this action. Self-reported and
            unattested — committed to capsule_id so tampering is detectable,
            but faithfulness is not verified (§-02).
        spec_version: Spec revision string (defaults to ``-05``).
        format_version: Serialization suite version (defaults to ``"4"``).
        tool_name: Name of the tool that was called. Used to build a readable
            ``action_id`` when one is not provided.
        tool_input: Tool input (currently ignored in the capsule body; reserved
            for future traceability fields).
        tool_output: Tool output (currently ignored in the capsule body; reserved
            for future traceability fields).

    Returns:
        Sealed capsule dict with ``capsule_id`` at the top level.
    """
    # Derive action_id from tool_name if not supplied.
    if action_id is None:
        base = tool_name or "tool-call"
        action_id = f"{base}/{uuid.uuid4()}"

    ts = timestamp or _utc_now()

    if format_version != DEFAULT_FORMAT_VERSION:
        raise InvariantError(
            "build_capsule() creates only format_version '4'; format_version '2' is verification-only"
        )

    # ModelAttestation: with model identity when both are given; compute-only
    # when only compute_attestation is present (commits I/O digests without model).
    model_att: ModelAttestation | None = None
    if model_id is not None and provider is not None:
        model_att = ModelAttestation(
            model_id=model_id,
            provider=provider,
            compute_attestation=compute_attestation,
        )
    elif compute_attestation is not None:
        model_att = ModelAttestation(compute_attestation=compute_attestation)

    chain: Chain | None = None
    if prior_capsule_id is not None:
        # Adapter tier (tool_name set) defaults to "sequence"; full API defaults to "follows".
        # "follows" is the registered §6 bare next-link relation; "sequence" is a
        # deployed legacy alias slated for migration to "follows" (spec/REGISTRY.md §6,
        # "Deployed legacy alias"). Kept as-is here so deployed adapter streams stay
        # byte-stable until the migration is cut.
        if chain_relation is None:
            rel = "sequence" if tool_name is not None else "follows"
        else:
            rel = chain_relation
        chain = Chain(parent_capsule_id=prior_capsule_id, relation=rel)

    if effect is not None and effect.effect_attestation is None:
        status = effect.status
        if status in ("dispatched", "confirmed", "failed", "reverted"):
            effect = dataclasses.replace(effect, effect_attestation="runtime_claimed")

    effect_dict = dataclasses.asdict(effect) if effect is not None else None
    effect_mode = derive_effect_mode(effect_dict)
    ledger_mode = "chained" if chain is not None else "standalone"

    assurance = AssuranceBlock(
        attestation_mode="self_attested",
        effect_mode=effect_mode,
        ledger_mode=ledger_mode,
    )

    # Default disposition for fyi/observer calls: accept, policy-approved, executed.
    if disposition is None and action_type == "fyi":
        disposition = Disposition(
            decision="accept",
            approver="policy",
            human_disposed=False,
            verdict_class="executed",
        )

    srr = SelfReportedReasoning(digest=self_reported_reasoning_digest) if self_reported_reasoning_digest is not None else None

    capsule = Capsule(
        spec_version=spec_version,
        format_version=format_version,
        action_id=action_id,
        action_type=action_type,
        operator=operator,
        developer=developer,
        timestamp=ts,
        canonicalization_id=CANONICALIZATION_JCS,
        domain=domain,
        provenance=provenance,
        provenance_mode=provenance_mode,
        model_attestation=model_att,
        self_reported_reasoning=srr,
        effect=effect,
        assurance=assurance,
        disposition=disposition,
        chain=chain,
        constraints=constraints,
        references=references,
    )
    return capsule.seal()
