# SPDX-License-Identifier: Apache-2.0
"""Cross-class scope granularity: the lock is per ``(action_class,
developer)`` (``holds/scope.py``), the cap is per ``action_class``, and the
aggregate (``holds/aggregate.py``) must agree at that same granularity or a
concurrent caller in a *different* class reads straight through the gap
(Jody's 2026-08-11 PR #54 review, blocking finding).

Jody's concrete repro: cap 100 on classes A and B, concurrent 60-into-A and
60-into-B for the same developer both see aggregate 0 (dev-wide, unscoped)
and both pass -- final dev-wide exposure 120, even though each class's own
cap (100) was never itself exceeded. The fix (DECISION 2026-08-11, Option
A) scopes the aggregate to ``(developer, action_class)`` so class A and
class B become genuinely independent budgets -- which means two *fresh*
60-unit requests against two untouched 100-caps simply both allow, in any
order or interleaving, and that alone can't distinguish the fix from the
bug (a naive re-run of Jody's exact numbers passes either way, since the
old code's dev-wide read and the new code's class-scoped read both start
from 0).

To make the fix load-bearing and observable, this test gives class A an
existing 50-unit baseline exposure *before* the race (the same shape of
contamination Jody's finding describes, just made visible): the correct,
class-scoped answer is deterministic and order-independent -- A's own 60
request is denied (50 + 60 > 100, on A's own exposure alone) and B's 60
request is allowed (0 + 60 <= 100, B's own exposure, untouched by A's
baseline) -- while the pre-fix dev-wide read would let A's baseline leak
into B's decision and wrongly deny both.
"""
from __future__ import annotations

import threading

import pytest

from capsule_emit.holds import Action, HoldEngine
from capsule_emit.holds.aggregate import active_exposure_minor
from capsule_emit.ledger import read_ledger

DEVELOPER = "procurement-agent@v1"
OPERATOR = "acme-research"
CLASS_A = "goods.procure.class-a"
CLASS_B = "goods.procure.class-b"
CAP = 100
BASELINE_A = 50
REQUEST = 60


def _action(action_class, amount_minor, action_id):
    return Action(
        verb="transfer_funds", operator=OPERATOR, developer=DEVELOPER, action_class=action_class,
        amount_minor=amount_minor, currency="EUR", action_id=action_id,
    )


def _engine(ledger_path):
    return HoldEngine(ledger_path=str(ledger_path), cap_minor={CLASS_A: CAP, CLASS_B: CAP})


def _seed_baseline(engine):
    """A pre-existing, already-accepted reservation in class A only --
    established before the race, not part of it."""
    baseline = engine.evaluate_and_reserve(_action(CLASS_A, BASELINE_A, "seed/a"))
    assert baseline.outcome == "allow"


# -- concurrent cross-class race matches the single correct sequential answer


