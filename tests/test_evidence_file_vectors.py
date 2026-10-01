# SPDX-License-Identifier: Apache-2.0
"""Evidence files saved by an independent producer on a live two-node run
(``test-vectors/evidence-file``): what ``check_evidence_file`` makes of them,
with today's bundle verifier and with one that accepts a range root bound to
the checkpoint by a consistency proof."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("cll.checkpoint.index")

from capsule_emit.evidence_file import check_evidence_file  # noqa: E402
from capsule_emit.evidence_report import render_report_html  # noqa: E402

VECTORS = Path(__file__).resolve().parents[1] / "test-vectors" / "evidence-file"
FILES = ["two-node-provider.json", "two-node-requester.json"]


@pytest.mark.parametrize("name", FILES)
def test_a_live_evidence_file(name):
    bundle = json.loads((VECTORS / name).read_text())
    assert "consistency_proof" in bundle["completeness_certificate"]
    check = check_evidence_file(bundle)
    status = {c.name: c for c in check.checks}

    # Whatever the bundle verifier: the record, its signature, closure and
    # the signed checkpoint all check, and the checkpoint's key signed it.
    for name_ in ("records", "signatures", "graph_closure", "checkpoint"):
        assert status[name_].status == "pass", (name_, status[name_].findings)
    assert check.checkpoint_authenticated and check.signer_matches_checkpoint is True
    assert [r.seq for r in check.records] == [1]

    interval, membership = status["interval_coverage"], status["per_record_membership"]
    if interval.status == "fail":
        # A verifier that only accepts a range root equal to the checkpoint's.
        assert interval.findings == membership.findings == ("completeness_certificate_invalid",)
        assert check.verdict == "INVALID"
    else:
        # A verifier that accepts the consistency-bound range.
        assert interval.status == membership.status == "pass"
        assert interval.findings == ("checkpoint_leaves_after_interval:31",)
        assert membership.findings == ()
        assert check.verdict == "VALID"


def test_the_report_renders_a_live_file():
    bundle = json.loads((VECTORS / FILES[0]).read_text())
    check = check_evidence_file(bundle)
    page = render_report_html(bundle, check, source=FILES[0])
    assert "Evidence report" in page and bundle["checkpoint"]["log_id"] in page
