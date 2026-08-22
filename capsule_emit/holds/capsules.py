# SPDX-License-Identifier: Apache-2.0
"""Builders for the hold-lifecycle record types: ``hold.reserve``,
``hold.release``, ``hold.expire``, ``hold.reconcile``, plus a plain
decision capsule for a refused reservation attempt.

A reservation is a capsule, not side-band state: reserve/release/expire/
reconcile are sealed, appended records built the same way every other
capsule-emit capsule is built (``agent_action_capsule.Capsule`` +
self-attestation) — same digest-then-seal sequence, so these records are
ordinary, independently verifiable ledger capsules with no special-cased
verification path. Unlike a private ledger's signed-record model, these are
self-attested (``assurance.attestation_mode = "self_attested"``), matching
every other capsule this library emits — integrity comes from the
``capsule_id`` digest and (optionally) an external transparency anchor, not
a locally-managed signing key.

Record-type identity lives in the ``action_id`` verb (``hold.reserve/<uuid>``
etc.) — the same place every capsule-emit capsule carries its verb — not a
new top-level field, and not the spec's closed ``action_type`` enum (§5.1
restricts ``action_type`` to ``fyi``/``decide``; these are ``decide``, since
a reservation is a consequential decision about exposure).

``asg_payload`` is a single namespaced, non-spec payload extension (never a
repurposed spec-defined field) carrying the numeric fields the aggregate
(``holds/aggregate.py``) needs. It is committed into ``capsule_id`` like
every other field here, so it cannot be tampered with post-seal without
invalidating the digest.

Every numeric field here is integer minor units, MUST-FAIL on float — these
values flow into ``active_exposure_minor``'s ``sum``, which cannot
reproduce a float byte-exactly across implementations.
"""
from __future__ import annotations

from typing import Any

from agent_action_capsule import (
    AssuranceBlock,
    Capsule,
    Chain,
    ConstraintRecord,
    Disposition,
    compute_capsule_id,
)

from .action import Action
from .errors import FLOAT_IN_HOLD_AMOUNT, NON_INTEGER_HOLD_AMOUNT, HoldError

__all__ = [
    "ALLOW",
    "DENY",
    "SUPERSEDES",
    "CONFIRMS",
    "check_integer_amount",
    "build_hold_reserve_capsule",
    "build_hold_release_capsule",
    "build_hold_expire_capsule",
    "build_hold_reconcile_capsule",
    "build_hold_decision_capsule",
]

_SPEC_VERSION = "draft-mih-scitt-agent-action-capsule-02"
_FORMAT_VERSION = "2"

ALLOW = "allow"
DENY = "deny"

# The only registered chain.relation this module uses: "supersedes" for a
# terminal transition that closes/replaces the reserve's open state
# (release, expiry, a successful reconcile).
SUPERSEDES = "supersedes"

# A refused/denied attempt confirms (comments on) the capsule it was
# evaluated against, rather than superseding its open state.
CONFIRMS = "confirms"

_DISPOSITION_BY_OUTCOME = {
    ALLOW: {"decision": "accept", "verdict_class": None},
    DENY: {"decision": "reject", "verdict_class": "blocked"},
}


def check_integer_amount(value: Any, field: str) -> int:
    """Amounts are integer minor units — floats MUST-FAIL, applied at
    record-build time so a bad amount fails loudly here with a named
    reason, not deep inside JCS canonicalization when the capsule is later
    digested."""
    if isinstance(value, bool):
        raise HoldError(NON_INTEGER_HOLD_AMOUNT, f"{field!r} is a bool, not an integer amount")
    if isinstance(value, float):
        raise HoldError(FLOAT_IN_HOLD_AMOUNT, f"{field!r} carries a float ({value!r}); amounts MUST be integer minor units")
    if not isinstance(value, int):
        raise HoldError(NON_INTEGER_HOLD_AMOUNT, f"{field!r} is {type(value).__name__}, not an integer")
    return value


def _hold_action(action: Action, verb: str) -> Action:
    """A fresh ``Action`` carrying this hold record's own verb/action_id,
    otherwise identical to the action this hold is evaluated for."""
    return Action(
        verb=verb,
        operator=action.operator,
        developer=action.developer,
        action_class=action.action_class,
        timestamp=action.resolved_timestamp(),
        amount_minor=action.amount_minor,
        currency=action.currency,
        target=action.target,
    )


