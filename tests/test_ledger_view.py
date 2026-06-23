# SPDX-License-Identifier: Apache-2.0
"""Ledger view tests: all four rendering levels + CLI surface.

L1 — view()         one-line-per-capsule table (default)
L2 — view_chains()  chain-tree grouped by parent
L3 — show()         full single-capsule two-tier layout
L4 — --json         raw JSON passthrough (CLI)
"""
from __future__ import annotations

import io
import json

import pytest

from capsule_emit import emit, ledger_show, ledger_view, ledger_view_chains, read_ledger
from capsule_emit.cli import main as cli_main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "test.jsonl"


@pytest.fixture
def two_capsule_ledger(tmp_ledger):
    """A simple chain: root → confirmed child."""
    root = emit(
        action="write_order",
        operator="acme-co",
        developer="agent@v1",
        agent_input={"vendor": "Frobozz", "total": 100},
        agent_output={"po": "PO-001"},
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        verdict="executed",
        effect={"type": "write_order", "status": "dispatched"},
        anchor=False,
        ledger=tmp_ledger,
    )
    child = emit(
        action="confirm_write_order",
        operator="acme-co",
        developer="agent@v1",
        confirms=root.capsule_id,
        verdict="confirmed",
        anchor=False,
        ledger=tmp_ledger,
    )
    return tmp_ledger, root, child


# ---------------------------------------------------------------------------
# L1: flat summary table
# ---------------------------------------------------------------------------

def test_l1_view_prints_table(two_capsule_ledger):
    path, root, _ = two_capsule_ledger
    buf = io.StringIO()
    ledger_view(path, out=buf)
    out = buf.getvalue()
    assert "write_order" in out
    assert "acme-co" in out
    assert "executed" in out
    assert "confirmed" in out


def test_l1_view_shows_chain_column(two_capsule_ledger):
    path, root, child = two_capsule_ledger
    buf = io.StringIO()
    ledger_view(path, out=buf)
    out = buf.getvalue()
    # Chain column: "confirms→<parent_prefix>…"
    assert "confirms→" in out
    assert root.capsule_id[:8] in out


def test_l1_view_empty_ledger(tmp_ledger):
    buf = io.StringIO()
    ledger_view(tmp_ledger, out=buf)
    assert "empty" in buf.getvalue().lower()


def test_l1_view_nonexistent_path(tmp_path):
    buf = io.StringIO()
    ledger_view(tmp_path / "nope.jsonl", out=buf)
    assert "empty" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# L2: chain tree
# ---------------------------------------------------------------------------

def test_l2_view_chains_shows_root_and_child(two_capsule_ledger):
    path, root, child = two_capsule_ledger
    buf = io.StringIO()
    ledger_view_chains(path, out=buf)
    out = buf.getvalue()
    assert root.capsule_id[:12] in out
    assert child.capsule_id[:12] in out


def test_l2_view_chains_shows_confirms_relation(two_capsule_ledger):
    path, _, child = two_capsule_ledger
    buf = io.StringIO()
    ledger_view_chains(path, out=buf)
    out = buf.getvalue()
    assert "confirms" in out


def test_l2_view_chains_child_indented_under_root(two_capsule_ledger):
    path, root, child = two_capsule_ledger
    buf = io.StringIO()
    ledger_view_chains(path, out=buf)
    lines = buf.getvalue().splitlines()
    root_line = next(l for l in lines if root.capsule_id[:12] in l)
    child_line = next(l for l in lines if child.capsule_id[:12] in l)
    # Child line must be more indented than root line
    root_indent = len(root_line) - len(root_line.lstrip())
    child_indent = len(child_line) - len(child_line.lstrip())
    assert child_indent > root_indent, "child must be indented under root"


def test_l2_view_chains_shows_model(two_capsule_ledger):
    path, root, _ = two_capsule_ledger
    buf = io.StringIO()
    ledger_view_chains(path, out=buf)
    out = buf.getvalue()
    assert "claude-sonnet-4-6" in out


def test_l2_view_chains_empty(tmp_ledger):
    buf = io.StringIO()
    ledger_view_chains(tmp_ledger, out=buf)
    assert "empty" in buf.getvalue().lower()


