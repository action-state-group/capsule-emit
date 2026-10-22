# SPDX-License-Identifier: Apache-2.0
"""Tests for the Verification-stage comment (capsule_emit.verification_comment
+ the ``evidence`` CLI).

Covers:
- markdown structure: title, Implements: line, per-capsule table rows, offline
  verify commands, viewer permalink (and its omission via viewer_link=False)
- the link's bundle: the caller's, said to carry a log proof when it has a
  checkpoint and refused when INVALID; else one with no log proof, said so
- fail-closed: an empty ledger and a tampered capsule both refuse
- honesty: attestation mode reported exactly as the capsules carry it
- chained ledgers bundle cleanly (relation="follows")
- CLI: exit 0 + markdown on stdout / --out file; exit 1 on tampered ledger
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capsule_emit import read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter
from capsule_emit.cli import main
from capsule_emit.verification_comment import VerificationCommentError, build_verification_comment

ISSUE = "https://github.com/example-org/example/issues/1"
BASE_URL = "https://verify.example"  # the verify surface these tests name (no default)


def _ledger(tmp_path: Path, *, chained: bool = True) -> Path:
    path = tmp_path / "ledger.jsonl"
    emitter = MCPCapsuleEmitter(
        operator="test-org",
        developer="goose-agent@v1",
        ledger=path,
        anchor=False,
    )
    r1 = emitter.emit_capsule(
        "run_repro",
        tool_input={"issue": ISSUE},
        tool_output={"exit_code": 1},
        action_type="fyi",
        effect={"status": "dispatched", "type": "reproduce"},
    )
    emitter.emit_capsule(
        "run_tests",
        tool_input={"issue": ISSUE},
        tool_output={"passed": 3, "failed": 0},
        action_type="decide",
        effect={"status": "dispatched", "type": "test_run"},
        prior_capsule_id=r1.capsule_id if chained else None,
        relation="follows" if chained else None,
    )
    return path


def test_markdown_structure(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    md = build_verification_comment(capsules, issue_url=ISSUE, base_url=BASE_URL)
    assert md.startswith("## Verification evidence")
    assert f"Implements: {ISSUE}" in md
    assert "| 1 | `run_repro` | fyi |" in md
    assert "| 2 | `run_tests` | decide |" in md
    assert "agent-action-capsule verify --store capsule-ledger" in md
    assert "capsule-emit verify --store capsule-ledger" in md
    assert "verify.example/bundle#" in md
    assert "this link carries no log proof" in md
    # short capsule_ids from the real ledger appear in the table
    for cap in capsules:
        assert f"`{cap['capsule_id'][:8]}`" in md


def test_no_issue_no_viewer_link(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    md = build_verification_comment(capsules, viewer_link=False)
    assert "Implements:" not in md
    assert "verify.agentactioncapsule.org" not in md
    # offline verify path remains — that is the substance
    assert "agent-action-capsule verify --store" in md


def test_attestation_mode_reported_as_carried(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    md = build_verification_comment(capsules, base_url=BASE_URL)
    assert "Attestation mode: self_attested" in md
    assert "adds no claims of its own" in md


def test_empty_refuses(tmp_path):
    with pytest.raises(VerificationCommentError, match="no capsules"):
        build_verification_comment([])


def test_tampered_refuses(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    tampered = [json.loads(json.dumps(c)) for c in capsules]
    tampered[0]["model_attestation"]["compute_attestation"]["agent_output_digest"] = "0" * 64
    with pytest.raises(VerificationCommentError, match="refusing to build a verification comment"):
        build_verification_comment(tampered)


def test_cli_stdout(tmp_path, capsys):
    path = _ledger(tmp_path)
    rc = main(["evidence", "--base-url", BASE_URL, "--ledger", str(path), "--issue", ISSUE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Verification evidence" in out
    assert ISSUE in out


def test_cli_out_file(tmp_path, capsys):
    path = _ledger(tmp_path)
    out_file = tmp_path / "verification-comment.md"
    rc = main(["evidence", "--base-url", BASE_URL, "--ledger", str(path), "--out", str(out_file)])
    assert rc == 0
    assert "VALID" in capsys.readouterr().out
    assert out_file.read_text().startswith("## Verification evidence")


def test_cli_tampered_exits_1(tmp_path, capsys):
    path = _ledger(tmp_path)
    capsules = read_ledger(path)
    capsules[0]["model_attestation"]["compute_attestation"]["agent_output_digest"] = "0" * 64
    bad = tmp_path / "tampered.jsonl"
    bad.write_text("\n".join(json.dumps(c) for c in capsules) + "\n")
    rc = main(["evidence", "--base-url", BASE_URL, "--ledger", str(bad)])
    assert rc == 1
    assert "refusing" in capsys.readouterr().err


def _linked_bundle(md: str) -> dict:
    from agent_action_capsule.bundle import decode_fragment

    url = md.split("verify viewer](", 1)[1].split(")", 1)[0]
    return decode_fragment(url.split("#", 1)[1])


def test_the_link_without_a_bundle_carries_the_capsules_unproven(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    linked = _linked_bundle(build_verification_comment(capsules, base_url=BASE_URL))
    assert [r["capsule_id"] for r in linked["records"]] == [c["capsule_id"] for c in capsules]
    assert linked["root"] == capsules[-1]["capsule_id"]
    assert "checkpoint" not in linked


def test_a_given_bundle_is_linked_as_it_is_and_its_log_proof_named(tmp_path):
    from agent_action_capsule.bundle import build_evidence_bundle, proof_source_from_cll
    from test_evidence_bundle import _mesh_ledger

    from capsule_emit.evidence_bundle import MeshLedgerLog

    d, _, capsules = _mesh_ledger(tmp_path, padding=False)
    log = MeshLedgerLog(d)
    bundle = build_evidence_bundle(proof_source_from_cll(log, log), root=capsules[-1]["capsule_id"], interval_from=1)
    md = build_verification_comment(capsules, bundle=bundle, base_url=BASE_URL)
    assert "this link carries a log proof under the log's signed checkpoint (VALID" in md
    assert _linked_bundle(md) == bundle


def test_an_invalid_bundle_is_refused(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    linked = _linked_bundle(build_verification_comment(capsules, base_url=BASE_URL))
    linked["records"][0]["action_type"] = "tampered"
    with pytest.raises(VerificationCommentError, match="INVALID"):
        build_verification_comment(capsules, bundle=linked, base_url=BASE_URL)


def test_cli_bundle_file(tmp_path, capsys):
    path = _ledger(tmp_path)
    capsules = read_ledger(path)
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps(_linked_bundle(build_verification_comment(capsules, base_url=BASE_URL))))
    rc = main(["evidence", "--base-url", BASE_URL, "--ledger", str(path), "--bundle", str(bundle_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "this link carries no log proof (INCOMPLETE under strict verification)" in out


def test_a_checkpoint_member_alone_is_not_a_log_proof(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    linked = _linked_bundle(build_verification_comment(capsules, base_url=BASE_URL))
    linked["checkpoint"] = {}
    md = build_verification_comment(capsules, bundle=linked, base_url=BASE_URL)
    assert "this link carries no log proof (INCOMPLETE under strict verification)" in md


def test_a_bundle_that_does_not_carry_the_listed_capsules_is_refused(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    foreign = _linked_bundle(build_verification_comment(read_ledger(_ledger(other)), base_url=BASE_URL))
    with pytest.raises(VerificationCommentError, match="does not carry"):
        build_verification_comment(capsules, bundle=foreign, base_url=BASE_URL)
    from agent_action_capsule.bundle import build_unproven_bundle

    partial = build_unproven_bundle(capsules[1:], root=capsules[-1]["capsule_id"], closure_depth=0)
    with pytest.raises(VerificationCommentError, match="does not carry 1 of"):
        build_verification_comment(capsules, bundle=partial, base_url=BASE_URL)
