# SPDX-License-Identifier: Apache-2.0
"""Atomic evaluate-and-reserve: reservation counts toward the aggregate at
seal time, and evaluate-and-reserve is a single critical section per scope
so concurrent callers can never jointly over-reserve it."""
from __future__ import annotations

import threading
import time

import pytest

from capsule_emit.holds import Action, HoldEngine, HoldStatus
from capsule_emit.holds.aggregate import active_exposure_minor
from capsule_emit.holds.scope import ScopeLocks
from capsule_emit.ledger import read_ledger
from capsule_emit.verification import verify_capsule as verify

DEVELOPER = "procurement-agent@v1"
OPERATOR = "acme-research"


def _action(developer=DEVELOPER, amount_minor=100, action_id=None, target=None):
    return Action(
        verb="transfer_funds", operator=OPERATOR, developer=developer, action_class="money.transfer",
        amount_minor=amount_minor, currency="EUR", action_id=action_id, target=target,
    )


# -- reserve-at-seal cites the aggregate + reserved amount -------------------


def test_reserve_cites_aggregate_and_reserved_amount(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = HoldEngine(ledger_path=str(ledger_path), cap_minor={"money.transfer": 1_000_000})
    decision = engine.evaluate_and_reserve(_action(amount_minor=500))
    assert decision.outcome == "allow"
    capsule = decision.capsule
    payload = capsule["asg_payload"]

    assert payload["reserved_amount_minor"] == 500
    assert isinstance(payload["reserved_amount_minor"], int)
    assert payload["aggregate_before_minor"] == 0
    assert decision.aggregate_minor == 500

    # a real, independently verifiable capsule -- no special-cased path
    result = verify(capsule)
    assert result.ok, result.findings


# -- expires_at is recorded on the reserve, never enforced by the engine -----


def test_reserve_records_expires_at_when_the_caller_supplies_it(tmp_path):
    """``expires_at`` is a plain audit field: the caller states its intended
    deadline, the engine records it on the reserve capsule verbatim, and
    never reads it back -- expiry stays caller-invoked (``engine.expire()``),
    so a later auditor can tell whether an expiry was timely or arbitrary."""
    ledger_path = tmp_path / "ledger.jsonl"
    engine = HoldEngine(ledger_path=str(ledger_path), cap_minor={"money.transfer": 1_000_000})
    action = Action(
        verb="transfer_funds", operator=OPERATOR, developer=DEVELOPER, action_class="money.transfer",
        amount_minor=500, currency="EUR", expires_at="2026-08-11T12:00:00Z",
    )
    decision = engine.evaluate_and_reserve(action)
    assert decision.outcome == "allow"
    assert decision.capsule["asg_payload"]["expires_at"] == "2026-08-11T12:00:00Z"
    assert verify(decision.capsule).ok


def test_reserve_omits_expires_at_when_the_caller_does_not_supply_it(tmp_path):
    """No default deadline is invented -- absence of ``expires_at`` means
    the caller didn't state one, not that one was silently computed."""
    ledger_path = tmp_path / "ledger.jsonl"
    engine = HoldEngine(ledger_path=str(ledger_path), cap_minor={"money.transfer": 1_000_000})
    decision = engine.evaluate_and_reserve(_action(amount_minor=500))
    assert decision.outcome == "allow"
    assert "expires_at" not in decision.capsule["asg_payload"]


# -- N concurrent calls against a cap admitting K -----------------------------


def _run_concurrency_round(n: int, k: int, unit: int, ledger_path) -> tuple[int, int]:
    """Fire ``n`` concurrent evaluate_and_reserve calls (same scope) against
    a cap sized to admit exactly ``k`` of them. Returns (allowed, denied)."""
    engine = HoldEngine(ledger_path=str(ledger_path), cap_minor={"money.transfer": unit * k})

    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()  # maximize interleaving: every thread starts together
        decision = engine.evaluate_and_reserve(_action(amount_minor=unit, action_id=f"transfer_funds/{i}"))
        with lock:
            outcomes.append(decision.outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = outcomes.count("allow")
    denied = n - allowed

    # zero interleavings over-reserve: independently recompute the aggregate
    # over the resulting ledger and confirm it never exceeds the cap.
    records = read_ledger(str(ledger_path))
    agg = active_exposure_minor(records, DEVELOPER)
    assert agg <= unit * k
    reserve_count = sum(1 for r in records if (r.get("action_id") or "").startswith("hold.reserve/"))
    assert reserve_count == allowed

    return allowed, denied


@pytest.mark.parametrize("round_idx", range(100))
def test_concurrent_evaluate_and_reserve_admits_exactly_k(round_idx, tmp_path):
    """Property/soak: repeat the same N-vs-K race many times, real threads,
    no mocks. K of N unit reservations must be admitted every time."""
    n, k, unit = 12, 4, 100
    ledger_path = tmp_path / f"ledger-{round_idx}.jsonl"
    allowed, denied = _run_concurrency_round(n, k, unit, ledger_path)
    assert allowed == k, f"round {round_idx}: expected exactly {k} admitted, got {allowed}"
    assert denied == n - k


# -- a pending hold blocks a second action before any resolution -------------


def test_pending_hold_blocks_action_that_would_exceed_cap(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = HoldEngine(ledger_path=str(ledger_path), cap_minor={"money.transfer": 10_000_000})

    first = engine.evaluate_and_reserve(
        _action(amount_minor=9_500_000, action_id="transfer_funds/1", target="acct-1")
    )
    assert first.outcome == "allow"
    assert first.hold_status == HoldStatus.ACTIVE

    # a distinct target so this is isolated to the caps/active-holds
    # mechanism, purely because of the PENDING hold, before any
    # conversion/approval has happened.
    second = engine.evaluate_and_reserve(
        _action(amount_minor=1_000_000, action_id="transfer_funds/2", target="acct-2")
    )
    assert second.outcome == "deny"


# -- serialization point unavailable -> fail closed ---------------------------


def test_engine_unavailable_fails_closed_no_reserve(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = HoldEngine(
        ledger_path=str(ledger_path), cap_minor={"money.transfer": 1_000_000}, engine_available=lambda: False,
    )
    decision = engine.evaluate_and_reserve(_action(amount_minor=100))
    assert decision.outcome == "deny"
    records = read_ledger(str(ledger_path))
    assert not any((r.get("action_id") or "").startswith("hold.reserve/") for r in records)


def test_engine_unavailable_mutant_disabling_the_check_flips_to_allow(tmp_path):
    """Mutant test: the fail-closed branch in ``evaluate_and_reserve`` is
    the condition under test. Disabling it (as a real code mutation would)
    must flip a would-be-denied reservation to allowed -- proving the check
    is load-bearing, not a decorative no-op."""
    real = HoldEngine(
        ledger_path=str(tmp_path / "real.jsonl"), cap_minor={"money.transfer": 1_000_000},
        engine_available=lambda: False,
    )
    real_decision = real.evaluate_and_reserve(_action(amount_minor=100))
    assert real_decision.outcome == "deny"

    # the mutant: the engine reports available even though it is not --
    # i.e. the fail-closed condition never fires.
    mutant = HoldEngine(
        ledger_path=str(tmp_path / "mutant.jsonl"), cap_minor={"money.transfer": 1_000_000},
        engine_available=lambda: True,  # mutated: the unreachable sequencer is masked
    )
    mutant_decision = mutant.evaluate_and_reserve(_action(amount_minor=100))
    assert mutant_decision.outcome == "allow", "mutant did not flip the outcome -- the fail-closed check is not load-bearing"


# -- the scope lock (critical section) is load-bearing, not decorative ------


class _UnlockedScopeLocks(ScopeLocks):
    """Mutant of the real ``ScopeLocks``: every ``get()`` returns a brand
    new, never-shared lock -- so no two callers for the same scope are ever
    actually serialized against each other. This exercises the REAL
    ``HoldEngine`` code path end to end; only the shared-lock property that
    makes the critical section a critical section is removed."""

    def get(self, scope):  # noqa: ARG002
        return threading.Lock()


def test_concurrent_reserve_mutant_no_shared_scope_lock_over_reserves(tmp_path):
    """Mutant test for the critical section itself (not just the fail-closed
    availability check above): defeat scope-lock sharing and confirm the
    same N-vs-K race that is exact under the real lock now over-reserves.

    A race is intrinsically timing-dependent, so this injects a small,
    deterministic delay between "read the current aggregate" and "append
    the reservation" -- exactly the window the scope lock exists to close.
    With the real (shared) lock this delay changes nothing (callers are
    already serialized, so nobody's read overlaps anybody else's write).
    With the lock defeated, every thread's read happens before any thread's
    write lands, so the mutant reliably over-reserves on every run -- not a
    flaky one-off.
    """
    import capsule_emit.holds.engine as holds_engine_module

    real_aggregate = holds_engine_module.active_exposure_minor

    def _delayed_aggregate(*args, **kwargs):
        result = real_aggregate(*args, **kwargs)
        time.sleep(0.01)
        return result

    holds_engine_module.active_exposure_minor = _delayed_aggregate
    try:
        n, k, unit = 10, 4, 100
        ledger_path = tmp_path / "mutant-race.jsonl"
        engine = HoldEngine(
            ledger_path=str(ledger_path), cap_minor={"money.transfer": unit * k},
            scope_locks=_UnlockedScopeLocks(),
        )

        outcomes: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def worker(i: int) -> None:
            barrier.wait()
            decision = engine.evaluate_and_reserve(_action(amount_minor=unit, action_id=f"transfer_funds/{i}"))
            with lock:
                outcomes.append(decision.outcome)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed = outcomes.count("allow")
        assert allowed > k, (
            "mutant (no shared scope lock) did not over-reserve -- "
            "the scope lock is not load-bearing"
        )
    finally:
        holds_engine_module.active_exposure_minor = real_aggregate


# -- replay reproduces the cited aggregate byte-exactly -----------------------


def test_replay_reproduces_cited_aggregate_byte_exactly(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = HoldEngine(ledger_path=str(ledger_path), cap_minor={"money.transfer": 10_000_000})

    engine.evaluate_and_reserve(_action(amount_minor=300_000, action_id="transfer_funds/a"))
    engine.evaluate_and_reserve(_action(amount_minor=150_000, action_id="transfer_funds/b", developer="other-dev"))
    d3 = engine.evaluate_and_reserve(_action(amount_minor=200_000, action_id="transfer_funds/c"))
    assert d3.outcome == "allow"

    cited_aggregate_before = d3.capsule["asg_payload"]["aggregate_before_minor"]

    # independent verifier: fresh evaluation over the ledger, including the
    # hold capsules, must reproduce the same aggregate byte-exactly.
    records = read_ledger(str(ledger_path))
    full_agg = active_exposure_minor(records, DEVELOPER)
    assert full_agg == cited_aggregate_before + 200_000  # includes d3's own reservation

    # the cited aggregate is BEFORE this action's own reservation lands;
    # the independent recompute over the same prefix (excluding d3 itself)
    # must match exactly.
    prefix_records = records[:-1]
    prefix_agg = active_exposure_minor(prefix_records, DEVELOPER)
    assert prefix_agg == cited_aggregate_before
