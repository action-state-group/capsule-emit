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

import capsule_emit.evidence_request as evidence_request
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
# Caps and paging — the mutant this task exists to catch: a range answer
# must never return the whole ledger unbounded.
# ---------------------------------------------------------------------------


@pytest.fixture
def five_capsule_ledger(tmp_path, stub_witness):
    ledger_path = tmp_path / "ledger.jsonl"
    caps = [seal(None, action=f"act-{i}", operator="acme", anchor=False, ledger=ledger_path).capsule for i in range(5)]
    cp = witness.push(str(ledger_path))
    assert cp is not None
    return ledger_path, [c["capsule_id"] for c in caps]


def _range_request(selector: str, **page_or_coverage) -> bytes:
    page = page_or_coverage.pop("page", None)
    body = {"subject": {"kind": "range", "selector": selector}, "coverage": page_or_coverage}
    if page is not None:
        body["page"] = page
    return json.dumps(body).encode()


def test_range_default_page_size_caps_and_pages(monkeypatch, five_capsule_ledger):
    monkeypatch.setattr(evidence_request, "DEFAULT_PAGE_SIZE", 2)
    ledger_path, cids = five_capsule_ledger
    selector = f"{cids[0]}..{cids[-1]}"

    seen: list[str] = []
    token = None
    pages = 0
    while True:
        page = {"token": token} if token is not None else {}
        result = answer(_range_request(selector, page=page), ledger=ledger_path)
        assert isinstance(result, Artifact)
        assert len(result.bundles) <= 2
        seen.extend(b.capsule_id for b in result.bundles)
        pages += 1
        token = result.next_page_token
        if token is None:
            break
        assert pages < 10  # guard against an infinite loop if paging regresses

    assert pages == 3  # 2 + 2 + 1
    assert seen == cids  # every capsule, in order, no duplicates, none dropped


def test_range_explicit_page_size_capped_at_max(monkeypatch, five_capsule_ledger):
    monkeypatch.setattr(evidence_request, "MAX_PAGE_SIZE", 3)
    ledger_path, cids = five_capsule_ledger
    selector = f"{cids[0]}..{cids[-1]}"

    result = answer(_range_request(selector, page={"size": 100}), ledger=ledger_path)
    assert isinstance(result, Artifact)
    assert len(result.bundles) == 3  # clamped to MAX_PAGE_SIZE despite the client asking for 100
    assert result.next_page_token is not None


def test_range_page_token_malformed_is_request_malformed(five_capsule_ledger):
    ledger_path, cids = five_capsule_ledger
    selector = f"{cids[0]}..{cids[-1]}"

    result = answer(_range_request(selector, page={"token": "not-a-number"}), ledger=ledger_path)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_REQUEST_MALFORMED


def test_range_within_one_page_carries_no_next_page_token(covered_ledger):
    ledger_path, caps = covered_ledger
    selector = f"{caps[0]['capsule_id']}..{caps[1]['capsule_id']}"
    result = answer(_range_request(selector), ledger=ledger_path)
    assert isinstance(result, Artifact)
    assert len(result.bundles) == 2
    assert result.next_page_token is None


def test_record_subject_ignores_page_field(covered_ledger):
    ledger_path, caps = covered_ledger
    cid = caps[0]["capsule_id"]
    request = json.dumps(
        {"subject": {"kind": "record", "capsule_id": cid}, "coverage": {}, "page": {"size": 1, "token": "0"}}
    ).encode()
    result = answer(request, ledger=ledger_path)
    assert isinstance(result, Artifact)
    assert len(result.bundles) == 1
    assert result.next_page_token is None


def test_range_paging_caller_invariance(monkeypatch, five_capsule_ledger):
    monkeypatch.setattr(evidence_request, "DEFAULT_PAGE_SIZE", 2)
    ledger_path, cids = five_capsule_ledger
    selector = f"{cids[0]}..{cids[-1]}"
    now = "2026-09-05T00:00:00Z"

    request_a = json.dumps(
        {"subject": {"kind": "range", "selector": selector}, "coverage": {}, "page": {"token": "2"}, "nonce": "a"}
    ).encode()
    request_b = json.dumps(
        {"subject": {"kind": "range", "selector": selector}, "coverage": {}, "page": {"token": "2"}, "nonce": "b"}
    ).encode()

    result_a = answer(request_a, ledger=ledger_path, now=now)
    result_b = answer(request_b, ledger=ledger_path, now=now)
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
# AND the responding node opted in via allow_forced_checkpoint (default off)
# ---------------------------------------------------------------------------


def _stamp_count(ledger_path) -> int:
    from capsule_emit.ledger import CHECKPOINT_STAMP_KIND, read_ledger_entries

    entries = read_ledger_entries(ledger_path)
    return sum(1 for e in entries if e.get("kind") == CHECKPOINT_STAMP_KIND)


def test_min_freshness_with_deadline_and_explicit_opt_in_pushes_and_succeeds(tmp_path, stub_witness):
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
    result = answer(request, ledger=ledger_path, allow_forced_checkpoint=True)
    assert isinstance(result, Artifact), result
    assert result.bundles[0].capsule_id == cid
    assert _stamp_count(ledger_path) == 1


def test_min_freshness_with_deadline_but_no_opt_in_refuses_without_pushing(tmp_path, stub_witness):
    """The mutant this task exists to catch: a coverage/min_freshness
    request against a pull-only door must never force a WRITE just because
    the requester supplied a deadline -- the node's own
    allow_forced_checkpoint opt-in (default False) is required too."""
    ledger_path = tmp_path / "ledger.jsonl"
    cap = seal(None, action="uncovered", operator="acme", anchor=False, ledger=ledger_path).capsule
    cid = cap["capsule_id"]

    request = json.dumps(
        {
            "subject": {"kind": "record", "capsule_id": cid},
            "coverage": {"min_freshness": {"max_age_seconds": 60}},
            "deadline": 30,
        }
    ).encode()
    result = answer(request, ledger=ledger_path)  # allow_forced_checkpoint defaults False
    assert isinstance(result, Refusal)
    assert result.reason == REASON_COVERAGE_UNSATISFIABLE
    assert _stamp_count(ledger_path) == 0


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
    result = answer(request, ledger=ledger_path, allow_forced_checkpoint=True)
    assert isinstance(result, Refusal)
    assert result.reason == REASON_COVERAGE_UNSATISFIABLE

    # Confirm no checkpoint was forced as a side effect of the refused call
    # -- absent deadline, even an opted-in node never pushes.
    assert _stamp_count(ledger_path) == 0


# ---------------------------------------------------------------------------
# Refusal object shape — never an unsigned 404
# ---------------------------------------------------------------------------


def test_refusal_carries_signed_shape_not_a_bare_dict(covered_ledger):
    ledger_path, _caps = covered_ledger
    result = answer(_record_request("ff" * 32), ledger=ledger_path)
    d = result.to_dict()
    assert set(d.keys()) == {"request_digest", "reason", "issued_at", "key_id", "sig"}
    assert d["sig"] and d["key_id"]
