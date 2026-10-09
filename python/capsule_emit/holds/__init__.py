# SPDX-License-Identifier: Apache-2.0
"""Reservation-as-capsule holds: authorization-hold semantics for a budget
scope, layered on top of ``capsule_emit``'s own emit/ledger primitives.

    from capsule_emit.holds import Action, HoldEngine

    engine = HoldEngine(ledger_path="ledger.jsonl", cap_minor={"money.transfer": 10_000_000})
    decision = engine.evaluate_and_reserve(
        Action(verb="transfer_funds", operator="acme-co", developer="po-agent@v1",
               action_class="money.transfer", amount_minor=500_000)
    )
    if decision.outcome == "allow":
        ...  # dispatch, then later engine.reconcile(...) with the executed amount

Planned spend counts toward the cap *at seal time*: two concurrent requests
for the same scope never both see the same stale aggregate and both pass —
``evaluate_and_reserve`` is a single critical section per
``(action_class, developer)`` scope (``holds/scope.py``).
"""
from __future__ import annotations

from .action import Action
from .engine import HoldDecision, HoldEngine, HoldStatus
from .scope import ScopeLocks

__all__ = ["Action", "HoldDecision", "HoldEngine", "HoldStatus", "ScopeLocks"]
