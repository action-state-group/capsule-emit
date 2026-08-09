# SPDX-License-Identifier: Apache-2.0
"""``HoldEngine``: atomic evaluate-and-reserve.

Local (in-process, one ledger file) single-writer semantics only — a
distributed sequencer across processes/nodes is a deployment-specific
concern, not this module's. See ``holds/scope.py`` for why a per-scope lock
is what actually closes the double-spend race a naive "read aggregate, then
decide, then append" sequence leaves open.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from agent_action_capsule import verify as verify_capsule

from ..ledger import append_to_ledger, read_ledger
from .action import Action
from .aggregate import active_exposure_minor
from .capsules import (
    ALLOW,
    CONFIRMS,
    DENY,
    SUPERSEDES,
    build_hold_decision_capsule,
    build_hold_expire_capsule,
    build_hold_reconcile_capsule,
    build_hold_release_capsule,
    build_hold_reserve_capsule,
    check_integer_amount,
)
from .errors import (
    CAP_EXCEEDED,
    HOLD_ALREADY_TERMINAL,
    HOLD_NOT_FOUND,
    HOLD_STATUS_AMBIGUOUS,
    OVER_TOLERANCE,
    RECONCILE_AFTER_EXPIRY,
    SEQUENCER_UNAVAILABLE,
)
from .scope import ScopeLocks

__all__ = ["HoldStatus", "HoldDecision", "HoldEngine"]


def _verb(capsule: dict) -> str:
    action_id = capsule.get("action_id") or ""
    return action_id.split("/", 1)[0] if action_id else ""


class HoldStatus(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    RECONCILED = "reconciled"
    # A hold's status could not be determined (missing record, a chained
    # terminal record that fails independent verification). Every caller
    # treats AMBIGUOUS as terminal/deny for consequential classes — never
    # as "probably still active".
    AMBIGUOUS = "ambiguous"


# Verbs a chained `supersedes` record over a reserve capsule may carry, and
# the terminal status each one closes the hold into.
_TERMINAL_VERBS: dict[str, HoldStatus] = {
    "hold.release": HoldStatus.RELEASED,
    "hold.expire": HoldStatus.EXPIRED,
    "hold.reconcile": HoldStatus.RECONCILED,
}


@dataclass(frozen=True)
class HoldDecision:
    """A hold-lifecycle operation's result.

    ``reason_code`` is the machine-readable counterpart to ``reason`` —
    lives here, on the Python-side decision object, rather than on the
    sealed capsule: the capsule only carries the ``asg_payload.reason_code``
    field for a refused attempt, never free prose reasoning.
    """

    outcome: str  # "allow" | "deny"
    capsule: dict | None
    reason: str
    reason_code: str | None = None
    hold_status: HoldStatus | None = None
    aggregate_minor: int | None = None


class HoldEngine:
    def __init__(
        self,
        *,
        ledger_path: str | os.PathLike,
        cap_minor: dict[str, int] | None = None,
        tolerance_minor: dict[str, int] | None = None,
        engine_available: Callable[[], bool] = lambda: True,
        scope_locks: ScopeLocks | None = None,
    ) -> None:
        self._ledger_path = ledger_path
        self._cap_minor = cap_minor or {}
        self._tolerance_minor = tolerance_minor or {}
        self._engine_available = engine_available
        self._scope_locks = scope_locks or ScopeLocks()

    # -- atomic evaluate-and-reserve --------------------------------------

    def evaluate_and_reserve(self, action: Action) -> HoldDecision:
        """Fail-closed preflight: "a gate that cannot reach the scope's
        serialization point must deny, never evaluate against an
        unserialized read." The reservation decision itself runs under this
        scope's lock (``scope.py``): the read (current aggregate), the
        decide (cap check), and the append (reservation) are one atomic
        critical section, so N concurrent calls for the same scope can
        never jointly over-reserve it."""
        if not self._engine_available():
            return self._deny(
                action, reason_code=SEQUENCER_UNAVAILABLE, aggregate_before_minor=None,
                reason="scope serialization point is unreachable; fail closed (no reserve -> no pass)",
            )

        scope = (action.action_class or "", action.developer)
        with self._scope_locks.get(scope):
            return self._evaluate_and_reserve_locked(action)

    def _evaluate_and_reserve_locked(self, action: Action) -> HoldDecision:
        records = read_ledger(self._ledger_path)
        current = active_exposure_minor(records, action.developer)
        requested = action.amount_minor if action.amount_minor is not None else 0
        cap = self._cap_minor.get(action.action_class)

        if cap is not None and current + requested > cap:
            reason = f"aggregate {current} + requested {requested} exceeds cap {cap} for {action.action_class!r}"
            return self._deny(action, reason_code=CAP_EXCEEDED, reason=reason, aggregate_before_minor=current)

        capsule = build_hold_reserve_capsule(
            action=action, reserved_amount_minor=requested, aggregate_before_minor=current, cap_minor=cap,
        )
        append_to_ledger(capsule, self._ledger_path)
        return HoldDecision(
            outcome=ALLOW, capsule=capsule, reason="reserved", hold_status=HoldStatus.ACTIVE,
            aggregate_minor=current + requested,
        )

    def _deny(self, action: Action, *, reason_code: str, reason: str, aggregate_before_minor: int | None) -> HoldDecision:
        """Recordable fail-closed case: a plain DENY decision capsule,
        never a hold record (nothing was ever reserved)."""
        capsule = build_hold_decision_capsule(
            action=action, outcome=DENY, reason_code=reason_code, reason=reason,
            aggregate_before_minor=aggregate_before_minor,
        )
        append_to_ledger(capsule, self._ledger_path)
        return HoldDecision(outcome=DENY, capsule=capsule, reason=reason, reason_code=reason_code)

    # -- hold status (earliest chained terminal record wins) -------------

    def hold_status(self, reserve_capsule_id: str) -> tuple[HoldStatus, dict | None]:
        """(status, terminal_capsule_or_None). AMBIGUOUS if the reserve
        record itself is missing/fails verification, or a chained terminal
        record fails independent verification (corruption) — never
        reported as ACTIVE in that case. The earliest chained ``supersedes``
        record in ledger order is authoritative, mirroring the spec's own
        concurrent-supersedes precedent.

        Always a fresh, uncached ledger read: this is also what makes a
        resume-time recheck (``approval.py``'s ``seal_approval``) meaningful
        — a hold that expired *after* it was reserved but *before* a slow
        approval arrives is reflected here the moment the expiry capsule
        lands, not on some stale cached view.
        """
        records = read_ledger(self._ledger_path)
        by_id = {r["capsule_id"]: r for r in records if r.get("capsule_id")}

        reserve_record = by_id.get(reserve_capsule_id)
        if reserve_record is None:
            return HoldStatus.AMBIGUOUS, None
        if not verify_capsule(reserve_record).ok:
            return HoldStatus.AMBIGUOUS, None

        for record in records:  # ledger order -> earliest wins
            chain = record.get("chain") or {}
            if chain.get("parent_capsule_id") != reserve_capsule_id or chain.get("relation") != SUPERSEDES:
                continue
            verb = _verb(record)
            if verb not in _TERMINAL_VERBS:
                continue
            if not verify_capsule(record).ok:
                return HoldStatus.AMBIGUOUS, record
            return _TERMINAL_VERBS[verb], record

        return HoldStatus.ACTIVE, None

    # -- release / expire --------------------------------------------------

    def release(self, reserve_capsule_id: str, *, reason: str | None = None) -> HoldDecision:
        return self._close(reserve_capsule_id, verb="release", reason=reason)

    def expire(self, reserve_capsule_id: str, *, reason: str | None = None) -> HoldDecision:
        """TERMINAL for this hold — after this call succeeds, nothing may
        dispatch citing the original reservation. Enforced by
        ``hold_status``/``_deny_terminal`` on every later release/expire
        attempt against the same reserve capsule, and by the caller's own
        resume-time ``hold_status`` recheck before dispatch (``approval.py``)."""
        return self._close(reserve_capsule_id, verb="expire", reason=reason)

    def _close(self, reserve_capsule_id: str, *, verb: str, reason: str | None) -> HoldDecision:
        records = read_ledger(self._ledger_path)
        by_id = {r["capsule_id"]: r for r in records if r.get("capsule_id")}
        reserve_record = by_id.get(reserve_capsule_id)
        if reserve_record is None:
            return HoldDecision(
                outcome=DENY, capsule=None, reason_code=HOLD_NOT_FOUND,
                reason=f"{HOLD_NOT_FOUND}: hold {reserve_capsule_id[:16]}… not found", hold_status=None,
            )

        payload = reserve_record.get("asg_payload") or {}
        subject = reserve_record.get("developer", "")
        action_class = (payload.get("hold_scope") or {}).get("action_class")
        scope = (action_class or "", subject)
        with self._scope_locks.get(scope):
            status, terminal_record = self.hold_status(reserve_capsule_id)
            if status != HoldStatus.ACTIVE:
                return self._deny_terminal(reserve_record, status, terminal_record, verb=verb)

            reserved_amount = payload.get("reserved_amount_minor", 0)
            attempt_action = _attempt_action(reserve_record, verb=verb, action_class=action_class)
            builder = build_hold_release_capsule if verb == "release" else build_hold_expire_capsule
            capsule = builder(
                action=attempt_action, reserve_capsule_id=reserve_capsule_id,
                reserved_amount_minor=reserved_amount, reason=reason,
            )
            append_to_ledger(capsule, self._ledger_path)
            new_status = HoldStatus.RELEASED if verb == "release" else HoldStatus.EXPIRED
            return HoldDecision(outcome=ALLOW, capsule=capsule, reason=f"hold {verb}d", hold_status=new_status)

    def _deny_terminal(
        self, reserve_record: dict, status: HoldStatus, terminal_record: dict | None, *, verb: str,
    ) -> HoldDecision:
        reserve_capsule_id = reserve_record["capsule_id"]
        if status == HoldStatus.AMBIGUOUS:
            reason_code = HOLD_STATUS_AMBIGUOUS
            reason_text = (
                f"hold {reserve_capsule_id[:16]}… status could not be determined "
                "(missing or unverifiable record); failing closed as terminal for this consequential class"
            )
        elif status == HoldStatus.EXPIRED and verb == "reconcile":
            reason_code = RECONCILE_AFTER_EXPIRY
            reason_text = (
                f"hold {reserve_capsule_id[:16]}… already expired; a late approval is authentication, "
                "not authorization -- resume requires a fresh evaluate_and_reserve, not resumption/"
                "reconciliation of the expired hold"
            )
        else:
            reason_code = HOLD_ALREADY_TERMINAL
            reason_text = f"hold {reserve_capsule_id[:16]}… is already {status.value}; cannot {verb} it again"

        attempt_action = _attempt_action(reserve_record, verb=verb)
        chain_parent = (terminal_record or {}).get("capsule_id") or reserve_capsule_id
        capsule = build_hold_decision_capsule(
            action=attempt_action, outcome=DENY, reason_code=reason_code, reason=reason_text,
            chain_parent=chain_parent, chain_relation=CONFIRMS,
        )
        append_to_ledger(capsule, self._ledger_path)
        return HoldDecision(outcome=DENY, capsule=capsule, reason=reason_text, reason_code=reason_code, hold_status=status)

    # -- reconcile ----------------------------------------------------------

    def reconcile(
        self,
        reserve_capsule_id: str,
        *,
        action_class: str | None,
        executed_amount_minor: int,
        execution_capsule_id: str | None = None,
    ) -> HoldDecision:
        """Planned vs. executed: reserve at planned, convert at executed.
        In-tolerance conversions append a ``hold.reconcile`` capsule — the
        aggregate (``holds/aggregate.py``) then reads
        ``executed_amount_minor`` for this hold once it lands, by delta
        algebra alone. Over-tolerance conversions NEVER build that record —
        they route through a plain DENY decision capsule instead, a limit
        event: fail-closed, never a silent top-up of the aggregate."""
        check_integer_amount(executed_amount_minor, "executed_amount_minor")
        records = read_ledger(self._ledger_path)
        by_id = {r["capsule_id"]: r for r in records if r.get("capsule_id")}
        reserve_record = by_id.get(reserve_capsule_id)
        if reserve_record is None:
            return HoldDecision(
                outcome=DENY, capsule=None, reason_code=HOLD_NOT_FOUND,
                reason=f"{HOLD_NOT_FOUND}: hold {reserve_capsule_id[:16]}… not found", hold_status=None,
            )

        payload = reserve_record.get("asg_payload") or {}
        subject = reserve_record.get("developer", "")
        scope_action_class = (payload.get("hold_scope") or {}).get("action_class")
        scope = (scope_action_class or "", subject)
        with self._scope_locks.get(scope):
            status, terminal_record = self.hold_status(reserve_capsule_id)
            if status != HoldStatus.ACTIVE:
                return self._deny_terminal(reserve_record, status, terminal_record, verb="reconcile")

            reserved_amount = payload.get("reserved_amount_minor", 0)
            delta = executed_amount_minor - reserved_amount
            tolerance = self._tolerance_minor.get(action_class, 0)
            attempt_action = _attempt_action(reserve_record, verb="reconcile", action_class=action_class)

            if delta <= tolerance:
                capsule = build_hold_reconcile_capsule(
                    action=attempt_action, reserve_capsule_id=reserve_capsule_id,
                    execution_capsule_id=execution_capsule_id, reserved_amount_minor=reserved_amount,
                    executed_amount_minor=executed_amount_minor, tolerance_minor=tolerance,
                )
                append_to_ledger(capsule, self._ledger_path)
                return HoldDecision(
                    outcome=ALLOW, capsule=capsule, reason="reconciled", hold_status=HoldStatus.RECONCILED,
                )

            reason = (
                f"executed {executed_amount_minor} exceeds reserved {reserved_amount} by {delta} "
                f"(minor units), beyond tolerance {tolerance}"
            )
            capsule = build_hold_decision_capsule(
                action=attempt_action, outcome=DENY, reason_code=OVER_TOLERANCE, reason=reason,
                chain_parent=reserve_capsule_id, chain_relation=CONFIRMS,
            )
            append_to_ledger(capsule, self._ledger_path)
            return HoldDecision(
                outcome=DENY, capsule=capsule,
                reason="over-tolerance conversion; routed as a limit event, aggregate not silently adjusted",
                reason_code=OVER_TOLERANCE, hold_status=HoldStatus.ACTIVE,
            )


def _attempt_action(reserve_capsule: dict, *, verb: str, action_class: str | None = None) -> Action:
    """A minimal ``Action`` representing an attempt (release/expire/a
    denied-terminal retry) against an existing hold — carries the reserve
    capsule's own operator/developer/currency/target context, not the
    original reservation's verb."""
    payload = reserve_capsule.get("asg_payload") or {}
    scope = payload.get("hold_scope") or {}
    return Action(
        verb=verb if verb.startswith("hold.") else f"hold.{verb}",
        operator=reserve_capsule.get("operator", ""),
        developer=reserve_capsule.get("developer", ""),
        action_class=action_class if action_class is not None else scope.get("action_class"),
        currency=payload.get("currency"),
        target=payload.get("target"),
    )
