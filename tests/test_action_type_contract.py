# SPDX-License-Identifier: Apache-2.0
"""Contract: documented action_type set == verifier-accepted set (§5.1).

VALID_ACTION_TYPES in core.py is the machine-readable form of what the emit()
docstring documents.  The tests here probe verify() with each candidate to
confirm the two sides stay in sync.  Widening or narrowing either side without
updating the other will flip a test red.

Mutant discipline (QUEUE_PROTOCOL §7): every negative check must fail its
mutant.  For each "must reject" assertion below, removing the assertion is the
mutant; the `test_documented_set_matches_verifier_accepted_set` test catches
the mutant by probing the verifier independently.
"""
from __future__ import annotations

import pytest
from agent_action_capsule import verify

from capsule_emit import emit
from capsule_emit.core import VALID_ACTION_TYPES


def _capsule(tmp_path, action_type: str | None, idx: int = 0) -> dict:
    result = emit(
        action="check_action",
        operator="test-org",
        developer="test-agent@v1",
        agent_input={"n": idx},
        agent_output={"ok": True},
        model={"provider": "test", "model_id": "test-model"},
        verdict="executed",
        effect={"type": "check_action", "status": "dispatched"},
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        action_type=action_type,
    )
    return result.capsule


def test_valid_action_types_each_pass_verify(tmp_path):
    """Every value in VALID_ACTION_TYPES must make verify().ok True."""
    for i, candidate in enumerate(sorted(VALID_ACTION_TYPES)):
        cap = _capsule(tmp_path, candidate, idx=i)
        assert verify(cap).ok, f"VALID_ACTION_TYPES member {candidate!r} must pass verify()"


def test_act_is_rejected(tmp_path):
    """'act' must make verify().ok False — removing this is the mutant.

    The test_documented_set_matches_verifier_accepted_set test below is the
    independent catch: if 'act' somehow starts passing verify(), that test fails
    even if this explicit assertion were removed.
    """
    cap = _capsule(tmp_path, "act")
    vr = verify(cap)
    assert not vr.ok, "'act' must be rejected by the §5.1 verifier"
    assert any(
        f.code == "action_type_invalid" for f in vr.findings
    ), "'act' must produce an action_type_invalid finding"


def test_retrieve_is_rejected(tmp_path):
    """'retrieve' must make verify().ok False — it appeared in the old docstring."""
    cap = _capsule(tmp_path, "retrieve")
    assert not verify(cap).ok, "'retrieve' must be rejected by the §5.1 verifier"


def test_documented_set_matches_verifier_accepted_set(tmp_path):
    """VALID_ACTION_TYPES == probe-derived set of values verify() accepts.

    This is the property lock.  Changing either the constant or the verifier's
    §5.1 check without updating the other will fail here.  The probe candidates
    are the documented set plus the previously-documented-but-invalid values.
    """
    candidates = VALID_ACTION_TYPES | {"act", "retrieve"}
    accepted: set[str] = set()
    for i, candidate in enumerate(sorted(candidates)):
        cap = _capsule(tmp_path, candidate, idx=i)
        if verify(cap).ok:
            accepted.add(candidate)

    assert accepted == VALID_ACTION_TYPES, (
        f"Verifier accepts {accepted!r} but VALID_ACTION_TYPES is {set(VALID_ACTION_TYPES)!r}. "
        "Update VALID_ACTION_TYPES (if §5.1 was intentionally widened) or fix "
        "the docstring (if the widening was accidental)."
    )
