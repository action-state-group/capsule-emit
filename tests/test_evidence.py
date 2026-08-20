# SPDX-License-Identifier: Apache-2.0
"""Tests for the Verification-evidence bundle (capsule_emit.evidence + CLI).

Covers:
- markdown structure: title, Implements: line, per-capsule table rows, offline
  verify commands, viewer permalink (and its omission via viewer_link=False)
- fail-closed: an empty ledger and a tampered capsule both refuse to bundle
- honesty: attestation mode reported exactly as the capsules carry it
- chained ledgers bundle cleanly (relation="sequence")
- CLI: exit 0 + markdown on stdout / --out file; exit 1 on tampered ledger
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capsule_emit import read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter
from capsule_emit.cli import main
from capsule_emit.evidence import EvidenceError, build_evidence_markdown

ISSUE = "https://github.com/example-org/example/issues/1"


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
        relation="sequence" if chained else None,
    )
    return path


def test_markdown_structure(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    md = build_evidence_markdown(capsules, issue_url=ISSUE)
    assert md.startswith("## Verification evidence")
    assert f"Implements: {ISSUE}" in md
    assert "| 1 | `run_repro` | fyi |" in md
    assert "| 2 | `run_tests` | decide |" in md
    assert "agent-action-capsule verify --store ledger.jsonl" in md
    assert "capsule-emit permalink --ledger ledger.jsonl --check" in md
    assert "verify.agentactioncapsule.org/v/" in md
    # short capsule_ids from the real ledger appear in the table
    for cap in capsules:
        assert f"`{cap['capsule_id'][:8]}`" in md


def test_no_issue_no_viewer_link(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    md = build_evidence_markdown(capsules, viewer_link=False)
    assert "Implements:" not in md
    assert "verify.agentactioncapsule.org" not in md
    # offline verify path remains — that is the substance
    assert "agent-action-capsule verify --store" in md


def test_attestation_mode_reported_as_carried(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    md = build_evidence_markdown(capsules)
    assert "Attestation mode: self_attested" in md
    assert "adds no claims of its own" in md


def test_empty_refuses(tmp_path):
    with pytest.raises(EvidenceError, match="no capsules"):
        build_evidence_markdown([])


def test_tampered_refuses(tmp_path):
    capsules = read_ledger(_ledger(tmp_path))
    tampered = [json.loads(json.dumps(c)) for c in capsules]
    tampered[0]["model_attestation"]["compute_attestation"]["agent_output_digest"] = "0" * 64
    with pytest.raises(EvidenceError, match="refusing to build an evidence bundle"):
        build_evidence_markdown(tampered)


def test_cli_stdout(tmp_path, capsys):
    path = _ledger(tmp_path)
    rc = main(["evidence", "--ledger", str(path), "--issue", ISSUE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Verification evidence" in out
    assert ISSUE in out


def test_cli_out_file(tmp_path, capsys):
    path = _ledger(tmp_path)
    out_file = tmp_path / "verification-comment.md"
    rc = main(["evidence", "--ledger", str(path), "--out", str(out_file)])
    assert rc == 0
    assert "VALID" in capsys.readouterr().out
    assert out_file.read_text().startswith("## Verification evidence")


def test_cli_tampered_exits_1(tmp_path, capsys):
    path = _ledger(tmp_path)
    capsules = read_ledger(path)
    capsules[0]["model_attestation"]["compute_attestation"]["agent_output_digest"] = "0" * 64
    bad = tmp_path / "tampered.jsonl"
    bad.write_text("\n".join(json.dumps(c) for c in capsules) + "\n")
    rc = main(["evidence", "--ledger", str(bad)])
    assert rc == 1
    assert "refusing" in capsys.readouterr().err
