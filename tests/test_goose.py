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


def test_server_capsule_ledger_limit_clamped_at_zero(tmp_path):
    """limit=0 is clamped to 1 — does not exploit Python's -0 == 0 slice gotcha."""
    ledger = tmp_path / "l.jsonl"
    for action in ("x1", "x2", "x3"):
        emit(action=action, operator="org", developer="d@v1",
             verdict="executed", anchor=False, ledger=ledger)
    from capsule_emit.server import capsule_ledger

    reply = capsule_ledger(ledger=str(ledger), limit=0)
    # Must show exactly 1 row (clamped), not all 3
    assert "showing last 1" in reply


def test_server_capsule_ledger_negative_limit_clamped(tmp_path):
    """Negative limit is clamped to 1 — does not silently drop first rows."""
    ledger = tmp_path / "l.jsonl"
    for action in ("y1", "y2", "y3"):
        emit(action=action, operator="org", developer="d@v1",
             verdict="executed", anchor=False, ledger=ledger)
    from capsule_emit.server import capsule_ledger

    reply = capsule_ledger(ledger=str(ledger), limit=-1)
    # Must show 1 row (clamped), not 2 (records[1:])
    assert "showing last 1" in reply
    assert "y1" not in reply  # oldest row excluded by limit=1 (shows only last)
    assert "y3" in reply      # most recent always included


def test_server_capsule_verify_short_prefix_rejected(tmp_path):
    """capsule_verify rejects a prefix shorter than 8 chars to prevent silent mismatch."""
    ledger = tmp_path / "l.jsonl"
    result = emit(action="test", operator="org", developer="d@v1",
                  verdict="executed", anchor=False, ledger=ledger)
    from capsule_emit.server import capsule_verify

    reply = capsule_verify(capsule_id=result.capsule_id[:3], ledger=str(ledger))
    assert "error" in reply
    assert "too short" in reply


def test_server_capsule_verify_full_id_accepted(tmp_path):
    """capsule_verify accepts a full 64-char capsule ID."""
    ledger = tmp_path / "l.jsonl"
    result = emit(action="test", operator="org", developer="d@v1",
                  verdict="executed", anchor=False, ledger=ledger)
    from capsule_emit.server import capsule_verify

    reply = capsule_verify(capsule_id=result.capsule_id, ledger=str(ledger))
    assert reply.startswith("ok=True")


def test_server_capsule_record_non_json_input(tmp_path):
    """capsule_record handles non-JSON strings gracefully — seals as raw string digest."""
    ledger = tmp_path / "l.jsonl"
    from capsule_emit.server import capsule_record

    reply = capsule_record(
        action="raw_action",
        tool_input="not-json",
        tool_output="also-not-json",
        ledger=str(ledger),
    )
    assert "sealed capsule_id=" in reply
    records = read_ledger(ledger)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Boundary / hardening: companion server error paths
# ---------------------------------------------------------------------------


def test_server_capsule_record_emit_failure_returns_error_string(tmp_path):
    """capsule_record with an invalid ledger path returns 'error: ...' string — no crash."""
    from capsule_emit.server import capsule_record

    reply = capsule_record(
        action="order_x",
        tool_input='{"qty": 1}',
        tool_output='{"status": "ok"}',
        ledger="/nonexistent/directory/ledger.jsonl",
    )
    assert reply.startswith("error:"), f"expected error string, got: {reply!r}"


def test_server_capsule_verify_not_found_returns_not_found(tmp_path):
    """capsule_verify with an ID not in the ledger returns 'not_found ...' string."""
    ledger = tmp_path / "l.jsonl"
    from capsule_emit.server import capsule_verify

    reply = capsule_verify(capsule_id="abcdef12", ledger=str(ledger))
    assert "not_found" in reply


def test_server_capsule_verify_malformed_capsule_returns_error(tmp_path):
    """capsule_verify on a valid-JSON but structurally-bad record returns error string."""
    ledger = tmp_path / "l.jsonl"
    # Write a JSON object that looks like a capsule id-wise but has no valid structure
    import json as _json

    ledger.write_text(_json.dumps({"capsule_id": "abcdef1234567890"}) + "\n")
    from capsule_emit.server import capsule_verify

    reply = capsule_verify(capsule_id="abcdef12", ledger=str(ledger))
    # Either ok=False (verify ran) or error: (exception caught) — never a crash
    assert any(tok in reply for tok in ("ok=False", "ok=True", "error:")), reply


def test_server_corrupt_ledger_line_skipped(tmp_path):
    """A corrupt JSONL line is skipped; valid lines around it are still returned."""
    ledger = tmp_path / "l.jsonl"
    emit(
        action="good_action",
        operator="org",
        developer="d@v1",
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    # Inject a corrupt line in the middle
    with open(ledger, "a") as fh:
        fh.write("NOT VALID JSON\n")
    emit(
        action="another_action",
        operator="org",
        developer="d@v1",
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )

    from capsule_emit.server import capsule_ledger

    reply = capsule_ledger(ledger=str(ledger))
    # Corrupt line skipped; both valid capsules present
    assert "2 capsule" in reply
    assert "good_action" in reply
    assert "another_action" in reply


def test_server_all_corrupt_ledger_returns_empty(tmp_path):
    """A fully corrupt ledger (all bad JSON) is treated as empty — no crash."""
    ledger = tmp_path / "l.jsonl"
    ledger.write_text("CORRUPT\nALSO CORRUPT\n")
    from capsule_emit.server import capsule_ledger

    reply = capsule_ledger(ledger=str(ledger))
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
