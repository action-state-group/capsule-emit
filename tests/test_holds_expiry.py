# SPDX-License-Identifier: Apache-2.0
"""Expiry terminally resolves a hold; a resume-time approval re-checks the
hold's current status before it is allowed to dispatch."""
from __future__ import annotations

from agent_action_capsule import verify

from capsule_emit.approval import seal_approval
from capsule_emit.holds import Action, HoldEngine, HoldStatus
from capsule_emit.holds.errors import HOLD_ALREADY_TERMINAL, HOLD_STATUS_AMBIGUOUS
from capsule_emit.ledger import read_ledger

DEVELOPER = "procurement-agent@v1"
OPERATOR = "acme-research"


def _action(amount_minor=100, action_id=None, target=None):
    return Action(
        verb="transfer_funds", operator=OPERATOR, developer=DEVELOPER, action_class="money.transfer",
        amount_minor=amount_minor, currency="EUR", action_id=action_id, target=target,
    )


def _engine(ledger_path, **kw):
    return HoldEngine(ledger_path=str(ledger_path), cap_minor={"money.transfer": 10_000_000}, **kw)


# -- expiry is TERMINAL -------------------------------------------------------


def test_expire_is_terminal_further_lifecycle_calls_deny(tmp_path):
    engine = _engine(tmp_path / "ledger.jsonl")
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000, target="acct-1"))
    assert reserve.outcome == "allow"
    reserve_id = reserve.capsule["capsule_id"]

    expired = engine.expire(reserve_id, reason="ttl elapsed")
    assert expired.outcome == "allow"
    assert expired.hold_status == HoldStatus.EXPIRED
    assert verify(expired.capsule).ok

    # nothing may act on the original hold after this
    again_expire = engine.expire(reserve_id)
    assert again_expire.outcome == "deny"
    assert again_expire.reason_code == HOLD_ALREADY_TERMINAL
    assert again_expire.hold_status == HoldStatus.EXPIRED

    release_attempt = engine.release(reserve_id)
    assert release_attempt.outcome == "deny"
    assert release_attempt.reason_code == HOLD_ALREADY_TERMINAL


# -- resume = a fresh evaluate_and_reserve, no special path on HoldEngine ----


def test_resume_after_expiry_is_a_fresh_evaluate_and_reserve_no_special_path(tmp_path):
    engine = _engine(tmp_path / "ledger.jsonl")
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000, target="acct-1"))
    reserve_id = reserve.capsule["capsule_id"]
    engine.expire(reserve_id)

    # HoldEngine exposes no resume(reserve_id)-shaped method at all -- the
    # only way forward is calling evaluate_and_reserve() again, through the
    # SAME atomic (scope-locked, aggregate-evaluating) path as any other
    # action.
    assert not hasattr(engine, "resume")

    resumed = engine.evaluate_and_reserve(_action(amount_minor=1_000, target="acct-1"))
    assert resumed.outcome == "allow"
    assert resumed.capsule["capsule_id"] != reserve_id
    assert resumed.capsule["action_id"].startswith("hold.reserve/")

    # the fresh reservation is genuinely new, independent exposure: the
    # expired hold's own exposure already netted to zero.
    from capsule_emit.holds.aggregate import active_exposure_minor

    records = read_ledger(str(tmp_path / "ledger.jsonl"))
    assert active_exposure_minor(records, DEVELOPER) == 1_000


# -- the four-step breach sequence: expiry, other spend, then a late resume -


RESERVE_AMOUNT = 9_500_000  # cap is 10,000,000 minor units
OTHER_AMOUNT = 9_800_000  # consumes nearly all remaining headroom after expiry


def _breach_sequence(tmp_path):
    """1. reserve 9,500,000 against the 10,000,000 cap.
    2. the hold expires.
    3. other, unrelated activity consumes most of the now-freed headroom.
    4. a late approval for the ORIGINAL (now-expired) hold arrives.
    Returns (engine, ledger_path, reserve_id, status_at_resume)."""
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)

    reserve = engine.evaluate_and_reserve(
        _action(amount_minor=RESERVE_AMOUNT, action_id="transfer_funds/1", target="acct-1")
    )
    assert reserve.outcome == "allow"
    reserve_id = reserve.capsule["capsule_id"]

    expired = engine.expire(reserve_id, reason="ttl elapsed while awaiting approval")
    assert expired.outcome == "allow"

    other = engine.evaluate_and_reserve(
        _action(amount_minor=OTHER_AMOUNT, action_id="transfer_funds/2", target="acct-2")
    )
    assert other.outcome == "allow"

    status, _terminal = engine.hold_status(reserve_id)
    return engine, ledger_path, reserve_id, status


