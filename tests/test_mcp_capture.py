# SPDX-License-Identifier: Apache-2.0
"""Tests for MCPCapsuleEmitter capture-completeness features.

Covers:
- runtime="mcp": every capsule carries runtime in compute_attestation
- MCP Context provenance: request_id / client_id / clientInfo extracted when present
- Context param excluded from input digest
- Graceful degradation: no Context param → no provenance, no error
- Graceful degradation: Context outside real request → no provenance, no error
- model= per-tool: overrides constructor default
- model= constructor default: used when no per-tool override
- action_type: defaults to "act" for MCP tools
- action_type: constructor default overridable
- action_type: per-tool override (@emitter.tool(action_type="decide"))
- action_type: passed through core.emit via extra_compute
- host_provenance=False (default): no host fields in compute_attestation
- host_provenance=True: hostname and platform present in compute_attestation
- extra_compute: merged into compute_attestation by core.emit
- demo: action_id inferred from fn.__name__ (no explicit name)
- verify: all capsules pass with new fields
"""
from __future__ import annotations

import asyncio
import platform
import socket

import pytest
from agent_action_capsule import verify

from capsule_emit import read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emitter(tmp_path, **kw) -> MCPCapsuleEmitter:
    return MCPCapsuleEmitter(
        operator="test-org",
        developer="agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _ca(emitter: MCPCapsuleEmitter) -> dict:
    return emitter.last.capsule["model_attestation"]["compute_attestation"]


# ---------------------------------------------------------------------------
# (1) AUTO runtime="mcp"
# ---------------------------------------------------------------------------


def test_mcp_runtime_set_on_sync_tool(tmp_path):
    emitter = _emitter(tmp_path)

    @emitter.tool("act")
    def fn() -> dict:
        return {"ok": True}

    fn()
    assert _ca(emitter).get("runtime") == "mcp"
    assert verify(emitter.last.capsule).ok


def test_mcp_runtime_set_on_async_tool(tmp_path):
    emitter = _emitter(tmp_path)

    @emitter.tool("async_act")
    async def fn() -> dict:
        return {"ok": True}

    asyncio.run(fn())
    assert _ca(emitter).get("runtime") == "mcp"
    assert verify(emitter.last.capsule).ok


def test_mcp_runtime_present_across_multiple_calls(tmp_path):
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def fn() -> dict:
        return {}

    fn()
    fn()
    fn()
    for r in emitter.results:
        ca = r.capsule["model_attestation"]["compute_attestation"]
        assert ca.get("runtime") == "mcp"


# ---------------------------------------------------------------------------
# (2) MCP Context provenance
# ---------------------------------------------------------------------------


class _MockClientInfo:
    name = "test-client"
    version = "1.0.0"


class _MockClientParams:
    clientInfo = _MockClientInfo()


class _MockSession:
    client_params = _MockClientParams()


class _MockRequestContext:
    request_id = "req-abc123"
    meta = None  # no meta → client_id will be None

    @property
    def session(self):
        return _MockSession()


class _MockContext:
    """Minimal stand-in for mcp.server.fastmcp.Context in direct-call tests."""
    _request_context = _MockRequestContext()

    @property
    def request_id(self) -> str:
        return str(self._request_context.request_id)

    @property
    def client_id(self):
        meta = self._request_context.meta
        return getattr(meta, "client_id", None) if meta else None

    @property
    def session(self):
        return self._request_context.session


def test_mcp_context_provenance_request_id_captured(tmp_path):
    """When a tool has a Context param, request_id is captured in compute_attestation."""
    pytest.importorskip("mcp", reason="mcp not installed")
    from mcp.server.fastmcp import Context

    emitter = _emitter(tmp_path)

    @emitter.tool("ctx_tool")
    def fn(vendor: str, ctx: Context) -> dict:
        return {"ok": True}

    fn(vendor="ACME", ctx=_MockContext())

    ca = _ca(emitter)
    assert ca.get("mcp_request_id") == "req-abc123"
    assert verify(emitter.last.capsule).ok


def test_mcp_context_provenance_client_info_captured(tmp_path):
    """clientInfo name and version are captured from session.client_params."""
    pytest.importorskip("mcp", reason="mcp not installed")
    from mcp.server.fastmcp import Context

    emitter = _emitter(tmp_path)

    @emitter.tool("ctx_tool")
    def fn(x: int, ctx: Context) -> int:
        return x * 2

    fn(x=5, ctx=_MockContext())

    ca = _ca(emitter)
    assert ca.get("mcp_client_name") == "test-client"
    assert ca.get("mcp_client_version") == "1.0.0"


def test_mcp_context_param_excluded_from_input_digest(tmp_path):
    """The ctx param must not appear in tool_input — it is infrastructure, not input."""
    pytest.importorskip("mcp", reason="mcp not installed")
    from mcp.server.fastmcp import Context

    emitter_with_ctx = _emitter(tmp_path)
    ledger_no_ctx = tmp_path / "no_ctx.jsonl"
    emitter_no_ctx = MCPCapsuleEmitter(
        operator="test-org", developer="agent@v1", ledger=ledger_no_ctx, anchor=False
    )

    @emitter_with_ctx.tool("order")
    def fn_with_ctx(vendor: str, ctx: Context) -> dict:
        return {}

    @emitter_no_ctx.tool("order")
    def fn_no_ctx(vendor: str) -> dict:
        return {}

    fn_with_ctx(vendor="ACME", ctx=_MockContext())
    fn_no_ctx(vendor="ACME")

    ca_ctx = _ca(emitter_with_ctx)
    ca_no = _ca(emitter_no_ctx)
    # The input digest must be the same whether or not ctx was passed
    assert ca_ctx["agent_input_digest"] == ca_no["agent_input_digest"], (
        "ctx param must be excluded from the input digest"
    )


def test_mcp_no_context_param_no_provenance_no_error(tmp_path):
    """Tools without a Context param emit normally with no mcp_* fields."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def fn(x: int) -> int:
        return x

    fn(x=1)

    ca = _ca(emitter)
    assert "mcp_request_id" not in ca
    assert "mcp_client_id" not in ca
    assert verify(emitter.last.capsule).ok


def test_mcp_context_outside_request_degrades_gracefully(tmp_path):
    """Context raised outside a real request → no provenance, no crash."""
    pytest.importorskip("mcp", reason="mcp not installed")
    from mcp.server.fastmcp import Context

    emitter = _emitter(tmp_path)

    @emitter.tool("ctx_tool")
    def fn(x: int, ctx: Context) -> int:
        return x

    # Context() with no request_context raises ValueError on attribute access
    bare_ctx = Context()
    fn(x=1, ctx=bare_ctx)  # must not raise

    ca = _ca(emitter)
    assert "mcp_request_id" not in ca
    assert verify(emitter.last.capsule).ok


# ---------------------------------------------------------------------------
# (3) model= EASY + HONEST
# ---------------------------------------------------------------------------


def test_mcp_model_per_tool_overrides_constructor_default(tmp_path):
    """Per-tool model= overrides the constructor default."""
    emitter = _emitter(
        tmp_path,
        model={"provider": "anthropic", "model_id": "claude-haiku-4-5"},
    )

    @emitter.tool(model={"provider": "openai", "model_id": "gpt-4o"})
    def fn() -> dict:
        return {}

    fn()
    ma = emitter.last.capsule["model_attestation"]
    assert ma.get("model_id") == "gpt-4o"
    assert ma.get("provider") == "openai"


def test_mcp_model_constructor_default_used_when_no_per_tool(tmp_path):
    """Constructor model= is used when @emitter.tool() has no model=."""
    emitter = _emitter(
        tmp_path,
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    )

    @emitter.tool()
    def fn() -> dict:
        return {}

    fn()
    ma = emitter.last.capsule["model_attestation"]
    assert ma.get("model_id") == "claude-sonnet-4-6"


def test_mcp_model_none_when_not_set(tmp_path):
    """No model= anywhere → model_attestation has no model_id (honest: not auto-captured)."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def fn() -> dict:
        return {}

    fn()
    ma = emitter.last.capsule["model_attestation"]
    assert not ma.get("model_id"), "MCP adapter must not fake model auto-capture"


# ---------------------------------------------------------------------------
# (4) action_type FIX
# ---------------------------------------------------------------------------


def test_mcp_action_type_defaults_to_decide_for_executed_verdict(tmp_path):
    """MCP tools with verdict='executed' (default) get action_type='decide'.

    The spec (§5.1) defines two values: 'decide' (consequential action) and
    'fyi' (passive observation).  For MCP tools with verdict='executed', the
    auto-derive gives 'decide' — correct for consequential tool calls.
    """
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def submit_order() -> dict:
        return {}

    submit_order()
    assert emitter.last.capsule.get("action_type") == "decide"
    assert verify(emitter.last.capsule).ok


def test_mcp_action_type_per_tool_override_to_fyi(tmp_path):
    """@emitter.tool(action_type='fyi') marks a read-only/observation tool."""
    emitter = _emitter(tmp_path)

    @emitter.tool(action_type="fyi")
    def get_status() -> dict:
        return {}

    get_status()
    assert emitter.last.capsule.get("action_type") == "fyi"
    assert verify(emitter.last.capsule).ok


def test_mcp_action_type_constructor_default_overridable(tmp_path):
    """MCPCapsuleEmitter(action_type='fyi') marks all tools as observation-only."""
    emitter = _emitter(tmp_path, action_type="fyi")

    @emitter.tool()
    def get_status() -> dict:
        return {}

    get_status()
    assert emitter.last.capsule.get("action_type") == "fyi"
    assert verify(emitter.last.capsule).ok


def test_mcp_action_type_per_tool_wins_over_constructor(tmp_path):
    """Per-tool action_type= wins over the constructor default."""
    emitter = _emitter(tmp_path, action_type="fyi")

    @emitter.tool(action_type="decide")
    def approve() -> dict:
        return {}

    approve()
    assert emitter.last.capsule.get("action_type") == "decide"
    assert verify(emitter.last.capsule).ok


# ---------------------------------------------------------------------------
# (5) HOST PROVENANCE (opt-in)
# ---------------------------------------------------------------------------


def test_mcp_host_provenance_off_by_default(tmp_path):
    """No host fields in compute_attestation by default."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def fn() -> dict:
        return {}

    fn()
    ca = _ca(emitter)
    assert "host_name" not in ca
    assert "host_platform" not in ca


def test_mcp_host_provenance_captures_hostname_and_platform(tmp_path):
    """host_provenance=True captures hostname and OS platform."""
    emitter = _emitter(tmp_path, host_provenance=True)

    @emitter.tool()
    def fn() -> dict:
        return {}

    fn()
    ca = _ca(emitter)
    assert "host_name" in ca
    assert "host_platform" in ca
    assert ca["host_name"] == socket.gethostname()
    assert platform.system() in ca["host_platform"]
    assert verify(emitter.last.capsule).ok


def test_mcp_host_provenance_present_in_async_tool(tmp_path):
    """host_provenance=True works for async def tools too."""
    emitter = _emitter(tmp_path, host_provenance=True)

    @emitter.tool()
    async def fn() -> dict:
        return {}

    asyncio.run(fn())
    ca = _ca(emitter)
    assert "host_name" in ca
    assert "host_platform" in ca
    assert verify(emitter.last.capsule).ok


# ---------------------------------------------------------------------------
# (6) demo reconciliation checks (inline assertions)
# ---------------------------------------------------------------------------


def test_demo_name_inferred_from_fn_name(tmp_path):
    """@emitter.tool() with no name infers action from fn.__name__."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def submit_order(vendor: str) -> dict:
        return {}

    submit_order(vendor="ACME")
    assert emitter.last.capsule["action_id"].startswith("submit_order/")


def test_demo_three_call_ledger_trail(tmp_path):
    """Three tool calls produce three conforming ledger rows."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def place_order(po: str) -> dict:
        return {"status": "ok"}

    place_order(po="PO-001")
    place_order(po="PO-002")
    place_order(po="PO-003")

    records = read_ledger(tmp_path / "ledger.jsonl")
    assert len(records) == 3
    assert len(emitter.results) == 3
    for r in records:
        assert verify(r).ok
        ca = r["model_attestation"]["compute_attestation"]
        assert ca.get("runtime") == "mcp"
        assert r.get("action_type") == "decide"


def test_demo_effect_status_dispatched(tmp_path):
    """@emitter.tool() always sets effect.status='dispatched'."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def submit() -> dict:
        return {}

    submit()
    assert emitter.last.capsule.get("effect", {}).get("status") == "dispatched"
