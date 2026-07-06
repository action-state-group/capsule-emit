# SPDX-License-Identifier: Apache-2.0
"""Tests for the capsule-emit approval module.

Covers:
- seal_approval produces correct chain (parent_capsule_id + relation="resolves")
- seal_approval sets compute_attestation.human_disposed=True
- list_pending: empty when resolved
- list_pending: shows unresolved blocked capsule
- list_pending: crash-resume — blocked capsule persists after ledger re-read
- seal_approval with decision="deny" → verdict_class="denied"
- Zero engine imports (no gopher_ai references in approval.py)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agent_action_capsule import verify

from capsule_emit.approval import list_pending, seal_approval
from capsule_emit.core import emit
from capsule_emit.ledger import read_ledger, append_to_ledger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _blocked_capsule(tmp_path: Path, action: str = "write_po") -> dict:
    """Emit a blocked capsule directly and return the capsule dict."""
    result = emit(
        action=action,
        operator="test-org",
        developer="agent@v1",
        verdict="blocked",
        effect={"type": action, "status": "planned"},
        anchor=False,
        ledger=tmp_path / "ledger.jsonl",
    )
    return result.capsule


def _approval_result(tmp_path: Path, blocked_id: str, decision: str = "approve"):
    """Seal an approval capsule for the given blocked_id."""
    return seal_approval(
        blocked_capsule_id=blocked_id,
        approver_id="alice@org.example",
        decision=decision,
        action_digest="abc123",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        operator="test-org",
        developer="approver@v1",
    )


# ---------------------------------------------------------------------------
# test_seal_approval_chains_to_blocked
# ---------------------------------------------------------------------------


def test_seal_approval_chains_to_blocked(tmp_path):
    """seal_approval produces a capsule with correct chain.parent_capsule_id."""
    blocked = _blocked_capsule(tmp_path)
    blocked_id = blocked["capsule_id"]

    result = _approval_result(tmp_path, blocked_id)
    capsule = result.capsule

    chain = capsule.get("chain") or {}
    assert chain.get("parent_capsule_id") == blocked_id, (
        f"chain.parent_capsule_id should be {blocked_id!r}, got {chain.get('parent_capsule_id')!r}"
    )
    assert chain.get("relation") == "resolves", (
        f"chain.relation should be 'resolves', got {chain.get('relation')!r}"
    )


# ---------------------------------------------------------------------------
# test_seal_approval_human_disposed_true
# ---------------------------------------------------------------------------


def test_seal_approval_human_disposed_true(tmp_path):
    """seal_approval sets compute_attestation.human_disposed to True."""
    blocked = _blocked_capsule(tmp_path)
    result = _approval_result(tmp_path, blocked["capsule_id"])
    capsule = result.capsule

    ca = capsule["model_attestation"]["compute_attestation"]
    assert ca.get("human_disposed") is True, (
        f"compute_attestation.human_disposed should be True, got {ca.get('human_disposed')!r}"
    )


def test_seal_approval_approver_id_in_compute_attestation(tmp_path):
    """seal_approval stores approver_id in compute_attestation."""
    blocked = _blocked_capsule(tmp_path)
    result = seal_approval(
        blocked_capsule_id=blocked["capsule_id"],
        approver_id="bob@example.com",
        decision="approve",
        action_digest="digest-xyz",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
    )
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert ca.get("approver_id") == "bob@example.com"
    assert ca.get("action_digest") == "digest-xyz"


def test_seal_approval_capsule_verifies(tmp_path):
    """Approval capsule passes agent_action_capsule.verify()."""
    blocked = _blocked_capsule(tmp_path)
    result = _approval_result(tmp_path, blocked["capsule_id"])
    v = verify(result.capsule)
    assert v.ok, f"Approval capsule failed verify: {v}"


# ---------------------------------------------------------------------------
# test_list_pending_empty_when_resolved
# ---------------------------------------------------------------------------


def test_list_pending_empty_when_resolved(tmp_path):
    """After seal_approval, the blocked capsule is not in list_pending()."""
    blocked = _blocked_capsule(tmp_path)
    pending_before = list_pending(tmp_path / "ledger.jsonl")
    assert len(pending_before) == 1, "Should have one pending capsule before approval"

    _approval_result(tmp_path, blocked["capsule_id"])

    pending_after = list_pending(tmp_path / "ledger.jsonl")
    assert len(pending_after) == 0, (
        f"list_pending() should be empty after approval, got {len(pending_after)} entries"
    )


# ---------------------------------------------------------------------------
# test_list_pending_shows_unresolved
# ---------------------------------------------------------------------------


def test_list_pending_shows_unresolved(tmp_path):
    """Blocked capsule without a resolution appears in list_pending()."""
    blocked = _blocked_capsule(tmp_path)
    pending = list_pending(tmp_path / "ledger.jsonl")

    ids = [p["capsule_id"] for p in pending]
    assert blocked["capsule_id"] in ids, (
        f"Blocked capsule {blocked['capsule_id']!r} should be in list_pending(), got {ids}"
    )


def test_list_pending_empty_ledger(tmp_path):
    """list_pending() returns [] for an empty or missing ledger."""
    # Missing file
    assert list_pending(tmp_path / "nonexistent.jsonl") == []

    # Empty file
    empty = tmp_path / "empty.jsonl"
    empty.touch()
    assert list_pending(empty) == []


def test_list_pending_executed_capsule_not_pending(tmp_path):
    """Executed (passed) capsules do not appear in list_pending()."""
    emit(
        action="safe_action",
        operator="test-org",
        developer="agent@v1",
        verdict="executed",
        effect={"type": "safe_action", "status": "dispatched"},
        anchor=False,
        ledger=tmp_path / "ledger.jsonl",
    )
    pending = list_pending(tmp_path / "ledger.jsonl")
    assert pending == [], f"Executed capsules should not be pending, got {pending}"


def test_list_pending_multiple_blocked_one_resolved(tmp_path):
    """Two blocked capsules, one resolved → only unresolved appears in list_pending()."""
    ledger = tmp_path / "ledger.jsonl"

    blocked_1 = emit(
        action="action_a",
        operator="test-org",
        developer="agent@v1",
        verdict="blocked",
        effect={"type": "action_a", "status": "planned"},
        anchor=False,
        ledger=ledger,
    ).capsule

    blocked_2 = emit(
        action="action_b",
        operator="test-org",
        developer="agent@v1",
        verdict="blocked",
        effect={"type": "action_b", "status": "planned"},
        anchor=False,
        ledger=ledger,
    ).capsule

    # Resolve only blocked_1
    seal_approval(
        blocked_capsule_id=blocked_1["capsule_id"],
        approver_id="alice@org.example",
        decision="approve",
        action_digest="d1",
        ledger=ledger,
        anchor=False,
    )

    pending = list_pending(ledger)
    pending_ids = [p["capsule_id"] for p in pending]

    assert blocked_1["capsule_id"] not in pending_ids, "Resolved capsule must not be pending"
    assert blocked_2["capsule_id"] in pending_ids, "Unresolved capsule must be pending"
    assert len(pending) == 1


# ---------------------------------------------------------------------------
# test_list_pending_crash_resume
# ---------------------------------------------------------------------------


def test_list_pending_crash_resume(tmp_path):
    """Write a ledger with a blocked capsule; re-read it; list_pending still finds it.

    Simulates a process restart: the ledger was written in a prior run,
    the process crashed before seal_approval ran.  On the next startup,
    list_pending must still surface the blocked capsule — the only source
    of truth is the ledger file itself.
    """
    ledger = tmp_path / "ledger.jsonl"

    # --- "prior run" --- emit a blocked capsule and write it to disk
    blocked = emit(
        action="send_payment",
        operator="payments-co",
        developer="payment-agent@v1",
        verdict="blocked",
        effect={"type": "send_payment", "status": "planned"},
        anchor=False,
        ledger=ledger,
    ).capsule
    blocked_id = blocked["capsule_id"]

    # Verify it's on disk
    assert ledger.exists()
    rows = read_ledger(ledger)
    assert len(rows) == 1

    # --- "process restart" --- simulate by reading ledger cold, no in-memory state
    # list_pending reads from disk; it must find the blocked capsule
    pending = list_pending(ledger)

    assert len(pending) == 1, (
        f"After crash-resume, list_pending should find 1 blocked capsule, got {len(pending)}"
    )
    assert pending[0]["capsule_id"] == blocked_id, (
        f"Expected blocked capsule {blocked_id!r}, got {pending[0]['capsule_id']!r}"
    )
    assert pending[0]["disposition"]["verdict_class"] == "blocked"

    # Resolving AFTER the "restart" removes it from pending
    seal_approval(
        blocked_capsule_id=blocked_id,
        approver_id="ops@payments.example",
        decision="approve",
        action_digest="payment-digest",
        ledger=ledger,
        anchor=False,
    )
    pending_after = list_pending(ledger)
    assert len(pending_after) == 0, "After resolution, list_pending must be empty"


# ---------------------------------------------------------------------------
# test_seal_approval_deny
# ---------------------------------------------------------------------------


def test_seal_approval_deny(tmp_path):
    """decision='deny' → verdict_class='denied' and chain.relation='resolves'."""
    blocked = _blocked_capsule(tmp_path)
    result = seal_approval(
        blocked_capsule_id=blocked["capsule_id"],
        approver_id="alice@org.example",
        decision="deny",
        action_digest="d2",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
    )
    capsule = result.capsule

    assert capsule["disposition"]["verdict_class"] == "denied", (
        f"Expected 'denied', got {capsule['disposition']['verdict_class']!r}"
    )
    assert capsule["disposition"]["decision"] == "deny"
    assert capsule["chain"]["relation"] == "resolves"
    assert capsule["chain"]["parent_capsule_id"] == blocked["capsule_id"]


def test_seal_approval_deny_resolves_pending(tmp_path):
    """decision='deny' also resolves the blocked capsule in list_pending()."""
    blocked = _blocked_capsule(tmp_path)
    pending_before = list_pending(tmp_path / "ledger.jsonl")
    assert len(pending_before) == 1

    seal_approval(
        blocked_capsule_id=blocked["capsule_id"],
        approver_id="alice@org.example",
        decision="deny",
        action_digest="d3",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
    )
    pending_after = list_pending(tmp_path / "ledger.jsonl")
    assert len(pending_after) == 0, "A denied capsule should also resolve the pending state"


def test_seal_approval_invalid_decision(tmp_path):
    """seal_approval raises ValueError for an invalid decision string."""
    blocked = _blocked_capsule(tmp_path)
    with pytest.raises(ValueError, match="decision must be"):
        seal_approval(
            blocked_capsule_id=blocked["capsule_id"],
            approver_id="alice@org.example",
            decision="maybe",
            action_digest="d4",
            ledger=tmp_path / "ledger.jsonl",
            anchor=False,
        )


# ---------------------------------------------------------------------------
# Zero engine imports — approval.py must not import gopher_ai
# ---------------------------------------------------------------------------


def test_no_engine_imports():
    """approval.py must not reference gopher_ai (the private engine)."""
    import pathlib

    approval_src = pathlib.Path(__file__).parent.parent / "capsule_emit" / "approval.py"
    text = approval_src.read_text(encoding="utf-8")
    assert "gopher_ai" not in text, (
        "approval.py must not import gopher_ai — this is a public-safe module"
    )
