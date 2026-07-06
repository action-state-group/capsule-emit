#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests for the AAuth + Capsule mutual interop demo.

Exercises:
  - mutual seal: both orgs produce valid capsules
  - shared subject_digest: both capsules attest over the same action digest
  - disposition.authority: the AAuth grant reference is recorded on Planner's capsule
  - verify: agent-action-capsule Class-1 verify passes ok=True for both
  - chain: DJ's capsule correctly chains to Planner's
  - no network required: runs fully offline (anchor=False)

Run:
    pip install "capsule-emit" "agent-action-capsule" pytest
    pytest examples/aauth-capsule-interop/test_mutual.py -v
"""
from __future__ import annotations

# Import helpers from the demo module
import sys
from pathlib import Path

import pytest
from agent_action_capsule import verify
from agent_action_capsule.canonical import json_digest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo import ACTION, _stub_aauth_grant, seal_dj, seal_planner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "test_ledger.jsonl"


@pytest.fixture
def subject_digest() -> str:
    return json_digest(ACTION)


@pytest.fixture
def grant_jti() -> str:
    return _stub_aauth_grant()


@pytest.fixture
def planner_capsule(subject_digest: str, grant_jti: str, ledger: Path) -> dict:
    return seal_planner(
        ACTION, subject_digest, grant_jti,
        ledger=ledger, should_anchor=False, anchor_endpoint=None,
    )


@pytest.fixture
def dj_capsule(subject_digest: str, planner_capsule: dict, ledger: Path) -> dict:
    outcome = {
        "accepted": True,
        "slot_confirmed": f"{ACTION['date']} @ {ACTION['venue']}",
        "set_duration_min": ACTION["set_duration_min"],
        "booking_ref": "DJ-TEST0001",
    }
    return seal_dj(
        ACTION, subject_digest, planner_capsule["capsule_id"], outcome,
        ledger=ledger, should_anchor=False, anchor_endpoint=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSubjectDigest:
    def test_subject_digest_is_hex64(self, subject_digest: str) -> None:
        assert len(subject_digest) == 64
        assert all(c in "0123456789abcdef" for c in subject_digest)

    def test_subject_digest_is_deterministic(self) -> None:
        sd1 = json_digest(ACTION)
        sd2 = json_digest(ACTION)
        assert sd1 == sd2, "json_digest must be deterministic"

    def test_subject_digest_changes_on_mutation(self, subject_digest: str) -> None:
        mutated = {**ACTION, "date": "2099-01-01"}
        assert json_digest(mutated) != subject_digest


class TestPlannerSeal:
    def test_planner_capsule_structure(self, planner_capsule: dict) -> None:
        assert "capsule_id" in planner_capsule
        assert len(planner_capsule["capsule_id"]) == 64

    def test_planner_operator(self, planner_capsule: dict) -> None:
        assert planner_capsule["operator"] == "planner-org"

    def test_planner_developer(self, planner_capsule: dict) -> None:
        assert planner_capsule["developer"] == "planner-agent@v1"

    def test_planner_verdict(self, planner_capsule: dict) -> None:
        assert planner_capsule["disposition"]["verdict_class"] == "executed"

    def test_planner_authority_set(self, planner_capsule: dict, grant_jti: str) -> None:
        """AAuth grant JTI is recorded as an opaque reference in disposition.authority."""
        authority = planner_capsule["disposition"].get("authority")
        assert authority == grant_jti
        assert "stub" in authority  # labeled as stub in the demo
        assert len(authority) > 10  # non-empty opaque reference

    def test_planner_authority_is_not_token_body(self, planner_capsule: dict) -> None:
        """authority must be an identifier reference, never the full token."""
        authority = planner_capsule["disposition"].get("authority", "")
        # Token bodies are JWTs: three base64url segments separated by dots
        assert authority.count(".") < 2, "authority must not be a JWT body"

    def test_planner_effect_dispatched(self, planner_capsule: dict) -> None:
        assert planner_capsule["effect"]["status"] == "dispatched"

    def test_planner_subject_digest_present(
        self, planner_capsule: dict, subject_digest: str
    ) -> None:
        ca = planner_capsule["model_attestation"]["compute_attestation"]
        assert ca["subject_digest"] == subject_digest

    def test_planner_verify_ok(self, planner_capsule: dict) -> None:
        vr = verify(planner_capsule)
        assert vr.ok, f"planner capsule failed verify: {[f.detail for f in vr.findings]}"


class TestDJSeal:
    def test_dj_capsule_structure(self, dj_capsule: dict) -> None:
        assert "capsule_id" in dj_capsule
        assert len(dj_capsule["capsule_id"]) == 64

    def test_dj_operator(self, dj_capsule: dict) -> None:
        assert dj_capsule["operator"] == "dj-org"

    def test_dj_developer(self, dj_capsule: dict) -> None:
        assert dj_capsule["developer"] == "dj-agent@v1"

    def test_dj_verdict(self, dj_capsule: dict) -> None:
        assert dj_capsule["disposition"]["verdict_class"] == "executed"

    def test_dj_effect_confirmed(self, dj_capsule: dict) -> None:
        assert dj_capsule["effect"]["status"] == "confirmed"

    def test_dj_effect_has_response_digest(self, dj_capsule: dict) -> None:
        response_digest = dj_capsule["effect"].get("response_digest")
        assert response_digest is not None
        assert len(response_digest) == 64

    def test_dj_subject_digest_present(
        self, dj_capsule: dict, subject_digest: str
    ) -> None:
        ca = dj_capsule["model_attestation"]["compute_attestation"]
        assert ca["subject_digest"] == subject_digest

    def test_dj_verify_ok(self, dj_capsule: dict) -> None:
        vr = verify(dj_capsule)
        assert vr.ok, f"dj capsule failed verify: {[f.detail for f in vr.findings]}"


class TestMutualChain:
    def test_dj_chains_to_planner(
        self, planner_capsule: dict, dj_capsule: dict
    ) -> None:
        assert dj_capsule["chain"]["parent_capsule_id"] == planner_capsule["capsule_id"]

    def test_chain_relation_is_confirms(self, dj_capsule: dict) -> None:
        assert dj_capsule["chain"]["relation"] == "confirms"

    def test_same_subject_digest_both_orgs(
        self, planner_capsule: dict, dj_capsule: dict, subject_digest: str
    ) -> None:
        """The shared subject_digest binds both capsules to the same action."""
        sd_a = planner_capsule["model_attestation"]["compute_attestation"]["subject_digest"]
        sd_b = dj_capsule["model_attestation"]["compute_attestation"]["subject_digest"]
        assert sd_a == sd_b == subject_digest, (
            f"subject_digest mismatch: planner={sd_a} dj={sd_b} expected={subject_digest}"
        )

    def test_different_capsule_ids(
        self, planner_capsule: dict, dj_capsule: dict
    ) -> None:
        """Each org produces a distinct capsule_id."""
        assert planner_capsule["capsule_id"] != dj_capsule["capsule_id"]

    def test_different_operators(
        self, planner_capsule: dict, dj_capsule: dict
    ) -> None:
        assert planner_capsule["operator"] != dj_capsule["operator"]

    def test_both_verify_ok(self, planner_capsule: dict, dj_capsule: dict) -> None:
        for cap, label in [(planner_capsule, "planner"), (dj_capsule, "dj")]:
            vr = verify(cap)
            assert vr.ok, f"{label} capsule failed verify: {[f.detail for f in vr.findings]}"


class TestLedger:
    def test_ledger_has_two_capsules(
        self, planner_capsule: dict, dj_capsule: dict, ledger: Path
    ) -> None:
        from capsule_emit import read_ledger
        records = read_ledger(ledger)
        assert len(records) == 2

    def test_ledger_capsule_ids_match(
        self, planner_capsule: dict, dj_capsule: dict, ledger: Path
    ) -> None:
        from capsule_emit import read_ledger
        records = read_ledger(ledger)
        stored_ids = {r["capsule_id"] for r in records}
        assert planner_capsule["capsule_id"] in stored_ids
        assert dj_capsule["capsule_id"] in stored_ids


class TestDispositionVocab:
    """The mutual disposition vocab: executed, blocked, denied, timeout,
    errored, deferred, expired, escalated."""

    @pytest.mark.parametrize("verdict", [
        "executed", "blocked", "denied", "timeout",
        "errored", "deferred", "expired", "escalated",
    ])
    def test_verdict_class_accepted(self, verdict: str, ledger: Path) -> None:
        """All mutual vocab values can be used without raising InvariantError."""
        from agent_action_capsule.contracts import (
            NEVER_DISPATCH_VERDICT_CLASSES,
            Disposition,
        )
        from agent_action_capsule.contracts import (
            EffectRecord as _ER,
        )
        from agent_action_capsule.emit import emit as aac_emit

        # Non-dispatching verdicts cannot carry a dispatched/confirmed effect
        needs_no_effect = verdict in NEVER_DISPATCH_VERDICT_CLASSES
        dispo = Disposition(
            decision="accept" if verdict not in ("blocked", "denied") else "reject",
            approver="policy",
            verdict_class=verdict,
        )
        cap = aac_emit(
            action_type="decide",
            operator="test-org",
            developer="test-agent@v1",
            tool_name="test_action",
            disposition=dispo,
            effect=_ER(type="test", status="dispatched") if not needs_no_effect else None,
        )
        vr = verify(cap)
        assert vr.ok, f"verdict={verdict} failed verify: {[f.detail for f in vr.findings]}"
