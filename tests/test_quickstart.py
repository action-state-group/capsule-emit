# SPDX-License-Identifier: Apache-2.0
"""Quickstart acceptance tests — the 5-minute bar for capsule-emit.

Verifies: emit → ledger append → ledger view → verify (VALID) → tamper (INVALID)
→ confirm-chain → manifest parse.
"""
from __future__ import annotations

import io

import pytest
from agent_action_capsule import verify

from capsule_emit import emit, ledger_view, load_manifest, read_ledger

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "ledger.jsonl"


# ---------------------------------------------------------------------------
# Core emit
# ---------------------------------------------------------------------------

def test_emit_returns_result_with_capsule_id(tmp_ledger):
    cap = emit(
        action="write_po",
        operator="acme-co",
        developer="po-agent@v1",
        agent_input={"vendor": "Frobozz Supply", "total": "1240.19"},
        agent_output={"status": "dispatched"},
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        verdict="executed",
        effect={"type": "write_po", "status": "dispatched"},
        anchor=False,
        ledger=tmp_ledger,
    )
    assert len(cap.capsule_id) == 64
    assert cap.capsule_id.islower()
    assert cap.anchored is False


def test_emit_verify_valid(tmp_ledger):
    cap = emit(
        action="write_po",
        operator="acme-co",
        developer="po-agent@v1",
        verdict="executed",
        anchor=False,
        ledger=tmp_ledger,
    )
    result = verify(cap.capsule)
    assert result.ok, [f.detail for f in result.findings if f.severity == "error"]


def test_emit_tamper_invalid(tmp_ledger):
    cap = emit(
        action="write_po",
        operator="acme-co",
        developer="po-agent@v1",
        verdict="executed",
        anchor=False,
        ledger=tmp_ledger,
    )
    tampered = dict(cap.capsule)
    tampered["operator"] = "evil-corp"
    result = verify(tampered)
    assert not result.ok, "tampered capsule should not verify"


def test_emit_agent_input_digest_committed(tmp_ledger):
    cap = emit(
        action="process",
        operator="org",
        developer="agent@v1",
        agent_input={"secret": "hunter2"},
        agent_output={"result": "ok"},
        model={"provider": "test", "model_id": "test-model"},
        verdict="executed",
        anchor=False,
        ledger=tmp_ledger,
    )
    # Digests are in compute_attestation — committed to capsule_id
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "agent_input_digest" in ca
    assert "agent_output_digest" in ca
    assert len(ca["agent_input_digest"]) == 64  # sha256 hex


def test_emit_without_model_still_works(tmp_ledger):
    cap = emit(
        action="log_event",
        operator="org",
        developer="agent@v1",
        agent_input={"key": "val"},
        verdict="executed",
        anchor=False,
        ledger=tmp_ledger,
    )
    result = verify(cap.capsule)
    assert result.ok


# ---------------------------------------------------------------------------
# Confirm chaining
# ---------------------------------------------------------------------------

def test_emit_confirm_chains(tmp_ledger):
    cap = emit(
        action="write_po",
        operator="acme-co",
        developer="po-agent@v1",
        verdict="executed",
        anchor=False,
        ledger=tmp_ledger,
    )
    confirm = emit(
        action="confirm_write_po",
        operator="acme-co",
        developer="po-agent@v1",
        confirms=cap.capsule_id,
        verdict="confirmed",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert confirm.capsule["chain"]["parent_capsule_id"] == cap.capsule_id
    assert confirm.capsule["chain"]["relation"] == "confirms"
    assert verify(confirm.capsule).ok


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_ledger_appended(tmp_ledger):
    emit(action="a", operator="o", developer="d", verdict="executed", anchor=False, ledger=tmp_ledger)
    emit(action="b", operator="o", developer="d", verdict="executed", anchor=False, ledger=tmp_ledger)
    records = read_ledger(tmp_ledger)
    assert len(records) == 2


def test_ledger_view_prints_table(tmp_ledger):
    emit(
        action="write_po",
        operator="acme-co",
        developer="po-agent@v1",
        verdict="executed",
        effect={"type": "write_po", "status": "dispatched"},
        anchor=False,
        ledger=tmp_ledger,
    )
    buf = io.StringIO()
    ledger_view(tmp_ledger, out=buf)
    output = buf.getvalue()
    assert "write_po" in output
    assert "acme-co" in output


def test_ledger_view_empty(tmp_ledger):
    buf = io.StringIO()
    ledger_view(tmp_ledger, out=buf)
    assert "empty" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# Manifest parser
# ---------------------------------------------------------------------------

def test_manifest_load(tmp_path):
    (tmp_path / "manifest.md").write_text(
        "---\nwicket_id: test-flow\ntitle: Test Flow\n---\n\n"
        "## Effect\n\n`do_thing` — autonomy `narrate`, reversibility `two_way`.\n\n"
        "## Constraints\n\n"
        "| id | what it checks | method | severity |\n"
        "|----|----------------|--------|----------|\n"
        "| `check_one` | Does X. | arithmetic | **block** |\n"
    )
    mf = load_manifest(tmp_path / "manifest.md")
    assert mf.wicket_id == "test-flow"
    assert mf.autonomy == "narrate"
    assert "check_one" in mf.constraint_names


def test_manifest_default_autonomy_narrate(tmp_path):
    (tmp_path / "manifest.md").write_text(
        "---\nwicket_id: simple\n---\n\n# Simple\nNo effect section.\n"
    )
    mf = load_manifest(tmp_path / "manifest.md")
    assert mf.autonomy == "narrate"


def test_manifest_missing_file_safe(tmp_path):
    mf = load_manifest(tmp_path / "nonexistent.md")
    assert mf.autonomy == "narrate"


# ---------------------------------------------------------------------------
# Full quickstart chain (acceptance test)
# ---------------------------------------------------------------------------

def test_full_quickstart_chain(tmp_ledger):
    """The 5-minute bar: emit → anchor-skipped → verify VALID → tamper INVALID."""
    # Emit
    cap = emit(
        action="write_po",
        operator="acme-co",
        developer="po-agent@v1",
        agent_input={"vendor": "Frobozz Supply", "total": "1240.19"},
        agent_output={"po_number": "PO-001"},
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        verdict="executed",
        effect={"type": "write_po", "status": "dispatched"},
        anchor=False,
        ledger=tmp_ledger,
    )
    assert len(cap.capsule_id) == 64

    # Ledger written
    records = read_ledger(tmp_ledger)
    assert len(records) == 1

    # Verify VALID
    assert verify(cap.capsule).ok

    # Tamper → INVALID
    bad = dict(cap.capsule)
    bad["operator"] = "attacker"
    assert not verify(bad).ok

    # Confirm chain
    conf = emit(
        action="confirm_write_po",
        operator="acme-co",
        developer="po-agent@v1",
        confirms=cap.capsule_id,
        verdict="confirmed",
        anchor=False,
        ledger=tmp_ledger,
    )
    assert verify(conf.capsule).ok
    assert conf.capsule["chain"]["parent_capsule_id"] == cap.capsule_id

    # Ledger view
    buf = io.StringIO()
    ledger_view(tmp_ledger, out=buf)
    assert "write_po" in buf.getvalue()
