# SPDX-License-Identifier: Apache-2.0
"""``LegacyLedger.load_cll``: a witnessed ``ledger.jsonl`` as CLL checkpoint
state, and agent-action-capsule's Evidence Bundle producer over it through
``proof_source_from_cll(ledger, ledger)``.

The ledgers are written by ``seal()``/``log()`` with the default witness
wiring against the stub Transparency Service of ``test_record_proof.py``, so
each checkpoint-stamp line carries the ``checkpoint_cose`` statement the
witness thread persisted.
"""
from __future__ import annotations

import json

import pytest
from agent_action_capsule.bundle import (
    BundleBuildError,
    build_evidence_bundle,
    proof_source_from_cll,
    verify_bundle,
)
from cll.log import CllState
from cll.log import CorruptError as LogCorruptError
from cll.log import UnsupportedError as LogUnsupportedError
from test_record_proof import _clean_witness_state, _stamp_count, _start_stub_ts, _wait_for  # noqa: F401

from capsule_emit import log, seal
from capsule_emit.legacy_ledger import LegacyLedger


@pytest.fixture
def witness_url(monkeypatch):
    """The stub Transparency Service's URL, as the default witness."""
    url, _, stop = _start_stub_ts()
    monkeypatch.setenv("CAPSULE_WITNESS_URL", url)
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    yield url
    stop()


def _seal(path, url, action, **kwargs):
    return seal(None, action=action, operator="acme", anchor=False, ledger=path, witness_url=url, **kwargs).capsule


@pytest.fixture
def witnessed(tmp_path, witness_url):
    """``ledger.jsonl``: c0, c1 (confirms c0), stamp 1, c2, c3 (confirms c2),
    stamp 2. Stamp 2's checkpoint covers the first five leaves."""
    url = witness_url
    path = tmp_path / "ledger.jsonl"
    c0 = _seal(path, url, "c0")
    c1 = _seal(path, url, "c1", confirms=c0["capsule_id"])
    assert _wait_for(lambda: _stamp_count(path) >= 1)
    c2 = _seal(path, url, "c2")
    c3 = _seal(path, url, "c3", confirms=c2["capsule_id"])
    assert _wait_for(lambda: _stamp_count(path) >= 2)
    return path, url, [c0, c1, c2, c3]


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write(path, lines):
    path.write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))


def _source(path):
    ledger = LegacyLedger(path)
    return proof_source_from_cll(ledger, ledger)


def test_load_cll_reads_the_newest_stamp_as_checkpoint_state(witnessed):
    from cll.checkpoint import verify_checkpoint_cose_offline

    path, _, _ = witnessed
    lines = _lines(path)
    assert [line.get("kind") for line in lines][2::3] == ["checkpoint_stamp", "checkpoint_stamp"]
    state = LegacyLedger(path).load_cll()
    assert state.checkpoint.bytes == bytes.fromhex(lines[5]["checkpoint_cose"])
    assert state.checkpoint.indexed_seq == state.indexed_seq == 5
    assert state.checkpoint.size == state.size == len(state.nodes) == 8
    signed = verify_checkpoint_cose_offline(state.checkpoint.bytes).decoded
    assert [p.hex() for p in state.checkpoint.peaks] == [bytes(p).hex() for p in signed.new_peak_hashes]


def test_a_range_after_the_stamp_leaf_is_valid(witnessed):
    path, _, caps = witnessed
    bundle = build_evidence_bundle(_source(path), root=caps[3]["capsule_id"], interval_from=4)
    certificate = bundle["completeness_certificate"]
    assert (certificate["first_seq"], certificate["last_seq"]) == (4, 5)
    assert [r["capsule_id"] for r in bundle["records"]] == [caps[2]["capsule_id"], caps[3]["capsule_id"]]
    assert all(r["signature"] and r["key_id"] for r in bundle["records"])
    assert verify_bundle(bundle, strict=True).verdict == "VALID"


