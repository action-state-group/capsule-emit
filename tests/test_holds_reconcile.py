# SPDX-License-Identifier: Apache-2.0
"""Planned != executed; reconcile as a record; over-tolerance is a limit
event that never silently adjusts the aggregate."""
from __future__ import annotations

from agent_action_capsule import verify

from capsule_emit.holds import Action, HoldEngine, HoldStatus
from capsule_emit.holds.aggregate import active_exposure_minor
from capsule_emit.holds.errors import OVER_TOLERANCE
from capsule_emit.ledger import read_ledger

DEVELOPER = "procurement-agent@v1"
OPERATOR = "acme-research"
TOLERANCE = 50_000


def _action(amount_minor=1_000_000, action_id=None, target=None):
    return Action(
        verb="transfer_funds", operator=OPERATOR, developer=DEVELOPER, action_class="money.transfer",
        amount_minor=amount_minor, currency="EUR", action_id=action_id, target=target,
    )


def _engine(ledger_path, **kw):
    return HoldEngine(
        ledger_path=str(ledger_path), cap_minor={"money.transfer": 100_000_000},
        tolerance_minor={"money.transfer": TOLERANCE}, **kw,
    )


def _aggregate(ledger_path) -> int:
    return active_exposure_minor(read_ledger(str(ledger_path)), DEVELOPER)


# -- reconcile carries planned/executed/delta, chains to both ---------------


def test_reconcile_carries_planned_executed_delta_and_chains_to_both(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, target="acct-1"))
    reserve_id = reserve.capsule["capsule_id"]

    execution_capsule_id = "e" * 64  # opaque foreign-system reference
    decision = engine.reconcile(
        reserve_id, action_class="money.transfer", executed_amount_minor=1_020_000,
        execution_capsule_id=execution_capsule_id,
    )
    assert decision.outcome == "allow"
    payload = decision.capsule["asg_payload"]
    assert payload["reserved_amount_minor"] == 1_000_000
    assert payload["executed_amount_minor"] == 1_020_000
    assert payload["delta_minor"] == 20_000
    assert payload["tolerance_minor"] == TOLERANCE
    assert payload["execution_capsule_id"] == execution_capsule_id
    assert all(
        isinstance(payload[k], int) for k in ("reserved_amount_minor", "executed_amount_minor", "delta_minor", "tolerance_minor")
    )

    # chained to the reserve capsule via `chain` (the schema's one parent slot)
    assert decision.capsule["chain"]["parent_capsule_id"] == reserve_id
    # and to the execution capsule via the payload citation
    assert decision.capsule["asg_payload"]["execution_capsule_id"] == execution_capsule_id

    assert verify(decision.capsule).ok


# -- over-tolerance is a limit event, never adjusts the aggregate -----------


def test_over_tolerance_denies_never_adjusts_aggregate(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, target="acct-1"))
    reserve_id = reserve.capsule["capsule_id"]

    over = engine.reconcile(reserve_id, action_class="money.transfer", executed_amount_minor=1_200_000)
    assert over.outcome == "deny"
    assert over.reason_code == OVER_TOLERANCE

    # NEVER a *successful* reconcile for the over-tolerance attempt
    assert over.capsule["disposition"]["decision"] != "accept"
    assert "delta_minor" not in over.capsule["asg_payload"]  # not the hold.reconcile record shape at all
    assert over.capsule["chain"]["parent_capsule_id"] == reserve_id

    # aggregate is NOT silently adjusted to the (rejected) executed amount --
    # the hold is still just the original reservation, pending resolution.
    status, _terminal = engine.hold_status(reserve_id)
    assert status == HoldStatus.ACTIVE
    assert _aggregate(ledger_path) == 1_000_000


