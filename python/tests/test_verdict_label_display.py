# SPDX-License-Identifier: Apache-2.0
"""Adjudications and log entries carry no verdict_class, and their decision is
always "accept". The three readers that fall back to the decision (viewer,
evidence table, permalink summary) must label them by record type instead,
never "accept"."""
from __future__ import annotations

import io

import pytest

from capsule_emit import log
from capsule_emit.adjudication import contradicted, seal_adjudication
from capsule_emit.core import _emit_capsule
from capsule_emit.display import verdict_label
from capsule_emit.evidence import _row as evidence_row
from capsule_emit.permalink import summarize
from capsule_emit.viewer import render_html, render_table

CONTRADICTED = contradicted("owner-b")


@pytest.fixture(scope="module")
def records(tmp_path_factory):
    ledger = tmp_path_factory.mktemp("display") / "ledger.jsonl"
    half_a = _emit_capsule(action="serve_a", operator="example-org", developer="agent@v1",
                           anchor=False, ledger=ledger)
    half_b = _emit_capsule(action="serve_b", operator="example-org", developer="agent@v1",
                           anchor=False, ledger=ledger)
    adjudication = seal_adjudication(
        half_a.capsule_id, half_b.capsule_id, CONTRADICTED,
        margin=0.5, margin_tau=0.9, ledger=ledger, operator="example-org", developer="referee@v1",
    ).capsule
    log_entry = log(b"an event", ledger=ledger, witness=False).capsule
    plain = half_a.capsule
    for cap in (adjudication, log_entry):
        assert "verdict_class" not in cap["disposition"]
        assert cap["disposition"]["decision"] == "accept"
    return {"adjudication": adjudication, "log": log_entry, "plain": plain}


EXPECTED = {"adjudication": CONTRADICTED, "log": "logged"}


@pytest.mark.parametrize("kind", ["adjudication", "log"])
def test_viewer_labels_by_record_type(records, kind):
    html = render_html([records[kind]])
    assert EXPECTED[kind] in html
    assert ">accept<" not in html
    out = io.StringIO()
    render_table([records[kind]], out=out)
    assert "accept" not in out.getvalue()


@pytest.mark.parametrize("kind", ["adjudication", "log"])
def test_evidence_row_labels_by_record_type(records, kind):
    row = evidence_row(1, records[kind])
    assert f"| {EXPECTED[kind]} |" in row
    assert "accept" not in row


@pytest.mark.parametrize("kind", ["adjudication", "log"])
def test_permalink_summary_labels_by_record_type(records, kind):
    line = summarize([records[kind]])
    assert EXPECTED[kind] in line
    assert "accept" not in line


def test_registered_verdict_class_still_wins(records):
    assert verdict_label(records["plain"]) == "executed"


def test_other_capsules_without_verdict_class_fall_back_to_decision(records):
    capsule = dict(records["plain"], disposition={"decision": "reject", "approver": "policy"})
    assert verdict_label(capsule) == "reject"


def test_log_label_is_never_written_to_the_capsule(records):
    assert "logged" not in str(records["log"])
