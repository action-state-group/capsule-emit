# SPDX-License-Identifier: Apache-2.0
"""Tests for E14 — ``capsule_emit.evidence_request.answer()``.

Uses ``CAPSULE_WITNESS=stub`` (zero-network, real checkpoint mechanics --
see ``tests/test_stub_witness.py``) so these run hermetically; every
negative case flips exactly one thing and confirms the mutant is caught,
per QUEUE_PROTOCOL §7.
"""
from __future__ import annotations

import json

import pytest

from capsule_emit import seal, witness
from capsule_emit.bundle import bundle as _bundle_fn
from capsule_emit.evidence_request import (
    REASON_COVERAGE_UNSATISFIABLE,
    REASON_NO_SUCH_RECORD,
    REASON_REQUEST_MALFORMED,
    Artifact,
    Refusal,
    RequestMalformedError,
    answer,
    parse_request,
    verify_refusal_offline,
)


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
def covered_ledger(tmp_path, stub_witness):
    """Two sealed capsules, both covered by one forced (stub) checkpoint."""
    ledger_path = tmp_path / "ledger.jsonl"
    caps = [seal(None, action=f"act-{i}", operator="acme", anchor=False, ledger=ledger_path).capsule for i in range(2)]
    cp = witness.push(str(ledger_path))
    assert cp is not None
    return ledger_path, caps


def _record_request(capsule_id: str, **coverage) -> bytes:
    return json.dumps({"subject": {"kind": "record", "capsule_id": capsule_id}, "coverage": coverage}).encode()


# ---------------------------------------------------------------------------
# parse_request — malformed detection
# ---------------------------------------------------------------------------


def test_parse_request_rejects_non_json():
    with pytest.raises(RequestMalformedError):
        parse_request(b"not json {{{")


def test_parse_request_rejects_missing_subject():
    with pytest.raises(RequestMalformedError):
        parse_request(json.dumps({"coverage": {}}).encode())


def test_parse_request_rejects_bad_subject_kind():
    with pytest.raises(RequestMalformedError):
        parse_request(json.dumps({"subject": {"kind": "not-a-real-kind"}}).encode())


def test_parse_request_ignores_unknown_fields():
    req = parse_request(
        json.dumps(
            {"subject": {"kind": "record", "capsule_id": "ab" * 32}, "coverage": {}, "unknown_field": "whatever"}
        ).encode()
    )
    assert req.subject["capsule_id"] == "ab" * 32


# ---------------------------------------------------------------------------
# answer() — malformed -> signed refusal, never an exception, never a 404
# ---------------------------------------------------------------------------


def test_answer_malformed_request_is_signed_refusal(covered_ledger):
    ledger_path, _caps = covered_ledger
    result = answer(b"not json {{{", ledger=ledger_path)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_REQUEST_MALFORMED
    assert verify_refusal_offline(result)


def test_answer_malformed_refusal_verifies_offline_and_tamper_fails(covered_ledger):
    ledger_path, _caps = covered_ledger
    result = answer(b"{}", ledger=ledger_path)
    assert result.reason == REASON_REQUEST_MALFORMED
    assert verify_refusal_offline(result)

    from dataclasses import replace

    tampered = replace(result, reason=REASON_COVERAGE_UNSATISFIABLE)
    assert not verify_refusal_offline(tampered)

    tampered_sig = replace(result, sig="00" * 64)
    assert not verify_refusal_offline(tampered_sig)


# ---------------------------------------------------------------------------
# answer() — record subject: the artifact path
# ---------------------------------------------------------------------------


def test_answer_record_returns_artifact_matching_bundle(covered_ledger):
    ledger_path, caps = covered_ledger
    cid = caps[0]["capsule_id"]
    result = answer(_record_request(cid), ledger=ledger_path)
    assert isinstance(result, Artifact)
    assert result.subject_kind == "record"
    assert len(result.bundles) == 1
    expected = _bundle_fn(ledger_path, cid)
    assert result.bundles[0].to_dict() == expected.to_dict()


def test_answer_no_such_record_is_recorded_absence(covered_ledger):
    ledger_path, _caps = covered_ledger
    result = answer(_record_request("ff" * 32), ledger=ledger_path)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_NO_SUCH_RECORD
    assert verify_refusal_offline(result)


# ---------------------------------------------------------------------------
# Caller invariance — the core test (QUEUE_PROTOCOL acceptance)
# ---------------------------------------------------------------------------


def test_caller_invariance_byte_identical_artifact_regardless_of_nonce(covered_ledger):
    ledger_path, caps = covered_ledger
    cid = caps[0]["capsule_id"]

    request_a = json.dumps(
        {"subject": {"kind": "record", "capsule_id": cid}, "coverage": {}, "nonce": "requester-a-nonce"}
    ).encode()
    request_b = json.dumps(
        {"subject": {"kind": "record", "capsule_id": cid}, "coverage": {}, "nonce": "requester-b-nonce"}
    ).encode()

    now = "2026-09-02T00:00:00Z"
    result_a = answer(request_a, ledger=ledger_path, now=now)
    result_b = answer(request_b, ledger=ledger_path, now=now)

    assert isinstance(result_a, Artifact) and isinstance(result_b, Artifact)
    assert json.dumps(result_a.to_dict(), sort_keys=True) == json.dumps(result_b.to_dict(), sort_keys=True)