def _seal(body: dict) -> dict:
    """Compute ``capsule_id`` over the canonical body (self-attested — no
    local signing key, matching every other capsule this library emits)."""
    capsule_id = compute_capsule_id(body)
    sealed = {"spec_version": body["spec_version"], "format_version": body["format_version"], "capsule_id": capsule_id}
    for k, v in body.items():
        if k not in sealed:
            sealed[k] = v
    return sealed


def _build(
    *,
    hold_action: Action,
    chain: Chain | None,
    asg_payload: dict,
    decision: str = "accept",
    verdict_class: str | None = None,
    constraints: tuple[ConstraintRecord, ...] = (),
) -> dict:
    disposition = Disposition(decision=decision, approver="policy", human_disposed=False, verdict_class=verdict_class)
    capsule_obj = Capsule(
        spec_version=_SPEC_VERSION,
        format_version=_FORMAT_VERSION,
        action_id=hold_action.resolved_action_id(),
        action_type="decide",
        operator=hold_action.operator,
        developer=hold_action.developer,
        timestamp=hold_action.resolved_timestamp(),
        assurance=AssuranceBlock(
            attestation_mode="self_attested",
            effect_mode="not_applicable",
            ledger_mode="chained" if chain is not None else "standalone",
        ),
        disposition=disposition,
        chain=chain,
        constraints=constraints,
    )
    body = capsule_obj.to_dict()
    body["asg_payload"] = asg_payload
    return _seal(body)


def build_hold_reserve_capsule(
    *,
    action: Action,
    reserved_amount_minor: int,
    aggregate_before_minor: int,
    cap_minor: int | None = None,
) -> dict:
    """Reserve-at-seal: cites the aggregate this decision was evaluated
    against (the caller's own pre-reservation exposure) and the reserved
    amount, so a later independent recompute over the same ledger prefix
    can confirm the citation byte-exactly. No ``chain`` — a fresh
    reservation opens a new hold, standalone; release/expiry/reconcile are
    what chain back to *this* capsule's id."""
    check_integer_amount(reserved_amount_minor, "reserved_amount_minor")
    hold_action = _hold_action(action, "hold.reserve")
    asg_payload: dict[str, Any] = {
        "amount_minor": reserved_amount_minor,
        "reserved_amount_minor": reserved_amount_minor,
        "hold_scope": {"action_class": action.action_class, "subject": action.developer},
        "aggregate_before_minor": aggregate_before_minor,
    }
    if cap_minor is not None:
        asg_payload["cap_minor"] = cap_minor
    if action.currency is not None:
        asg_payload["currency"] = action.currency
    if action.target is not None:
        asg_payload["target"] = action.target
    if action.expires_at is not None:
        asg_payload["expires_at"] = action.expires_at
    return _build(hold_action=hold_action, chain=None, asg_payload=asg_payload)


def build_hold_release_capsule(
    *, action: Action, reserve_capsule_id: str, reserved_amount_minor: int, reason: str | None = None,
) -> dict:
    """Voluntary cancellation of a still-active hold. Terminal: the
    reserve's exposure fully unwinds (``amount_minor`` is the negative of
    the reserved amount, so the aggregate nets back to zero for this
    hold)."""
    check_integer_amount(reserved_amount_minor, "reserved_amount_minor")
    hold_action = _hold_action(action, "hold.release")
    chain = Chain(parent_capsule_id=reserve_capsule_id, relation=SUPERSEDES)
    asg_payload: dict[str, Any] = {
        "amount_minor": -reserved_amount_minor,
        "released_amount_minor": reserved_amount_minor,
    }
    if reason is not None:
        asg_payload["reason"] = reason
    return _build(hold_action=hold_action, chain=chain, asg_payload=asg_payload)


