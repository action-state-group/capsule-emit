# SPDX-License-Identifier: Apache-2.0
"""Tests for the Goose extension integration.

Covers:
- companion server tools (capsule_record, capsule_verify, capsule_ledger)
- Pattern A: @emitter.tool() on FastMCP tools — capsule is sealed per call
- Pattern A: runtime="mcp" is stamped on every capsule
- Pattern A: tamper one byte → verify fails
- Pattern A: two calls → two capsule rows in ledger
- server.py module is importable (FastMCP server instantiates without error)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_action_capsule import verify

from capsule_emit import emit, read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

pytest.importorskip("mcp", reason="mcp package not installed")
from capsule_emit import server as _server_module  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emitter(tmp_path: Path, **kw) -> MCPCapsuleEmitter:
    return MCPCapsuleEmitter(
        operator="test-org",
        developer="goose-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _ca(result) -> dict:
    """Return compute_attestation block from an EmitResult."""
    return result.capsule.get("model_attestation", {}).get("compute_attestation", {})


# ---------------------------------------------------------------------------
# Companion server module
# ---------------------------------------------------------------------------


def test_server_module_importable():
    """capsule_emit.server imports without error and creates an FastMCP instance."""
    from mcp.server.fastmcp import FastMCP

    assert isinstance(_server_module.mcp, FastMCP)


def test_server_capsule_record(tmp_path):
    """capsule_record tool seals a capsule to the specified ledger."""
    import os

    os.environ["CAPSULE_LEDGER"] = str(tmp_path / "l.jsonl")

    # Re-import to pick up the env var (defaults are evaluated at import time
    # in the module — we call the function directly to bypass that)
    from capsule_emit.server import capsule_record

    reply = capsule_record(
        action="buy_widget",
        tool_input='{"qty": 3}',
        tool_output='{"status": "ok"}',
        ledger=str(tmp_path / "l.jsonl"),
    )
    assert "sealed capsule_id=" in reply
    records = read_ledger(tmp_path / "l.jsonl")
    assert len(records) == 1
    assert "buy_widget" in records[0].get("action_id", "")


def test_server_capsule_verify_ok(tmp_path):
    """capsule_verify returns ok=True for a valid capsule."""
    ledger = tmp_path / "l.jsonl"
    result = emit(
        action="test_action",
        operator="org",
        developer="dev@v1",
        agent_input={"x": 1},
        agent_output={"y": 2},
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    from capsule_emit.server import capsule_verify

    reply = capsule_verify(capsule_id=result.capsule_id[:8], ledger=str(ledger))
    assert reply.startswith("ok=True")


def test_server_capsule_ledger_summary(tmp_path):
    """capsule_ledger returns a summary with row count and action names."""
    ledger = tmp_path / "l.jsonl"
    for action in ("action_a", "action_b"):
        emit(
            action=action,
            operator="org",
            developer="dev@v1",
            agent_input={},
            agent_output={},
            verdict="executed",
            anchor=False,
            ledger=ledger,
        )
    from capsule_emit.server import capsule_ledger

    reply = capsule_ledger(ledger=str(ledger))
    assert "2 capsule" in reply
    assert "action_a" in reply
    assert "action_b" in reply


def test_server_capsule_ledger_empty(tmp_path):
    """capsule_ledger reports empty when ledger has no rows."""
    from capsule_emit.server import capsule_ledger

    reply = capsule_ledger(ledger=str(tmp_path / "nope.jsonl"))
    assert "empty" in reply


# ---------------------------------------------------------------------------
# Pattern A: @emitter.tool() on FastMCP tools
# ---------------------------------------------------------------------------


def test_goose_pattern_a_seals_capsule(tmp_path):
    """@emitter.tool() on an MCP tool seals a capsule per call."""
    emitter = _emitter(tmp_path)

    @emitter.tool(effect_type="write_order")
    def submit_order(vendor: str, amount: float) -> dict:
        return {"status": "ok", "vendor": vendor}

    submit_order(vendor="Frobozz", amount=1240.19)

    records = read_ledger(tmp_path / "ledger.jsonl")
    assert len(records) == 1
    assert "submit_order" in records[0].get("action_id", "")


def test_goose_pattern_a_runtime_mcp(tmp_path):
    """Every capsule from Pattern A carries runtime='mcp'."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def my_tool(x: str) -> str:
        return x.upper()

    my_tool(x="hello")
    ca = _ca(emitter.last)
    assert ca.get("runtime") == "mcp"


def test_goose_pattern_a_verify_ok(tmp_path):
    """Capsule emitted via Pattern A verifies ok=True."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def my_tool(x: int) -> int:
        return x * 2

    my_tool(x=21)
    vr = verify(emitter.last.capsule)
    assert vr.ok, f"expected ok=True, findings: {[f.detail for f in vr.findings]}"


def test_goose_pattern_a_tamper_fails(tmp_path):
    """Flipping one byte in output_digest makes verify return ok=False."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def my_tool(x: str) -> str:
        return x

    my_tool(x="hello")
    records = read_ledger(tmp_path / "ledger.jsonl")
    raw = records[0]
    tampered = json.loads(json.dumps(raw))
    ca = tampered["model_attestation"]["compute_attestation"]
    digest = ca["agent_output_digest"]
    ca["agent_output_digest"] = digest[:-1] + ("0" if digest[-1] != "0" else "1")

    vr = verify(tampered)
    assert not vr.ok, "tampered capsule must not verify ok=True"
    assert any("recomputed" in f.detail for f in vr.findings)


def test_goose_pattern_a_two_calls_two_rows(tmp_path):
    """Two tool calls produce two capsule rows in the ledger."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def get_price(item: str) -> float:
        return 42.0

    get_price(item="widget")
    get_price(item="gadget")

    records = read_ledger(tmp_path / "ledger.jsonl")
    assert len(records) == 2


def test_goose_pattern_a_fyi_action_type(tmp_path):
    """action_type='fyi' is stamped on read-only tools."""
    emitter = _emitter(tmp_path)

    @emitter.tool(action_type="fyi")
    def read_price(item: str) -> float:
        return 9.99

    read_price(item="doohickey")
    records = read_ledger(tmp_path / "ledger.jsonl")
    assert records[0].get("action_type") == "fyi"
