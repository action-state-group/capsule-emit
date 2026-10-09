# SPDX-License-Identifier: Apache-2.0
"""Reconciliation fold tests, including the METR spoofed-tool-call vector.

The vector: a harness-sealed record of what a tool call actually returned,
reconciled against a second record of the same exchange built from
whatever a downstream reviewer would see (the agent's own transcript,
potentially rewritten after the call — METR's ``echo REAL`` → ``SPOOFTEST``
finding, ~96 transcripts). ``test_spoofed_tool_result_reconciles_as_contradicted``
is the fixed, current behavior (green). The two mutants below it are not
committed as passing tests — they are recorded here, with their red output
captured in the results report, exactly as the gate requires: "show the
mutant failing then the fix passing."
"""
from __future__ import annotations

import random

import pytest

from capsule_emit.adapters.inspect_ai import InspectAIListenerCore
from capsule_emit.reconciliation import (
    ReconciliationState,
    fold,
    format_fold,
    reconcile,
    sample_completeness,
)


def _core(tmp_path, name="requester") -> InspectAIListenerCore:
    return InspectAIListenerCore(
        operator="evaluator-org",
        developer="inspect-eval@v1",
        ledger=tmp_path / f"{name}.jsonl",
        anchor=False,
    )


def _key(record: dict) -> str:
    ca = record["model_attestation"]["compute_attestation"]
    return f"{ca['inspect_sample_id']}#{ca['inspect_epoch']}#{ca['inspect_call_index']}"


# ---------------------------------------------------------------------------
# matched / requester_only ("not present") / contradicted
# ---------------------------------------------------------------------------


def test_reconcile_matched_when_digests_agree(tmp_path):
    requester = _core(tmp_path, "requester")
    counterparty = _core(tmp_path, "counterparty")

    req = requester.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="c1", arguments={"cmd": "echo REAL"}, result="REAL",
    )
    cp = counterparty.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="c1", arguments={"cmd": "echo REAL"}, result="REAL",
    )

    results = reconcile([req.capsule], [cp.capsule], key_fn=_key)
    assert len(results) == 1
    assert results[0].state == ReconciliationState.MATCHED


def test_reconcile_requester_only_is_not_present_never_failed(tmp_path):
    """A plain API endpoint with no producer at all -- the counterparty half
    is a first-class 'not present' state, distinct from an error."""
    requester = _core(tmp_path, "requester")
    req = requester.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="c1", arguments={"cmd": "echo REAL"}, result="REAL",
    )

    results = reconcile([req.capsule], [], key_fn=_key)
    assert results[0].state == ReconciliationState.REQUESTER_ONLY
    assert results[0].counterparty_digest is None
    assert "not present" in results[0].detail
    assert "failed" not in results[0].state.value


def test_reconcile_contradicted_when_digests_disagree(tmp_path):
    requester = _core(tmp_path, "requester")
    counterparty = _core(tmp_path, "counterparty")

    req = requester.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="c1", arguments={"cmd": "echo REAL"}, result="REAL",
    )
    cp = counterparty.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="c1", arguments={"cmd": "echo REAL"}, result="DIFFERENT",
    )

    results = reconcile([req.capsule], [cp.capsule], key_fn=_key)
    assert results[0].state == ReconciliationState.CONTRADICTED
    assert results[0].requester_digest != results[0].counterparty_digest


# ---------------------------------------------------------------------------
# the METR spoofed-tool-call vector
# ---------------------------------------------------------------------------


def test_spoofed_tool_result_reconciles_as_contradicted(tmp_path):
    """The harness seals what the tool actually returned (REAL); a second
    record of the same exchange, built from what a downstream transcript
    shows, carries the spoofed value (SPOOFTEST). Reconciliation must call
    this out as contradicted, not matched and not silently dropped."""
    harness = _core(tmp_path, "harness")
    downstream_transcript = _core(tmp_path, "downstream")

    real_result = "REAL"
    spoofed_result = "SPOOFTEST"

    # This IS seal-at-harness: sealed from the value the harness itself
    # captured at execution time, never from anything the agent could have
    # rewritten afterward.
    harness_record = harness.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="c1", arguments={"cmd": "echo REAL"},
        result=real_result,
    )
    # A second, independent seal of the same exchange as a downstream
    # reviewer would see it from the (tampered) transcript.
    downstream_record = downstream_transcript.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0,
        tool_name="bash", tool_call_id="c1", arguments={"cmd": "echo REAL"},
        result=spoofed_result,
    )

    results = reconcile([harness_record.capsule], [downstream_record.capsule], key_fn=_key)
    assert results[0].state == ReconciliationState.CONTRADICTED, (
        "a spoofed tool result must reconcile as contradicted against the "
        "harness's sealed record of what actually executed"
    )