def test_a_range_over_a_stamp_leaf_is_refused(witnessed):
    path, _, caps = witnessed
    with pytest.raises(BundleBuildError, match="seq 3 in the interval is not a record"):
        build_evidence_bundle(_source(path), root=caps[3]["capsule_id"], interval_from=1)


def test_without_a_range_the_bundle_is_producer_selected_and_incomplete(witnessed):
    path, _, caps = witnessed
    bundle = build_evidence_bundle(_source(path), root=caps[1]["capsule_id"], interval_from=None)
    assert bundle["completeness"]["selection"] == "producer-selected"
    assert [r["capsule_id"] for r in bundle["records"]] == [caps[0]["capsule_id"], caps[1]["capsule_id"]]
    assert verify_bundle(bundle, strict=True).verdict == "INCOMPLETE"


def test_a_record_past_the_checkpoint_is_refused(witnessed):
    path, url, caps = witnessed
    tail = _seal(path, url, "tail", witness=False)
    source = _source(path)
    with pytest.raises(BundleBuildError, match="not a leaf at or below the checkpoint"):
        build_evidence_bundle(source, root=tail["capsule_id"], interval_from=None)
    # The covered suffix still proves: the range ends at the checkpoint tip.
    bundle = build_evidence_bundle(source, root=caps[3]["capsule_id"], interval_from=4)
    assert verify_bundle(bundle, strict=True).verdict == "VALID"


def test_an_unsigned_record_in_the_range_is_incomplete(tmp_path, witness_url):
    url = witness_url
    path = tmp_path / "ledger.jsonl"
    _seal(path, url, "c0")
    _seal(path, url, "c1")
    assert _wait_for(lambda: _stamp_count(path) >= 1)
    entry = log(b"an unsigned artifact", ledger=path, witness_url=url)
    signed = _seal(path, url, "c3")
    assert _wait_for(lambda: _stamp_count(path) >= 2)

    bundle = build_evidence_bundle(_source(path), root=signed["capsule_id"], interval_from=4)
    unsigned = next(r for r in bundle["records"] if r["capsule_id"] == entry.capsule_id)
    assert "signature" not in unsigned and "key_id" not in unsigned
    assert verify_bundle(bundle, strict=True).verdict == "INCOMPLETE"


def test_stamps_without_cose_leave_no_checkpoint(witnessed):
    path, _, caps = witnessed
    lines = _lines(path)
    for line in lines:
        line.pop("checkpoint_cose", None)
    _write(path, lines)
    state = LegacyLedger(path).load_cll()
    assert state == CllState()
    with pytest.raises(BundleBuildError, match="no checkpoint"):
        build_evidence_bundle(_source(path), root=caps[3]["capsule_id"], interval_from=4)


def test_a_stamp_whose_cose_does_not_verify_is_passed_over(witnessed):
    path, _, _ = witnessed
    lines = _lines(path)
    cose = lines[5]["checkpoint_cose"]
    lines[5]["checkpoint_cose"] = cose[:-2] + ("00" if cose[-2:] != "00" else "01")
    _write(path, lines)
    state = LegacyLedger(path).load_cll()
    assert state.checkpoint.bytes == bytes.fromhex(lines[2]["checkpoint_cose"])
    assert state.checkpoint.indexed_seq == 2


def test_entries_that_do_not_rebuild_the_signed_root_are_corrupt(witnessed):
    path, _, _ = witnessed
    lines = _lines(path)
    _write(path, [lines[1], lines[0], *lines[2:]])
    with pytest.raises(LogCorruptError, match="do not rebuild the root"):
        LegacyLedger(path).load_cll()


def test_commit_cll_is_unsupported(witnessed):
    path, _, _ = witnessed
    ledger = LegacyLedger(path)
    with pytest.raises(LogUnsupportedError):
        ledger.commit_cll(0, None, CllState())
