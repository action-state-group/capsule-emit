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

from ..ledger import append_to_ledger, read_ledger
from .action import Action
from .aggregate import active_exposure_minor
from .capsules import ALLOW, DENY, build_hold_decision_capsule, build_hold_reserve_capsule
from .errors import CAP_EXCEEDED, SEQUENCER_UNAVAILABLE
from .scope import ScopeLocks

__all__ = ["HoldStatus", "HoldDecision", "HoldEngine"]


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
        engine_available: Callable[[], bool] = lambda: True,
        scope_locks: ScopeLocks | None = None,
    ) -> None:
        self._ledger_path = ledger_path
        self._cap_minor = cap_minor or {}
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