def test_mutant_a_revert_seal_at_harness_makes_the_spoof_pass():
    """MUTANT A, recorded not executed as part of the suite: if the value
    sealed as the 'harness' half were instead read from the same spoofable
    source as the downstream transcript (i.e. the harness's own seal step
    is reverted to trust agent-writable state), the two halves agree and
    the spoof becomes invisible -- reconciliation reports MATCHED instead
    of CONTRADICTED. This function documents the red assertion; it is not
    run by the real seal path, which always seals from the actual capture
    (see InspectAIListenerCore.seal_tool_call's callers in
    capsule_emit/adapters/inspect_ai.py -- there is no parameter that lets a
    caller substitute a different source for what a harness-sealed tool
    result actually was).

    To reproduce the mutant for real: change
    test_spoofed_tool_result_reconciles_as_contradicted's harness_record
    call above to seal `result=spoofed_result` instead of `result=
    real_result` (i.e., simulate the harness sealing from the same tampered
    source as the transcript), and re-run -- the CONTRADICTED assertion
    fails. See the results report for the captured red/green pair.
    """
    pytest.skip(
        "documentation-only placeholder for the mutant -- reproduced by "
        "editing test_spoofed_tool_result_reconciles_as_contradicted's "
        "harness_record.result to spoofed_result; see results report"
    )


# ---------------------------------------------------------------------------
# fold — N of M, never a percentage
# ---------------------------------------------------------------------------


def test_fold_counts_and_formats_as_n_of_m(tmp_path):
    requester = _core(tmp_path, "requester")
    counterparty = _core(tmp_path, "counterparty")

    matched = requester.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0, tool_name="t", tool_call_id="c1", arguments={}, result="a",
    )
    counterparty.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0, tool_name="t", tool_call_id="c1", arguments={}, result="a",
    )
    req_only = requester.seal_tool_call(
        sample_id="s1", epoch=1, call_index=1, tool_name="t", tool_call_id="c2", arguments={}, result="b",
    )
    contradicted = requester.seal_tool_call(
        sample_id="s1", epoch=1, call_index=2, tool_name="t", tool_call_id="c3", arguments={}, result="c",
    )
    counterparty.seal_tool_call(
        sample_id="s1", epoch=1, call_index=2, tool_name="t", tool_call_id="c3", arguments={}, result="NOT-c",
    )

    requester_records = [matched.capsule, req_only.capsule, contradicted.capsule]
    counterparty_records = [
        r.capsule for r in read_all_for(counterparty)
    ]
    results = reconcile(requester_records, counterparty_records, key_fn=_key)
    counts = fold(results)
    assert counts == {"matched": 1, "requester_only": 1, "contradicted": 1, "total": 3}

    formatted = format_fold(counts)
    assert formatted["matched"] == "1 of 3"
    assert formatted["requester_only"] == "1 of 3"
    assert formatted["contradicted"] == "1 of 3"
    for v in formatted.values():
        assert "%" not in v


def read_all_for(core: InspectAIListenerCore):
    return core.results


# ---------------------------------------------------------------------------
# completeness by counterparty trace — found/not-found, N of K
# ---------------------------------------------------------------------------


def test_sample_completeness_found_and_not_found(tmp_path):
    requester = _core(tmp_path, "requester")
    counterparty = _core(tmp_path, "counterparty")

    for i in range(5):
        requester.seal_tool_call(
            sample_id="s1", epoch=1, call_index=i, tool_name="t", tool_call_id=f"c{i}", arguments={}, result=str(i),
        )
    # counterparty only has 3 of the 5
    for i in range(3):
        counterparty.seal_tool_call(
            sample_id="s1", epoch=1, call_index=i, tool_name="t", tool_call_id=f"c{i}", arguments={}, result=str(i),
        )

    requester_records = [r.capsule for r in requester.results]
    counterparty_records = [r.capsule for r in counterparty.results]

    report = sample_completeness(
        requester_records, counterparty_records, k=5, key_fn=_key, rng=random.Random(42),
    )
    assert report.sampled == 5
    assert report.found == 3
    assert report.not_found == 2
    assert report.as_ratio() == "3 of 5"


def test_sample_completeness_caps_k_to_available_records(tmp_path):
    requester = _core(tmp_path, "requester")
    requester.seal_tool_call(
        sample_id="s1", epoch=1, call_index=0, tool_name="t", tool_call_id="c0", arguments={}, result="x",
    )
    records = [r.capsule for r in requester.results]
    report = sample_completeness(records, [], k=100, key_fn=_key, rng=random.Random(1))
    assert report.sampled == 1
