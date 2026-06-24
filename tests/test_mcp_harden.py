# SPDX-License-Identifier: Apache-2.0
"""Hardening tests for MCPCapsuleEmitter.

Covers:
- async def tools: wrapper is also async; output is awaited before emitting
- async def tools: capsule emits with correct I/O digests
- async def tools: name inferred from fn.__name__
- async def tools: exception propagates, no capsule emitted
- signature binding: positional call → complete named-arg dict
- signature binding: mixed positional+kwargs call → complete named-arg dict
- signature binding: default values filled in by apply_defaults()
- signature binding: positional and kwargs calls produce identical input digest
- output serialization: non-JSON-serializable output digested safely
- output serialization: bytes output digested safely
- FastMCP integration: functools.wraps preserves signature for schema gen
- FastMCP integration: sync tool emits capsule with correct I/O digests
- FastMCP integration: async tool works end-to-end through double-decorator
"""
from __future__ import annotations

import asyncio
import inspect

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


# ---------------------------------------------------------------------------
# Async tool support
# ---------------------------------------------------------------------------


def test_mcp_async_tool_returns_result_not_coroutine(tmp_path):
    """async def tool: calling the wrapper returns the result, not a coroutine."""
    emitter = _emitter(tmp_path)

    @emitter.tool("async_double")
    async def double(x: int) -> int:
        return x * 2

    result = asyncio.run(double(x=5))
    assert result == 10
    assert not inspect.iscoroutine(result)


def test_mcp_async_tool_emits_capsule_with_io_digests(tmp_path):
    """async def tool: capsule is emitted with correct input and output digests."""
    emitter = _emitter(tmp_path)

    @emitter.tool("async_echo")
    async def echo(msg: str) -> str:
        return f"ECHO:{msg}"

    asyncio.run(echo(msg="hello"))

    records = read_ledger(tmp_path / "ledger.jsonl")
    assert len(records) == 1
    assert "async_echo" in records[0].get("action_id", "")

    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "agent_input_digest" in ca
    assert "agent_output_digest" in ca
    assert verify(emitter.last.capsule).ok


def test_mcp_async_tool_name_inferred_from_fn_name(tmp_path):
    """async def tool: action name inferred from fn.__name__ when not supplied."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    async def my_async_fn(x: int) -> int:
        return x

    asyncio.run(my_async_fn(x=1))
    assert emitter.last.capsule["action_id"].startswith("my_async_fn/")


def test_mcp_async_tool_raises_propagates_no_capsule(tmp_path):
    """async def tool: exception propagates and no capsule is emitted."""
    emitter = _emitter(tmp_path)

    @emitter.tool("async_fail")
    async def fail_tool():
        raise ValueError("async failure")

    with pytest.raises(ValueError, match="async failure"):
        asyncio.run(fail_tool())

    assert emitter.last is None
    assert read_ledger(tmp_path / "ledger.jsonl") == []


def test_mcp_async_wrapper_is_coroutine_function(tmp_path):
    """Wrapper for async def fn must itself be a coroutine function (MCP SDK compat)."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    async def async_fn(x: int) -> int:
        return x

    assert inspect.iscoroutinefunction(async_fn)


def test_mcp_sync_wrapper_is_not_coroutine_function(tmp_path):
    """Wrapper for sync fn must NOT be a coroutine function."""
    emitter = _emitter(tmp_path)

    @emitter.tool()
    def sync_fn(x: int) -> int:
        return x

    assert not inspect.iscoroutinefunction(sync_fn)


# ---------------------------------------------------------------------------
# Signature binding
# ---------------------------------------------------------------------------