def build_hold_expire_capsule(
    *, action: Action, reserve_capsule_id: str, reserved_amount_minor: int, reason: str | None = None,
) -> dict:
    """Expiry: TERMINAL for this hold — after this capsule exists, nothing
    may dispatch citing the original reservation. Same net-to-zero exposure
    unwind as release; the two are distinguished by verb (a caller choosing
    to cancel vs. a TTL/policy elapsing), not by any different
    aggregate-visible effect."""
    check_integer_amount(reserved_amount_minor, "reserved_amount_minor")
    hold_action = _hold_action(action, "hold.expire")
    chain = Chain(parent_capsule_id=reserve_capsule_id, relation=SUPERSEDES)
    asg_payload: dict[str, Any] = {
        "amount_minor": -reserved_amount_minor,
        "expired_amount_minor": reserved_amount_minor,
    }
    if reason is not None:
        asg_payload["reason"] = reason
    return _build(hold_action=hold_action, chain=chain, asg_payload=asg_payload)


def build_hold_reconcile_capsule(
    *,
    action: Action,
    reserve_capsule_id: str,
    execution_capsule_id: str | None,
    reserved_amount_minor: int,
    executed_amount_minor: int,
    tolerance_minor: int,
) -> dict:
    """Planned vs. executed: reserve at planned amount, convert at executed
    amount, the delta sealed as this record — chained to the reserve
    capsule via ``chain`` (the schema's single-parent link) and to the
    execution capsule via the ``asg_payload.execution_capsule_id`` citation
    (a plain payload reference, not a registry relation — the schema has
    only one ``chain`` slot per capsule).

    ``amount_minor`` is the *delta* (``executed - reserved``): summed
    against the reserve's own ``+reserved_amount_minor`` contribution, the
    aggregate nets to exactly ``executed_amount_minor`` once this record
    lands — "executed once reconciled" falls out of the aggregate's own
    delta algebra, not a special case here.

    Only called for an in-tolerance conversion; an over-tolerance
    conversion routes through ``build_hold_decision_capsule`` instead
    (``holds/engine.py``) and this capsule is never built for it — never
    silently adjusts the aggregate.
    """
    check_integer_amount(reserved_amount_minor, "reserved_amount_minor")
    check_integer_amount(executed_amount_minor, "executed_amount_minor")
    check_integer_amount(tolerance_minor, "tolerance_minor")
    delta_minor = executed_amount_minor - reserved_amount_minor
    hold_action = _hold_action(action, "hold.reconcile")
    chain = Chain(parent_capsule_id=reserve_capsule_id, relation=SUPERSEDES)
    asg_payload: dict[str, Any] = {
        "amount_minor": delta_minor,
        "reserved_amount_minor": reserved_amount_minor,
        "executed_amount_minor": executed_amount_minor,
        "delta_minor": delta_minor,
        "tolerance_minor": tolerance_minor,
    }
    if execution_capsule_id is not None:
        asg_payload["execution_capsule_id"] = execution_capsule_id
    return _build(hold_action=hold_action, chain=chain, asg_payload=asg_payload)


def build_hold_decision_capsule(
    *,
    action: Action,
    outcome: str,
    reason_code: str,
    reason: str,
    aggregate_before_minor: int | None = None,
    chain_parent: str | None = None,
    chain_relation: str = CONFIRMS,
) -> dict:
    """A refused attempt (evaluate-and-reserve denied, or a lifecycle call
    against an already-terminal hold): sealed for the audit trail, but
    ``disposition.decision`` is never ``"accept"`` — ``active_exposure_minor``
    never picks it up, regardless of its ``action_id`` verb, matching every
    other refusal in this module."""
    mapping = _DISPOSITION_BY_OUTCOME[outcome]
    hold_action = _hold_action(action, action.verb)
    chain = Chain(parent_capsule_id=chain_parent, relation=chain_relation) if chain_parent is not None else None
    asg_payload: dict[str, Any] = {"reason_code": reason_code, "reason": reason}
    if aggregate_before_minor is not None:
        asg_payload["aggregate_before_minor"] = aggregate_before_minor
    constraint = ConstraintRecord(id=reason_code, result="fail", check_type="policy")
    return _build(
        hold_action=hold_action, chain=chain, asg_payload=asg_payload,
        decision=mapping["decision"], verdict_class=mapping["verdict_class"], constraints=(constraint,),
    )