def test_breach_sequence_late_approval_denied_and_refusal_recorded(tmp_path):
    engine, ledger_path, reserve_id, status = _breach_sequence(tmp_path)
    assert status == HoldStatus.EXPIRED

    result = seal_approval(
        blocked_capsule_id=reserve_id, approver_id="alice@acme.example", decision="approve",
        action_digest="a" * 64, ledger=str(ledger_path), anchor=False, operator=OPERATOR, developer=DEVELOPER,
        resume_ok=(status == HoldStatus.ACTIVE), resume_reason=f"hold status: {status.value}",
    )
    assert result.capsule["disposition"]["decision"] == "deny"
    assert result.capsule["disposition"]["verdict_class"] == "denied"
    assert result.capsule.get("effect") is None  # never dispatched
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert ca["resume_check"] == {"ok": False, "reason": "hold status: expired"}

    # the refusal is recorded: readable straight back from the ledger
    records = read_ledger(str(ledger_path))
    assert records[-1]["capsule_id"] == result.capsule_id
    assert verify(result.capsule).ok

    # the correct path forward: a fresh evaluate_and_reserve, which -- since
    # the remaining cap was already consumed by other activity -- correctly
    # denies too, not silently succeeds.
    resumed = engine.evaluate_and_reserve(
        _action(amount_minor=RESERVE_AMOUNT, action_id="transfer_funds/1-resume", target="acct-1")
    )
    assert resumed.outcome == "deny"


def test_breach_sequence_mutant_no_resume_check_dispatches_over_limit(tmp_path):
    """Mutant test: the resume-time recheck (this task's own fix) is what
    ``seal_approval``'s ``resume_ok``/``resume_reason`` wiring closes. Calling
    it WITHOUT that wiring -- as every caller did before this task -- is the
    mutant: it reproduces the original bug (late approval dispatches over
    the limit with every integrity check passing), proving the wiring is
    load-bearing, not decorative."""
    engine, ledger_path, reserve_id, status = _breach_sequence(tmp_path)
    assert status == HoldStatus.EXPIRED  # setup precondition unaffected by the mutant

    mutant_result = seal_approval(
        blocked_capsule_id=reserve_id, approver_id="alice@acme.example", decision="approve",
        action_digest="a" * 64, ledger=str(ledger_path), anchor=False, operator=OPERATOR, developer=DEVELOPER,
        # no resume_ok / resume_reason -- the mutant: the caller never asks.
    )
    assert mutant_result.capsule["disposition"]["decision"] == "approve", (
        "mutant did not flip the outcome -- omitting the resume-time recheck is not load-bearing"
    )
    assert mutant_result.capsule["effect"]["status"] == "dispatched", (
        "mutant did not flip dispatch -- omitting the resume-time recheck is not load-bearing"
    )


# -- ambiguous status fails closed --------------------------------------------


def test_hold_status_ambiguous_when_reserve_record_missing(tmp_path):
    engine = _engine(tmp_path / "ledger.jsonl")
    status, terminal = engine.hold_status("0" * 64)
    assert status == HoldStatus.AMBIGUOUS
    assert terminal is None


def test_ambiguous_terminal_record_fails_closed_for_consequential_class(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000, target="acct-1"))
    reserve_id = reserve.capsule["capsule_id"]
    expired = engine.expire(reserve_id)
    expire_id = expired.capsule["capsule_id"]

    import capsule_emit.holds.engine as holds_engine_module

    real_verify = holds_engine_module.verify_capsule

    def _forced_failure(capsule):
        if capsule.get("capsule_id") == expire_id:
            from agent_action_capsule import Finding, VerificationResult
            return VerificationResult(ok=False, findings=[Finding("simulated_ambiguity", "forced failure for test")])
        return real_verify(capsule)

    monkeypatch.setattr(holds_engine_module, "verify_capsule", _forced_failure)

    status, terminal = engine.hold_status(reserve_id)
    assert status == HoldStatus.AMBIGUOUS
    assert terminal is not None and terminal["capsule_id"] == expire_id

    # ambiguous is never silently treated as active: every lifecycle op
    # still fails closed (deny), not "proceed as if this hold is active".
    release_attempt = engine.release(reserve_id)
    assert release_attempt.outcome == "deny"
    assert release_attempt.reason_code in (HOLD_ALREADY_TERMINAL, HOLD_STATUS_AMBIGUOUS)