def test_mcp_positional_call_produces_named_dict(tmp_path):
    """Positional call f(v, t) produces input digest identical to kwargs call."""
    emitter_pos = _emitter(tmp_path)
    emitter_kw = MCPCapsuleEmitter(
        operator="test-org",
        developer="agent@v1",
        ledger=tmp_path / "ledger_kw.jsonl",
        anchor=False,
    )

    @emitter_pos.tool("fn")
    def fn_pos(vendor: str, total: float) -> dict:
        return {}

    @emitter_kw.tool("fn")
    def fn_kw(vendor: str, total: float) -> dict:
        return {}

    fn_pos("ACME", 1240.19)
    fn_kw(vendor="ACME", total=1240.19)

    ca_pos = emitter_pos.last.capsule["model_attestation"]["compute_attestation"]
    ca_kw = emitter_kw.last.capsule["model_attestation"]["compute_attestation"]
    assert ca_pos["agent_input_digest"] == ca_kw["agent_input_digest"], (
        "positional call must produce the same input digest as kwargs call"
    )


def test_mcp_mixed_positional_kwargs_same_digest(tmp_path):
    """Mixed call f(v, total=t) produces the same input digest as pure kwargs."""
    emitter_mixed = _emitter(tmp_path)
    emitter_kw = MCPCapsuleEmitter(
        operator="test-org",
        developer="agent@v1",
        ledger=tmp_path / "ledger_kw.jsonl",
        anchor=False,
    )

    @emitter_mixed.tool("fn")
    def fn_mixed(vendor: str, total: float) -> dict:
        return {}

    @emitter_kw.tool("fn")
    def fn_kw(vendor: str, total: float) -> dict:
        return {}

    fn_mixed("ACME", total=1240.19)
    fn_kw(vendor="ACME", total=1240.19)

    ca_mixed = emitter_mixed.last.capsule["model_attestation"]["compute_attestation"]
    ca_kw = emitter_kw.last.capsule["model_attestation"]["compute_attestation"]
    assert ca_mixed["agent_input_digest"] == ca_kw["agent_input_digest"], (
        "mixed positional+kwargs call must produce the same input digest as pure kwargs"
    )


def test_mcp_signature_defaults_filled_in(tmp_path):
    """apply_defaults() includes default-valued params omitted by the caller."""
    emitter_explicit = _emitter(tmp_path)
    emitter_omitted = MCPCapsuleEmitter(
        operator="test-org",
        developer="agent@v1",
        ledger=tmp_path / "ledger_omitted.jsonl",
        anchor=False,
    )

    @emitter_explicit.tool("fn")
    def fn_explicit(a: int, b: int = 0) -> int:
        return a + b

    @emitter_omitted.tool("fn")
    def fn_omitted(a: int, b: int = 0) -> int:
        return a + b

    fn_explicit(5, b=0)
    fn_omitted(5)

    ca_explicit = emitter_explicit.last.capsule["model_attestation"]["compute_attestation"]
    ca_omitted = emitter_omitted.last.capsule["model_attestation"]["compute_attestation"]
    assert ca_explicit["agent_input_digest"] == ca_omitted["agent_input_digest"], (
        "omitting a default param must produce the same digest as passing it explicitly"
    )


# ---------------------------------------------------------------------------
# Output serialization safety
# ---------------------------------------------------------------------------


class _Unserializable:
    """A type that is not JSON-serializable."""
    def __repr__(self) -> str:
        return "Unserializable()"


def test_mcp_non_json_serializable_output_digested_safely(tmp_path):
    """Non-JSON-serializable output is digested via str() fallback; no corrupt ledger."""
    emitter = _emitter(tmp_path)

    @emitter.tool("custom_obj_action")
    def fn() -> object:
        return _Unserializable()

    fn()

    records = read_ledger(tmp_path / "ledger.jsonl")
    assert len(records) == 1, "ledger must have exactly one row"
    assert verify(emitter.last.capsule).ok
    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "agent_output_digest" in ca