def test_over_tolerance_mutant_neutralizing_tolerance_flips_to_full_reconcile(tmp_path):
    """Mutant test: neutralize the tolerance value (the condition the
    over-tolerance check tests against) and confirm the same over-limit
    conversion that was denied now silently reconciles instead -- proving
    the tolerance check is load-bearing."""
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, target="acct-1"))
    reserve_id = reserve.capsule["capsule_id"]
    over = engine.reconcile(reserve_id, action_class="money.transfer", executed_amount_minor=1_200_000)
    assert over.outcome == "deny"

    reserve2 = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, target="acct-2"))
    reserve2_id = reserve2.capsule["capsule_id"]
    # mutant: the tolerance for this class is effectively removed (set so
    # large nothing can ever exceed it).
    engine._tolerance_minor["money.transfer"] = 10**12
    mutant = engine.reconcile(reserve2_id, action_class="money.transfer", executed_amount_minor=1_200_000)
    assert mutant.outcome == "allow", "mutant did not flip the outcome -- the tolerance check is not load-bearing"
    assert (mutant.capsule.get("action_id") or "").startswith("hold.reconcile/")


# -- reconcile after expiry: denied, citing re-evaluation --------------------


def test_reconcile_after_expiry_denied_citing_fresh_evaluation(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, target="acct-1"))
    reserve_id = reserve.capsule["capsule_id"]
    engine.expire(reserve_id, reason="ttl elapsed")

    from capsule_emit.holds.errors import RECONCILE_AFTER_EXPIRY

    attempt = engine.reconcile(reserve_id, action_class="money.transfer", executed_amount_minor=1_000_000)
    assert attempt.outcome == "deny"
    assert attempt.reason_code == RECONCILE_AFTER_EXPIRY
    assert "evaluate_and_reserve" in attempt.reason


# -- fold semantics: planned while held, executed once reconciled ------------


def test_fold_semantics_across_partial_over_within_and_over_beyond_tolerance(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)

    # -- partial fill: executed < reserved -------------------------------
    r1 = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, action_id="t/1", target="acct-1"))
    assert r1.outcome == "allow"
    assert _aggregate(ledger_path) == 1_000_000  # planned, while held
    c1 = engine.reconcile(r1.capsule["capsule_id"], action_class="money.transfer", executed_amount_minor=600_000)
    assert c1.outcome == "allow"
    assert _aggregate(ledger_path) == 600_000  # executed, once reconciled

    # -- over-fill within tolerance ---------------------------------------
    r2 = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, action_id="t/2", target="acct-2"))
    assert r2.outcome == "allow"
    assert _aggregate(ledger_path) == 600_000 + 1_000_000
    c2 = engine.reconcile(r2.capsule["capsule_id"], action_class="money.transfer", executed_amount_minor=1_000_000 + TOLERANCE)
    assert c2.outcome == "allow"
    assert _aggregate(ledger_path) == 600_000 + (1_000_000 + TOLERANCE)

    # -- over-fill beyond tolerance: aggregate is untouched ---------------
    r3 = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, action_id="t/3", target="acct-3"))
    assert r3.outcome == "allow"
    before = _aggregate(ledger_path)
    assert before == 600_000 + (1_000_000 + TOLERANCE) + 1_000_000
    c3 = engine.reconcile(
        r3.capsule["capsule_id"], action_class="money.transfer", executed_amount_minor=1_000_000 + TOLERANCE + 1,
    )
    assert c3.outcome == "deny"
    assert _aggregate(ledger_path) == before  # unchanged: the over-tolerance attempt never adjusts it

    # -- replay reproduces the aggregate at every stage --------------------
    records = read_ledger(str(ledger_path))
    final = active_exposure_minor(records, DEVELOPER)
    assert final == before == _aggregate(ledger_path)


# -- reconcile itself is a single critical section per scope -----------------


def test_reconcile_is_terminal_further_lifecycle_calls_deny(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000_000, target="acct-1"))
    reserve_id = reserve.capsule["capsule_id"]
    reconciled = engine.reconcile(reserve_id, action_class="money.transfer", executed_amount_minor=1_000_000)
    assert reconciled.outcome == "allow"
    assert reconciled.hold_status == HoldStatus.RECONCILED

    again = engine.reconcile(reserve_id, action_class="money.transfer", executed_amount_minor=1_000_000)
    assert again.outcome == "deny"

    release_attempt = engine.release(reserve_id)
    assert release_attempt.outcome == "deny"