def test_caller_invariance_holds_for_range_subject(covered_ledger):
    ledger_path, caps = covered_ledger
    selector = f"{caps[0]['capsule_id']}..{caps[1]['capsule_id']}"

    request_a = json.dumps(
        {"subject": {"kind": "range", "selector": selector}, "coverage": {}, "nonce": "n1"}
    ).encode()
    request_b = json.dumps(
        {"subject": {"kind": "range", "selector": selector}, "coverage": {}, "nonce": "n2"}
    ).encode()

    now = "2026-09-02T00:00:00Z"
    result_a = answer(request_a, ledger=ledger_path, now=now)
    result_b = answer(request_b, ledger=ledger_path, now=now)

    assert isinstance(result_a, Artifact) and isinstance(result_b, Artifact)
    assert len(result_a.bundles) == 2
    assert json.dumps(result_a.to_dict(), sort_keys=True) == json.dumps(result_b.to_dict(), sort_keys=True)


# ---------------------------------------------------------------------------
# Coverage: expected_pin — the requester's anchor must match exactly
# ---------------------------------------------------------------------------


def test_answer_wrong_pin_is_coverage_unsatisfiable(covered_ledger):
    ledger_path, caps = covered_ledger
    cid = caps[0]["capsule_id"]
    real = _bundle_fn(ledger_path, cid)

    wrong_root_request = _record_request(
        cid, expected_pin={"mmr_size": real.checkpoint.mmr_size, "root": "00" * 32}
    )
    result = answer(wrong_root_request, ledger=ledger_path)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_COVERAGE_UNSATISFIABLE
    assert verify_refusal_offline(result)

    wrong_size_request = _record_request(
        cid, expected_pin={"mmr_size": real.checkpoint.mmr_size + 1, "root": real.checkpoint.root}
    )
    result2 = answer(wrong_size_request, ledger=ledger_path)
    assert isinstance(result2, Refusal)
    assert result2.reason == REASON_COVERAGE_UNSATISFIABLE


def test_answer_correct_pin_succeeds(covered_ledger):
    ledger_path, caps = covered_ledger
    cid = caps[0]["capsule_id"]
    real = _bundle_fn(ledger_path, cid)

    request = _record_request(cid, expected_pin={"mmr_size": real.checkpoint.mmr_size, "root": real.checkpoint.root})
    result = answer(request, ledger=ledger_path)
    assert isinstance(result, Artifact)
    assert result.bundles[0].checkpoint.root == real.checkpoint.root


# ---------------------------------------------------------------------------
# Coverage: min_freshness — push() only when a deadline licenses the work
# ---------------------------------------------------------------------------


def test_min_freshness_with_deadline_pushes_and_succeeds(tmp_path, stub_witness):
    ledger_path = tmp_path / "ledger.jsonl"
    cap = seal(None, action="uncovered", operator="acme", anchor=False, ledger=ledger_path).capsule
    # No checkpoint has been forced yet -- the record is not yet covered.
    cid = cap["capsule_id"]

    request = json.dumps(
        {
            "subject": {"kind": "record", "capsule_id": cid},
            "coverage": {"min_freshness": {"max_age_seconds": 60}},
            "deadline": 30,
        }
    ).encode()
    result = answer(request, ledger=ledger_path)
    assert isinstance(result, Artifact), result
    assert result.bundles[0].capsule_id == cid


def test_min_freshness_without_deadline_refuses_without_pushing(tmp_path, stub_witness):
    ledger_path = tmp_path / "ledger.jsonl"
    cap = seal(None, action="uncovered", operator="acme", anchor=False, ledger=ledger_path).capsule
    cid = cap["capsule_id"]

    request = json.dumps(
        {
            "subject": {"kind": "record", "capsule_id": cid},
            "coverage": {"min_freshness": {"max_age_seconds": 60}},
        }
    ).encode()
    result = answer(request, ledger=ledger_path)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_COVERAGE_UNSATISFIABLE

    # Confirm no checkpoint was forced as a side effect of the refused call.
    from capsule_emit.ledger import CHECKPOINT_STAMP_KIND, read_ledger_entries

    entries = read_ledger_entries(ledger_path)
    assert not any(e.get("kind") == CHECKPOINT_STAMP_KIND for e in entries)


# ---------------------------------------------------------------------------
# Refusal object shape — never an unsigned 404
# ---------------------------------------------------------------------------


def test_refusal_carries_signed_shape_not_a_bare_dict(covered_ledger):
    ledger_path, _caps = covered_ledger
    result = answer(_record_request("ff" * 32), ledger=ledger_path)
    d = result.to_dict()
    assert set(d.keys()) == {"request_digest", "reason", "issued_at", "key_id", "sig"}
    assert d["sig"] and d["key_id"]