def test_mcp_bytes_output_digested_safely(tmp_path):
    """bytes output is digested via str() fallback; no corrupt ledger."""
    emitter = _emitter(tmp_path)

    @emitter.tool("bytes_action")
    def fn() -> bytes:
        return b"binary data"

    fn()

    records = read_ledger(tmp_path / "ledger.jsonl")
    assert len(records) == 1
    assert verify(emitter.last.capsule).ok
    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "agent_output_digest" in ca


# ---------------------------------------------------------------------------
# FastMCP integration (requires `pip install mcp`)
# ---------------------------------------------------------------------------


@pytest.mark.mcp
class TestFastMCPIntegration:
    """Integration tests with the real MCP SDK (FastMCP).

    Skipped automatically when `mcp` is not installed.
    Run with: pip install mcp && pytest -m mcp
    """

    def test_schema_introspection_survives_wrapper(self, tmp_path):
        """functools.wraps preserves the signature FastMCP uses for schema gen.

        FastMCP calls inspect.signature() on the decorated function.  Because
        @emitter.tool uses functools.wraps, inspect.signature follows __wrapped__
        and returns the original fn's parameter names and types — not (*args, **kwargs).
        """
        pytest.importorskip("mcp", reason="mcp not installed")
        from mcp.server.fastmcp import FastMCP

        emitter = _emitter(tmp_path)
        app = FastMCP("test-server")

        @app.tool()
        @emitter.tool()
        def write_order(vendor: str, total: float) -> dict:
            return {"po_id": "PO-001"}

        sig = inspect.signature(write_order)
        params = list(sig.parameters)
        assert "vendor" in params, "vendor param must survive double-decorator wrapping"
        assert "total" in params, "total param must survive double-decorator wrapping"

    def test_sync_tool_capsule_emits_correct_digests(self, tmp_path):
        """Sync tool with @app.tool @emitter.tool emits a valid capsule."""
        pytest.importorskip("mcp", reason="mcp not installed")
        from mcp.server.fastmcp import FastMCP

        emitter = _emitter(tmp_path)
        app = FastMCP("test-server")

        @app.tool()
        @emitter.tool()
        def write_order(vendor: str, total: float) -> dict:
            return {"po_id": "PO-001", "vendor": vendor, "total": total}

        result = write_order(vendor="ACME", total=1240.19)
        assert result["vendor"] == "ACME"

        assert emitter.last is not None
        ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
        assert "agent_input_digest" in ca
        assert "agent_output_digest" in ca
        assert verify(emitter.last.capsule).ok

    def test_async_tool_works_end_to_end(self, tmp_path):
        """Async tool with @app.tool @emitter.tool emits a valid capsule."""
        pytest.importorskip("mcp", reason="mcp not installed")
        from mcp.server.fastmcp import FastMCP

        emitter = _emitter(tmp_path)
        app = FastMCP("test-server")

        @app.tool()
        @emitter.tool()
        async def async_write_order(vendor: str, total: float) -> dict:
            return {"po_id": "PO-ASYNC", "vendor": vendor, "total": total}

        result = asyncio.run(async_write_order(vendor="ACME", total=99.0))
        assert result["po_id"] == "PO-ASYNC"

        assert emitter.last is not None
        ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
        assert "agent_input_digest" in ca
        assert "agent_output_digest" in ca
        assert verify(emitter.last.capsule).ok

    def test_ledger_trail_three_calls(self, tmp_path):
        """Three calls produce three ledger rows (audit trail)."""
        pytest.importorskip("mcp", reason="mcp not installed")
        from mcp.server.fastmcp import FastMCP

        emitter = _emitter(tmp_path)
        app = FastMCP("test-server")

        @app.tool()
        @emitter.tool()
        def submit_order(vendor: str, amount: float) -> dict:
            return {"status": "ok"}

        submit_order(vendor="A", amount=100.0)
        submit_order(vendor="B", amount=200.0)
        submit_order(vendor="C", amount=300.0)

        records = read_ledger(tmp_path / "ledger.jsonl")
        assert len(records) == 3
        assert len(emitter.results) == 3
        for r in records:
            assert verify(r).ok
