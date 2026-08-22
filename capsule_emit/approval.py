# SPDX-License-Identifier: Apache-2.0
"""Approval record pattern — seal a human-approval capsule chained to a blocked one.

Two public helpers:

``seal_approval(blocked_capsule_id, approver_id, decision, action_digest, *, ledger, anchor)``
    Seals a capsule carrying:

    - ``compute_attestation.human_disposed = True``
    - ``compute_attestation.approver_id`` — string identity (e.g. ``"alice@org.example"``)
    - ``compute_attestation.action_digest`` — same digest that was in the blocked capsule
    - ``disposition.decision`` — ``"approve"`` | ``"deny"``
    - ``disposition.verdict_class`` — ``"executed"`` if approved, ``"denied"`` if denied
    - ``chain.parent_capsule_id`` — the blocked capsule's ID
    - ``chain.relation`` — ``"resolves"``

``list_pending(ledger_path)``
    Returns every capsule in the ledger whose ``verdict_class == "blocked"`` or
    ``effect.status == "planned"`` and for which no downstream capsule carries
    ``chain.relation == "resolves"`` pointing at it.

    Fail-closed: absence of a chained approval capsule means the action is still
    pending.  Nothing is ever marked resolved unless an explicit ``"resolves"``
    chain entry is found.

No engine imports.  This is pure format + ledger pattern — ``seal_approval``'s
``resume_ok``/``resume_reason`` params are a plain data seam a caller feeds
from its own engine (e.g. ``capsule_emit.holds.HoldEngine.hold_status()``),
not an import of one. This is what lets a resume-time approval re-check the
cumulative state a hold may be gating: an approval for a blocked action is
authentication (a human said yes), not by itself proof that dispatching now
is still within budget — the caller is responsible for asking.
"""
from __future__ import annotations

import os
from typing import Any

from .core import EmitResult, _emit_capsule
from .ledger import read_ledger

__all__ = ["seal_approval", "list_pending"]


