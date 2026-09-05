# SPDX-License-Identifier: Apache-2.0
"""Tests for E14's ``chain_segment`` subject — ``capsule_emit.chain_segment``
and its wiring into ``capsule_emit.evidence_request.answer()``.

Uses ``CAPSULE_WITNESS=stub`` (zero-network, real checkpoint mechanics —
same harness ``tests/test_evidence_request.py`` uses) so these run
hermetically; every negative case flips exactly one thing and confirms the
mutant is caught, per QUEUE_PROTOCOL §7.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from capsule_emit import seal, signing, witness
from capsule_emit.adjudication import (
    VERDICT_CORROBORATED,
    VERDICT_INCONCLUSIVE,
    contradicted,
    seal_adjudication,
)
from capsule_emit.chain_segment import (
    ChainSegment,
    ChainSegmentError,
    chain_segment,
    verify_chain_segment,
)
from capsule_emit.evidence_request import (
    REASON_COVERAGE_UNSATISFIABLE,
    REASON_REQUEST_MALFORMED,
    Artifact,
    Refusal,
    answer,
)
from capsule_emit.ledger import read_ledger_entries


@pytest.fixture(autouse=True)
def _clean_witness_state():
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False
    yield
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False


@pytest.fixture
def stub_witness(monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS", "stub")


@pytest.fixture
def three_checkpoint_ledger(tmp_path, stub_witness):
    """Three checkpoints, two capsules apiece: cp1 (mmr covers 2 leaves),
    cp2 (2 more), cp3 (2 more) — every link in range."""
    ledger_path = tmp_path / "ledger.jsonl"
    checkpoints = []
    for batch in range(3):
        for i in range(2):
            seal(None, action=f"batch{batch}-{i}", operator="acme", anchor=False, ledger=ledger_path)
        cp = witness.push(str(ledger_path))
        assert cp is not None
        checkpoints.append(cp)
    return ledger_path, checkpoints


def _entries(ledger_path):
    return read_ledger_entries(ledger_path)


# ---------------------------------------------------------------------------
# chain_segment() — building the artifact
# ---------------------------------------------------------------------------


def test_from_genesis_to_latest_covers_every_checkpoint(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), from_size=0, to_size=checkpoints[-1].mmr_size)
    assert [link.checkpoint.mmr_size for link in seg.links] == [cp.mmr_size for cp in checkpoints]
    assert seg.links[0].consistency_proof is None  # segment start boundary — nothing before it in scope
    assert all(link.consistency_proof is not None for link in seg.links[1:])


def test_from_size_matching_a_checkpoint_starts_there(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), from_size=checkpoints[0].mmr_size, to_size=checkpoints[-1].mmr_size)
    assert [link.checkpoint.mmr_size for link in seg.links] == [cp.mmr_size for cp in checkpoints[0:]]
    # the FIRST link is now the boundary itself — its own consistency proof
    # (against whatever came before IT) is out of this segment's scope.
    assert seg.links[0].checkpoint.mmr_size == checkpoints[0].mmr_size
    assert seg.links[0].consistency_proof is None


def test_last_n_returns_the_final_n_checkpoints(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), last=2)
    assert [link.checkpoint.mmr_size for link in seg.links] == [cp.mmr_size for cp in checkpoints[-2:]]


def test_last_beyond_available_count_serves_everything_it_has(three_checkpoint_ledger):
    """A young log is never refused just because it hasn't reached the
    requested depth — 'last: 50' on a 3-checkpoint log serves all 3."""
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), last=50)
    assert [link.checkpoint.mmr_size for link in seg.links] == [cp.mmr_size for cp in checkpoints]


# ---------------------------------------------------------------------------
# chain_segment() — refusing beyond the log's size / unknown boundaries
# ---------------------------------------------------------------------------


def test_to_size_beyond_latest_checkpoint_is_refused(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    with pytest.raises(ChainSegmentError, match="beyond"):
        chain_segment(_entries(ledger_path), from_size=0, to_size=checkpoints[-1].mmr_size + 1000)


def test_to_size_matching_no_checkpoint_is_refused(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    # A size strictly between two real checkpoints was never itself signed.
    bad_to_size = checkpoints[0].mmr_size + 1
    assert bad_to_size < checkpoints[-1].mmr_size
    with pytest.raises(ChainSegmentError, match="does not match"):
        chain_segment(_entries(ledger_path), from_size=0, to_size=bad_to_size)


def test_from_size_matching_no_checkpoint_is_refused(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    with pytest.raises(ChainSegmentError, match="does not match"):
        chain_segment(_entries(ledger_path), from_size=checkpoints[0].mmr_size + 1, to_size=checkpoints[-1].mmr_size)


def test_from_after_to_is_refused(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    with pytest.raises(ChainSegmentError):
        chain_segment(_entries(ledger_path), from_size=checkpoints[-1].mmr_size, to_size=checkpoints[0].mmr_size)


def test_no_checkpoints_yet_is_refused(tmp_path, stub_witness):
    ledger_path = tmp_path / "ledger.jsonl"
    seal(None, action="uncovered", operator="acme", anchor=False, ledger=ledger_path)
    with pytest.raises(ChainSegmentError):
        chain_segment(_entries(ledger_path), last=1)


def test_last_and_from_to_are_mutually_exclusive(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    with pytest.raises(ChainSegmentError):
        chain_segment(_entries(ledger_path), last=1, from_size=0, to_size=checkpoints[-1].mmr_size)


# ---------------------------------------------------------------------------
# Leaf counts by kind, and adjudication verdict/role split
# ---------------------------------------------------------------------------


def test_leaf_counts_classify_stamp_and_capsule(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), from_size=0, to_size=checkpoints[-1].mmr_size)
    # cp1 covers only its own 2 fresh capsules (no prior stamp to count).
    assert seg.links[0].leaf_counts == {"capsule": 2}
    # cp2 and cp3 each additionally cover the PRIOR checkpoint's own stamp entry.
    assert seg.links[1].leaf_counts == {"stamp": 1, "capsule": 2}
    assert seg.links[2].leaf_counts == {"stamp": 1, "capsule": 2}


def test_adjudication_verdict_role_split(tmp_path, stub_witness):
    ledger_path = tmp_path / "ledger.jsonl"
    a = seal(None, action="a", operator="acme", anchor=False, ledger=ledger_path).capsule
    b = seal(None, action="b", operator="acme", anchor=False, ledger=ledger_path).capsule
    self_id = signing.resolve_signer(str(ledger_path)).key_id

    seal_adjudication(a["capsule_id"], b["capsule_id"], VERDICT_CORROBORATED, margin=1.0, margin_tau=0.9, ledger=ledger_path)
    seal_adjudication(a["capsule_id"], b["capsule_id"], contradicted(self_id), margin=0.5, margin_tau=0.9, ledger=ledger_path)
    seal_adjudication(a["capsule_id"], b["capsule_id"], contradicted("some-other-node"), margin=0.5, margin_tau=0.9, ledger=ledger_path)
    seal_adjudication(a["capsule_id"], b["capsule_id"], VERDICT_INCONCLUSIVE, margin=0.5, margin_tau=0.9, ledger=ledger_path)
    cp = witness.push(str(ledger_path))

    seg = chain_segment(_entries(ledger_path), from_size=0, to_size=cp.mmr_size, self_owner_id=self_id)
    assert seg.links[0].leaf_counts["adjudication"] == 4
    assert seg.links[0].adjudication == {
        "corroborated": 1,
        "contradicted_self": 1,
        "contradicted_other": 1,
        "inconclusive": 1,
    }


def test_adjudication_with_no_self_owner_id_counts_as_other(tmp_path, stub_witness):
    ledger_path = tmp_path / "ledger.jsonl"
    a = seal(None, action="a", operator="acme", anchor=False, ledger=ledger_path).capsule
    b = seal(None, action="b", operator="acme", anchor=False, ledger=ledger_path).capsule
    seal_adjudication(a["capsule_id"], b["capsule_id"], contradicted("someone"), margin=0.5, margin_tau=0.9, ledger=ledger_path)
    cp = witness.push(str(ledger_path))

    seg = chain_segment(_entries(ledger_path), from_size=0, to_size=cp.mmr_size)
    assert seg.links[0].adjudication == {
        "corroborated": 0,
        "contradicted_self": 0,
        "contradicted_other": 1,
        "inconclusive": 0,
    }


def test_leaf_digests_flag(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    entries = _entries(ledger_path)

    without = chain_segment(entries, from_size=0, to_size=checkpoints[0].mmr_size)
    assert without.links[0].leaf_digests is None

    with_digests = chain_segment(entries, from_size=0, to_size=checkpoints[0].mmr_size, leaf_digests=True)
    assert with_digests.links[0].leaf_digests is not None
    assert len(with_digests.links[0].leaf_digests) == 2


def test_custom_classify_overrides_default_vocabulary(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(
        _entries(ledger_path),
        from_size=0,
        to_size=checkpoints[0].mmr_size,
        classify=lambda e: "exchange",
    )
    assert seg.links[0].leaf_counts == {"exchange": 2}


# ---------------------------------------------------------------------------
# to_dict/from_dict round-trip
# ---------------------------------------------------------------------------


def test_round_trip_to_dict_from_dict(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), from_size=0, to_size=checkpoints[-1].mmr_size, leaf_digests=True)
    rebuilt = ChainSegment.from_dict(seg.to_dict())
    assert rebuilt.to_dict() == seg.to_dict()


# ---------------------------------------------------------------------------
# verify_chain_segment() — offline verification, and the two ACCEPTANCE mutants
# ---------------------------------------------------------------------------


def test_verify_chain_segment_ok_renders_depth_continuity_witnessed(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), from_size=0, to_size=checkpoints[-1].mmr_size)
    result = verify_chain_segment(seg)
    assert result.ok
    assert result.continuity == "unbroken"
    assert result.history_depth == 3
    assert result.witnessed == "0/3"  # stub stamps never grade WITNESSED


def test_mutant_drop_middle_checkpoint_breaks_continuity(three_checkpoint_ledger):
    """ACCEPTANCE mutant: drop one checkpoint from the middle -> the
    consistency chain breaks at that link, never silently re-links."""
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), from_size=0, to_size=checkpoints[-1].mmr_size)
    assert len(seg.links) == 3

    tampered = ChainSegment(v=seg.v, links=(seg.links[0], seg.links[2]))
    result = verify_chain_segment(tampered)
    assert not result.ok
    assert "broken at" in result.continuity
    assert f"mmr_size={checkpoints[2].mmr_size}" in result.continuity
    assert result.history_depth == 1  # only the first (unbroken) link counted


def test_mutant_tamper_prev_root_breaks_the_link(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    seg = chain_segment(_entries(ledger_path), from_size=0, to_size=checkpoints[-1].mmr_size)

    tampered_cp = dataclasses.replace(seg.links[1].checkpoint, prev_root="00" * 32)
    tampered_link = dataclasses.replace(seg.links[1], checkpoint=tampered_cp)
    tampered = ChainSegment(v=seg.v, links=(seg.links[0], tampered_link, seg.links[2]))

    result = verify_chain_segment(tampered)
    assert not result.ok
    assert "broken at" in result.continuity


def test_empty_segment_is_never_ok():
    result = verify_chain_segment(ChainSegment(v=1, links=()))
    assert not result.ok
    assert result.history_depth == 0


# ---------------------------------------------------------------------------
# evidence_request.answer() — the chain_segment subject end to end
# ---------------------------------------------------------------------------


def _chain_segment_request(**subject_extra) -> bytes:
    return json.dumps({"subject": {"kind": "chain_segment", **subject_extra}, "coverage": {}}).encode()


def test_answer_chain_segment_last(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    result = answer(_chain_segment_request(last=50), ledger=ledger_path)
    assert isinstance(result, Artifact)
    assert result.subject_kind == "chain_segment"
    assert len(result.bundles) == 1
    assert len(result.bundles[0].links) == 3


def test_answer_chain_segment_beyond_log_size_is_coverage_unsatisfiable(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    request = _chain_segment_request(from_size=0, to_size=checkpoints[-1].mmr_size + 1000)
    result = answer(request, ledger=ledger_path)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_COVERAGE_UNSATISFIABLE


@pytest.mark.parametrize(
    "subject_extra",
    [
        {"last": 1, "from_size": 0, "to_size": 1},  # both shapes at once
        {},  # neither shape
        {"last": 0},  # non-positive
        {"last": -1},
        {"from_size": -1, "to_size": 5},  # negative
        {"from_size": 5, "to_size": 1},  # to < from
        {"last": "5"},  # wrong type
    ],
)
def test_answer_chain_segment_malformed_subject_is_request_malformed(three_checkpoint_ledger, subject_extra):
    ledger_path, _checkpoints = three_checkpoint_ledger
    result = answer(_chain_segment_request(**subject_extra), ledger=ledger_path)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_REQUEST_MALFORMED


def test_answer_chain_segment_no_checkpoints_yet_is_no_such_record(tmp_path, stub_witness):
    ledger_path = tmp_path / "ledger.jsonl"
    seal(None, action="uncovered", operator="acme", anchor=False, ledger=ledger_path)
    result = answer(_chain_segment_request(last=1), ledger=ledger_path)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_COVERAGE_UNSATISFIABLE


def test_caller_invariance_holds_for_chain_segment(three_checkpoint_ledger):
    """ACCEPTANCE: two askers -> byte-identical, regardless of nonce."""
    ledger_path, checkpoints = three_checkpoint_ledger
    now = "2026-09-05T00:00:00Z"
    request_a = json.dumps(
        {"subject": {"kind": "chain_segment", "last": 50}, "coverage": {}, "nonce": "n1"}
    ).encode()
    request_b = json.dumps(
        {"subject": {"kind": "chain_segment", "last": 50}, "coverage": {}, "nonce": "n2"}
    ).encode()
    result_a = answer(request_a, ledger=ledger_path, now=now)
    result_b = answer(request_b, ledger=ledger_path, now=now)
    assert isinstance(result_a, Artifact) and isinstance(result_b, Artifact)
    assert json.dumps(result_a.to_dict(), sort_keys=True) == json.dumps(result_b.to_dict(), sort_keys=True)


def test_expected_pin_reuses_generic_resolution_against_last_checkpoint(three_checkpoint_ledger):
    ledger_path, checkpoints = three_checkpoint_ledger
    latest = checkpoints[-1]

    correct = answer(
        json.dumps(
            {
                "subject": {"kind": "chain_segment", "last": 50},
                "coverage": {"expected_pin": {"mmr_size": latest.mmr_size, "root": latest.root}},
            }
        ).encode(),
        ledger=ledger_path,
    )
    assert isinstance(correct, Artifact)

    wrong = answer(
        json.dumps(
            {
                "subject": {"kind": "chain_segment", "last": 50},
                "coverage": {"expected_pin": {"mmr_size": latest.mmr_size, "root": "00" * 32}},
            }
        ).encode(),
        ledger=ledger_path,
    )
    assert isinstance(wrong, Refusal)
    assert wrong.reason == REASON_COVERAGE_UNSATISFIABLE