def _run_cross_class_race(ledger_path) -> dict[str, str]:
    """Fire the A and B requests concurrently, barrier-started to maximize
    interleaving (same pattern as ``test_holds_reserve.py``'s N-vs-K race).
    Returns {"A": outcome, "B": outcome}."""
    engine = _engine(ledger_path)
    _seed_baseline(engine)

    outcomes: dict[str, str] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(label: str, action_class: str) -> None:
        barrier.wait()  # maximize interleaving: both threads start together
        decision = engine.evaluate_and_reserve(_action(action_class, REQUEST, f"race/{label}"))
        with lock:
            outcomes[label] = decision.outcome

    threads = [
        threading.Thread(target=worker, args=("A", CLASS_A)),
        threading.Thread(target=worker, args=("B", CLASS_B)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def _run_cross_class_sequentially(ledger_path, order: tuple[str, str]) -> dict[str, str]:
    """The same two requests, run one after another (no concurrency at
    all) -- the reference outcome that "sequential-equivalence" pins."""
    engine = _engine(ledger_path)
    _seed_baseline(engine)

    by_label = {"A": CLASS_A, "B": CLASS_B}
    outcomes: dict[str, str] = {}
    for label in order:
        decision = engine.evaluate_and_reserve(_action(by_label[label], REQUEST, f"race/{label}"))
        outcomes[label] = decision.outcome
    return outcomes


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_cross_class_sequential_reference_denies_a_allows_b(order, tmp_path):
    """Pin the single correct answer, independent of call order: A's own
    exposure (baseline 50 + requested 60) exceeds its cap; B's own
    exposure (0 + 60) does not. Neither class's decision depends on the
    other's, in either order."""
    ledger_path = tmp_path / f"seq-{'-'.join(order)}.jsonl"
    outcomes = _run_cross_class_sequentially(ledger_path, order)
    assert outcomes == {"A": "deny", "B": "allow"}


@pytest.mark.parametrize("round_idx", range(25))
def test_cross_class_concurrent_race_matches_sequential_equivalence(round_idx, tmp_path):
    """Property/soak: concurrent, barrier-started execution must reproduce
    the exact sequential-equivalence answer every round -- exactly one
    request denied (A, on its own exposure), the other allowed (B),
    regardless of interleaving. A joint allow (Jody's dev-wide-aggregate
    race) or a joint deny (cross-class contamination) are both a bug."""
    ledger_path = tmp_path / f"race-{round_idx}.jsonl"
    outcomes = _run_cross_class_race(ledger_path)
    assert outcomes == {"A": "deny", "B": "allow"}, f"round {round_idx}: {outcomes}"

    # independently recomputable: each class's own scoped aggregate reflects
    # only its own accepted records, never the other class's.
    records = read_ledger(str(ledger_path))
    assert active_exposure_minor(records, DEVELOPER, action_class=CLASS_A) == BASELINE_A
    assert active_exposure_minor(records, DEVELOPER, action_class=CLASS_B) == REQUEST
    assert active_exposure_minor(records, DEVELOPER) == BASELINE_A + REQUEST  # dev-wide, unscoped


# -- mutant: defeat the class filter, confirm the test flips -----------------


def test_cross_class_mutant_unscoped_aggregate_wrongly_denies_both(tmp_path):
    """Mutant test for the fix itself: defeat the ``action_class`` filter at
    the call site (reverting ``active_exposure_minor`` to the pre-fix
    dev-wide read regardless of what the engine passes) and confirm the
    same race that correctly denies exactly one (A) now wrongly denies
    both -- B's own untouched 100-cap gets rejected because A's baseline
    leaks across the class boundary. Proves the class-scoping fix is
    load-bearing, not decorative.

    Deterministic, not timing-dependent: A's own request always denies on
    its own exposure alone (with or without the fix), so A never appends a
    new reserve record and the dev-wide aggregate never moves during the
    race -- B's mutant-read of "current" is pinned at the baseline value
    regardless of thread interleaving.
    """
    import capsule_emit.holds.engine as holds_engine_module

    real_aggregate = holds_engine_module.active_exposure_minor

    def _unscoped_aggregate(records, subject, *, action_class=None, as_of=None):  # noqa: ARG001
        # the mutant: the class filter is dropped at the call site, exactly
        # undoing this task's fix (`action_class` is silently ignored).
        return real_aggregate(records, subject, as_of=as_of)

    holds_engine_module.active_exposure_minor = _unscoped_aggregate
    try:
        ledger_path = tmp_path / "mutant-cross-class.jsonl"
        outcomes = _run_cross_class_race(ledger_path)
        assert outcomes == {"A": "deny", "B": "deny"}, (
            "mutant did not flip B's outcome -- the class-scoped aggregate fix is not load-bearing "
            f"(got {outcomes}, expected the pre-fix cross-contamination denial of both)"
        )
    finally:
        holds_engine_module.active_exposure_minor = real_aggregate