def seal_approval(
    blocked_capsule_id: str,
    approver_id: str,
    decision: str,
    action_digest: str,
    *,
    ledger: str | os.PathLike,
    anchor: bool = False,
    action: str = "review_action",
    operator: str = "",
    developer: str = "",
    effect_type: str | None = None,
    resume_ok: bool | None = None,
    resume_reason: str | None = None,
) -> EmitResult:
    """Seal a human-approval capsule chained to a blocked capsule.

    Args:
        blocked_capsule_id: The ``capsule_id`` of the blocked capsule being resolved.
        approver_id: String identity of the approver (e.g. ``"alice@org.example"``).
            No identity validation is performed here — this is a string label only.
        decision: ``"approve"`` (allow the action) or ``"deny"`` (reject it).
        action_digest: The digest of the action payload from the blocked capsule.
            Used to bind the approval to the exact action that was blocked.
        ledger: Path to the JSONL ledger file to append the approval capsule to.
        anchor: Whether to fire-and-forget anchor the capsule (default ``False``
            for offline/demo use — pass ``True`` in production).
        action: Stable action name for the approval capsule (default
            ``"review_action"``).
        operator: Tenant/org identifier.
        developer: Agent name + version.
        effect_type: Effect type override.  Defaults to *action*.
        resume_ok: When an action that was blocked pending approval had a
            budget hold reserved for it, the caller re-checks the hold's
            *current* status (e.g. ``HoldEngine.hold_status()``) and reports
            the outcome here — ``True`` when no hold applies, or the hold is
            still active; ``False`` when it is not (expired, released, or
            otherwise no longer valid). ``resume_ok=False`` overrides
            ``decision="approve"`` to ``"deny"``: a late approval against a
            hold that is no longer active is authentication, not
            authorization — a hold's own exposure may have already been
            released and re-consumed elsewhere by the time this approval
            arrives, so dispatching on the strength of a stale decision
            would exceed the budget with every *integrity* check still
            passing. The correct path forward is a fresh evaluation (e.g.
            ``HoldEngine.evaluate_and_reserve()``), not resuming this one.
            Never overrides ``decision="deny"`` — a human denial is never
            silently reinterpreted as approval.

            Default is ``None``, meaning "no hold applies" -- resolved by
            looking ``blocked_capsule_id`` up in *ledger* and checking
            whether it is itself a ``hold.reserve`` record. When it is not
            (an ordinary blocked action with no hold in play), ``None``
            behaves as ``True``. When it IS a hold reserve, leaving
            ``resume_ok`` unset is refused loudly (:class:`ValueError`)
            instead of silently approving -- the old ``True`` default let a
            caller that forgot to wire the resume-time recheck silently
            dispatch over budget with every integrity check still passing.
        resume_reason: Human-readable reason for a ``resume_ok=False``
            override, recorded in ``compute_attestation.resume_check`` for
            the audit trail (e.g. the hold's own status string).

    Returns:
        :class:`~capsule_emit.core.EmitResult` with the sealed approval capsule.

    Raises:
        ValueError: When *decision* is not ``"approve"`` or ``"deny"``, or
            when ``resume_ok`` is left ``None`` for a blocked capsule that is
            itself a hold reserve.
    """
    if decision not in ("approve", "deny"):
        raise ValueError(
            f"decision must be 'approve' or 'deny', got {decision!r}"
        )

    if resume_ok is None:
        blocked_record = next(
            (c for c in read_ledger(ledger) if c.get("capsule_id") == blocked_capsule_id), None,
        )
        chains_from_reserve = bool(blocked_record) and (blocked_record.get("action_id") or "").startswith(
            "hold.reserve/"
        )
        if chains_from_reserve:
            raise ValueError(
                f"resume_ok must be explicitly True or False -- blocked capsule "
                f"{blocked_capsule_id[:16]}… is a hold reserve, so resuming it requires the caller's own "
                "resume-time recheck (e.g. HoldEngine.hold_status()); forgetting to wire it in must be loud, "
                "not a silent approve"
            )
        resume_ok = True  # no hold applies

    if decision == "approve" and not resume_ok:
        decision = "deny"

    verdict = "executed" if decision == "approve" else "denied"
    _effect_type = effect_type or action

    # Denied verdicts are in NEVER_DISPATCH_VERDICT_CLASSES (§5.4.2) — the spec
    # forbids pairing them with effect.status "dispatched" / "confirmed" / etc.
    # A denial means the action never runs, so there is no effect to record.
    # Approved resolutions carry status="dispatched" (the action is now cleared to run).
    _effect = (
        {"type": _effect_type, "status": "dispatched"}
        if decision == "approve"
        else None
    )

    extra_compute: dict[str, Any] = {
        "human_disposed": True,
        "approver_id": approver_id,
        "action_digest": action_digest,
    }
    if not resume_ok:
        extra_compute["resume_check"] = {"ok": False, "reason": resume_reason}

    return _emit_capsule(
        action=action,
        operator=operator,
        developer=developer,
        # Human disposition — requires approver="human" per capsule-emit invariant
        human_disposed=True,
        approver="human",
        decision=decision,
        verdict=verdict,
        # Chain: resolves the blocked capsule
        confirms=blocked_capsule_id,
        relation="resolves",
        # Effect (only for approvals; denied actions have no dispatched effect)
        effect=_effect,
        # Ledger + anchor
        ledger=ledger,
        anchor=anchor,
        # Approval record fields in compute_attestation
        extra_compute=extra_compute,
    )


def list_pending(ledger_path: str | os.PathLike) -> list[dict[str, Any]]:
    """Return all capsules that are blocked with no resolution chained to them.

    A capsule is *pending* when:

    1. Its ``disposition.verdict_class == "blocked"`` OR its
       ``effect.status == "planned"``
    2. No other capsule in the same ledger has
       ``chain.relation == "resolves"`` **and**
       ``chain.parent_capsule_id == <this capsule's capsule_id>``

    Fail-closed semantics: if the resolution capsule is absent (e.g. the
    process crashed before :func:`seal_approval` ran), the blocked capsule
    remains in the pending list on every subsequent call.  A blocked capsule
    is only removed from pending once an explicit ``"resolves"`` chain entry
    is present in the same ledger.

    Args:
        ledger_path: Path to the JSONL ledger file.  Returns ``[]`` when the
            file does not exist or is empty.

    Returns:
        List of capsule dicts that are blocked and unresolved, in ledger order.
    """
    capsules = read_ledger(ledger_path)
    if not capsules:
        return []

    # Collect the set of capsule_ids that have been explicitly resolved
    resolved_ids: set[str] = set()
    for cap in capsules:
        chain = cap.get("chain") or {}
        if chain.get("relation") == "resolves":
            parent = chain.get("parent_capsule_id")
            if parent:
                resolved_ids.add(parent)

    # Find all blocked/planned capsules that have not been resolved
    pending: list[dict[str, Any]] = []
    for cap in capsules:
        disp = cap.get("disposition") or {}
        eff = cap.get("effect") or {}
        is_blocked = (
            disp.get("verdict_class") == "blocked"
            or eff.get("status") == "planned"
        )
        if not is_blocked:
            continue
        cid = cap.get("capsule_id", "")
        if cid not in resolved_ids:
            pending.append(cap)

    return pending
