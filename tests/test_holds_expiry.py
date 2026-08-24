# SPDX-License-Identifier: Apache-2.0
"""Expiry terminally resolves a hold; a resume-time approval re-checks the
hold's current status before it is allowed to dispatch."""
from __future__ import annotations

import json

import pytest
from agent_action_capsule import verify

from capsule_emit.approval import seal_approval
from capsule_emit.canonicalization import compute_capsule_id
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


def test_breach_sequence_omitting_resume_ok_now_raises_not_silently_dispatches(tmp_path):
    """Regression: omitting ``resume_ok`` against a hold reserve used to
    silently default to ``True`` and dispatch over the limit (the original
    bug hold-02 closed the *wiring* for). This task's own fix (the
    ``resume_ok=None`` loud check) closes the *omission* itself -- a caller
    that forgets to wire the resume-time recheck in now gets a loud
    :class:`ValueError`, not a silent over-limit approval."""
    engine, ledger_path, reserve_id, status = _breach_sequence(tmp_path)
    assert status == HoldStatus.EXPIRED  # setup precondition unaffected

    with pytest.raises(ValueError, match="resume_ok must be explicitly"):
        seal_approval(
            blocked_capsule_id=reserve_id, approver_id="alice@acme.example", decision="approve",
            action_digest="a" * 64, ledger=str(ledger_path), anchor=False, operator=OPERATOR, developer=DEVELOPER,
            # no resume_ok / resume_reason -- must raise, not silently dispatch.
        )


def test_breach_sequence_explicit_resume_ok_true_still_dispatches_over_limit(tmp_path):
    """The loud check only catches *omission*. A caller that explicitly
    (mistakenly) hardcodes ``resume_ok=True`` instead of actually wiring in
    ``hold_status()`` is a deliberate override this task does not and
    cannot prevent -- ``resume_ok`` is a plain data seam fed by the caller
    (``approval.py``'s own docstring), not something ``seal_approval`` can
    independently verify. Pinned here as the residual risk the fix leaves
    in place, distinct from the (now closed) silent-default case above."""
    engine, ledger_path, reserve_id, status = _breach_sequence(tmp_path)
    assert status == HoldStatus.EXPIRED  # setup precondition unaffected

    mutant_result = seal_approval(
        blocked_capsule_id=reserve_id, approver_id="alice@acme.example", decision="approve",
        action_digest="a" * 64, ledger=str(ledger_path), anchor=False, operator=OPERATOR, developer=DEVELOPER,
        resume_ok=True,  # the mutant: hardcoded instead of wired to hold_status()
    )
    assert mutant_result.capsule["disposition"]["decision"] == "approve", (
        "hardcoded resume_ok=True did not dispatch -- explicit override semantics changed unexpectedly"
    )
    assert mutant_result.capsule["effect"]["status"] == "dispatched", (
        "hardcoded resume_ok=True did not dispatch -- explicit override semantics changed unexpectedly"
    )


# -- resume_ok default: loud when a hold applies, quiet when none does -------


def test_resume_ok_unset_against_a_reserve_raises_instead_of_silently_approving(tmp_path):
    """``resume_ok`` defaults to ``None``. When ``blocked_capsule_id`` is
    itself a hold reserve, forgetting to wire the resume-time recheck must
    be loud (raise), not silently resolve to the old ``True`` default that
    let a stale approval dispatch over budget."""
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000, target="acct-1"))
    reserve_id = reserve.capsule["capsule_id"]

    with pytest.raises(ValueError, match="resume_ok must be explicitly"):
        seal_approval(
            blocked_capsule_id=reserve_id, approver_id="alice@acme.example", decision="approve",
            action_digest="a" * 64, ledger=str(ledger_path), anchor=False, operator=OPERATOR, developer=DEVELOPER,
            # resume_ok left unset -- must not silently default to True.
        )

    # explicit True/False both still work, unaffected by the default change.
    result = seal_approval(
        blocked_capsule_id=reserve_id, approver_id="alice@acme.example", decision="approve",
        action_digest="a" * 64, ledger=str(ledger_path), anchor=False, operator=OPERATOR, developer=DEVELOPER,
        resume_ok=True,
    )
    assert result.capsule["disposition"]["decision"] == "approve"


def test_resume_ok_unset_when_no_hold_applies_behaves_as_true(tmp_path):
    """A blocked capsule that never chains from a hold reserve (an ordinary
    ``verdict="blocked"`` action, no ``HoldEngine`` in play at all) is the
    "no hold applies" case the ``None`` default documents -- it must resolve
    quietly to ``True``, not raise."""
    from capsule_emit.core import _emit_capsule

    ledger_path = tmp_path / "ledger.jsonl"
    blocked = _emit_capsule(
        action="write_po", operator=OPERATOR, developer=DEVELOPER, verdict="blocked",
        effect={"type": "write_po", "status": "planned"}, anchor=False, ledger=str(ledger_path),
    )

    result = seal_approval(
        blocked_capsule_id=blocked.capsule["capsule_id"], approver_id="alice@acme.example", decision="approve",
        action_digest="a" * 64, ledger=str(ledger_path), anchor=False, operator=OPERATOR, developer=DEVELOPER,
        # resume_ok left unset -- no hold applies, must not raise.
    )
    assert result.capsule["disposition"]["decision"] == "approve"


# -- ambiguous status fails closed --------------------------------------------


def test_hold_status_ambiguous_when_reserve_record_missing(tmp_path):
    engine = _engine(tmp_path / "ledger.jsonl")
    status, terminal = engine.hold_status("0" * 64)
    assert status == HoldStatus.AMBIGUOUS
    assert terminal is None


def test_hold_status_honors_declared_jcs(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    engine = _engine(ledger_path)
    reserve = engine.evaluate_and_reserve(_action(amount_minor=1_000, target="acct-1"))

    record = dict(reserve.capsule)
    record["canonicalization_id"] = "jcs"
    record["canonicalization_probe"] = {"null_member": None, "empty_array": []}
    record["capsule_id"] = compute_capsule_id(record)
    ledger_path.write_text(json.dumps(record) + "\n")

    status, terminal = engine.hold_status(record["capsule_id"])
    assert status == HoldStatus.ACTIVE
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