def test_l2_view_chains_orphan_section(tmp_path):
    """A capsule whose parent is missing lands in the orphaned section."""
    ledger = tmp_path / "orphan.jsonl"
    # Emit a child whose parent is not in the ledger
    parent_fake_id = "a" * 64
    child = emit(
        action="child",
        operator="org",
        developer="d@v1",
        confirms=parent_fake_id,
        verdict="confirmed",
        anchor=False,
        ledger=ledger,
    )
    buf = io.StringIO()
    ledger_view_chains(ledger, out=buf)
    out = buf.getvalue()
    assert "orphan" in out.lower()
    assert child.capsule_id[:12] in out


# ---------------------------------------------------------------------------
# L3: single capsule detail
# ---------------------------------------------------------------------------

def test_l3_show_finds_capsule(two_capsule_ledger):
    path, root, _ = two_capsule_ledger
    buf = io.StringIO()
    found = ledger_show(path, root.capsule_id, out=buf)
    assert found is True
    out = buf.getvalue()
    assert root.capsule_id in out
    assert "acme-co" in out
    assert "write_order" in out


def test_l3_show_full_id(two_capsule_ledger):
    path, root, _ = two_capsule_ledger
    buf = io.StringIO()
    ledger_show(path, root.capsule_id, out=buf)
    out = buf.getvalue()
    assert "disposition" in out
    assert "executed" in out


def test_l3_show_prefix_lookup(two_capsule_ledger):
    path, root, _ = two_capsule_ledger
    buf = io.StringIO()
    found = ledger_show(path, root.capsule_id[:16], out=buf)
    assert found is True


def test_l3_show_missing_capsule(two_capsule_ledger):
    path, _, _ = two_capsule_ledger
    buf = io.StringIO()
    found = ledger_show(path, "0" * 64, out=buf)
    assert found is False
    assert "not found" in buf.getvalue().lower()


def test_l3_show_effect_block(two_capsule_ledger):
    path, root, _ = two_capsule_ledger
    buf = io.StringIO()
    ledger_show(path, root.capsule_id, out=buf)
    out = buf.getvalue()
    assert "effect" in out
    assert "dispatched" in out


def test_l3_show_model_attestation(two_capsule_ledger):
    path, root, _ = two_capsule_ledger
    buf = io.StringIO()
    ledger_show(path, root.capsule_id, out=buf)
    out = buf.getvalue()
    assert "model_attestation" in out
    assert "claude-sonnet-4-6" in out


# ---------------------------------------------------------------------------
# L4: --json passthrough (via CLI)
# ---------------------------------------------------------------------------

def test_l4_json_via_cli(two_capsule_ledger, capsys):
    path, _, _ = two_capsule_ledger
    exit_code = cli_main(["ledger", "view", str(path), "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    records = json.loads(captured.out)
    assert len(records) == 2
    assert all("capsule_id" in r for r in records)


# ---------------------------------------------------------------------------
# CLI: all ledger subcommands
# ---------------------------------------------------------------------------

def test_cli_ledger_view_l1(two_capsule_ledger, capsys):
    path, _, _ = two_capsule_ledger
    exit_code = cli_main(["ledger", "view", str(path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "write_order" in out


def test_cli_ledger_view_chains(two_capsule_ledger, capsys):
    path, root, child = two_capsule_ledger
    exit_code = cli_main(["ledger", "view", str(path), "--chains"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert root.capsule_id[:12] in out
    assert child.capsule_id[:12] in out
    assert "confirms" in out


def test_cli_ledger_show(two_capsule_ledger, capsys):
    path, root, _ = two_capsule_ledger
    exit_code = cli_main(["ledger", "show", str(path), root.capsule_id])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert root.capsule_id in out
    assert "write_order" in out


def test_cli_ledger_show_not_found(two_capsule_ledger, capsys):
    path, _, _ = two_capsule_ledger
    exit_code = cli_main(["ledger", "show", str(path), "0" * 64])
    assert exit_code == 1


def test_cli_verify_store(two_capsule_ledger, capsys):
    path, _, _ = two_capsule_ledger
    exit_code = cli_main(["verify", "--store", str(path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "VALID" in out
