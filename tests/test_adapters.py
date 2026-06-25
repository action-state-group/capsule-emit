# SPDX-License-Identifier: Apache-2.0
"""Adapter tests: auto-model-capture + emit_capsule correctness.

Covers:
- LangChain: on_llm_start auto-captures model → threaded into next capsule
- LangChain: on_chat_model_start also captures model
- LangChain: falls back to constructor model= when no on_llm_start fires
- LangChain: captured model is cleared after each tool emit (no cross-leak)
- CrewAI: model= at construction auto-fills every capsule
- CrewAI: wrap(callable) emits capsule with correct action/I/O
- MCP: @emitter.tool emits capsule; model= explicit only
- Hermes: after_tool emits capsule with effect block
- Base: model= at construction is respected by all adapters
"""
from __future__ import annotations

import pytest

from capsule_emit import read_ledger
from capsule_emit.adapters._base import CapsuleEmitterBase
from capsule_emit.adapters.crewai import CrewAICapsuleEmitter
from capsule_emit.adapters.hermes import HermesCapsuleEmitter
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ma(result) -> dict:
    return result.capsule.get("model_attestation") or {}


# ---------------------------------------------------------------------------
# Base: constructor model= fallback
# ---------------------------------------------------------------------------

def test_base_constructor_model_fallback(tmp_path):
    """model= at construction fills every emit_capsule call that doesn't override."""
    ledger = tmp_path / "l.jsonl"
    base = CapsuleEmitterBase(
        operator="org",
        developer="agent@v1",
        ledger=ledger,
        anchor=False,
        model={"provider": "anthropic", "model_id": "claude-haiku-4-5"},
    )
    result = base.emit_capsule("test_action", tool_input="hi", tool_output="bye")
    ma = _ma(result)
    assert ma.get("model_id") == "claude-haiku-4-5"
    assert ma.get("provider") == "anthropic"


def test_base_per_call_model_overrides_default(tmp_path):
    """Per-call model= overrides the constructor default."""
    ledger = tmp_path / "l.jsonl"
    base = CapsuleEmitterBase(
        operator="org",
        developer="agent@v1",
        ledger=ledger,
        anchor=False,
        model={"provider": "openai", "model_id": "gpt-4o"},
    )
    result = base.emit_capsule(
        "test_action",
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    )
    ma = _ma(result)
    assert ma.get("model_id") == "claude-sonnet-4-6"
    assert ma.get("provider") == "anthropic"


def test_base_no_model_when_not_set(tmp_path):
    """No model= at construction and no per-call model → model_attestation has no model_id."""
    ledger = tmp_path / "l.jsonl"
    base = CapsuleEmitterBase(operator="org", developer="agent@v1", ledger=ledger, anchor=False)
    result = base.emit_capsule("test_action")
    ma = _ma(result)
    assert not ma.get("model_id")


# ---------------------------------------------------------------------------
# LangChain: on_llm_start auto-capture
# ---------------------------------------------------------------------------

pytest.importorskip("langchain_core", reason="langchain-core not installed")
from capsule_emit.adapters.langchain import (  # noqa: E402
    LangChainCapsuleEmitter,
    _extract_model_from_serialized,
)


def test_langchain_on_llm_start_captures_openai(tmp_path):
    """on_llm_start with OpenAI serialized dict → model_id and provider captured."""
    emitter = LangChainCapsuleEmitter(operator="org", developer="agent@v1", ledger=tmp_path / "l.jsonl", anchor=False)
    serialized = {
        "name": "ChatOpenAI",
        "kwargs": {"model_name": "gpt-4o", "temperature": 0},
    }
    emitter.on_llm_start(serialized, prompts=["hello"])
    emitter.on_tool_start({"name": "search"}, "query", run_id="r1")
    emitter.on_tool_end("results", run_id="r1")
    ma = _ma(emitter.last)
    assert ma.get("model_id") == "gpt-4o"
    assert ma.get("provider") == "openai"


def test_langchain_on_llm_start_captures_anthropic(tmp_path):
    """on_llm_start with Anthropic serialized dict → model captured."""
    emitter = LangChainCapsuleEmitter(operator="org", developer="agent@v1", ledger=tmp_path / "l.jsonl", anchor=False)
    serialized = {
        "name": "ChatAnthropic",
        "kwargs": {"model": "claude-sonnet-4-6"},
    }
    emitter.on_llm_start(serialized, prompts=["hello"])
    emitter.on_tool_start({"name": "calc"}, "1+1", run_id="r2")
    emitter.on_tool_end("2", run_id="r2")
    ma = _ma(emitter.last)
    assert ma.get("model_id") == "claude-sonnet-4-6"
    assert ma.get("provider") == "anthropic"


def test_langchain_on_chat_model_start_captures(tmp_path):
    """on_chat_model_start also captures model info."""
    emitter = LangChainCapsuleEmitter(operator="org", developer="agent@v1", ledger=tmp_path / "l.jsonl", anchor=False)
    serialized = {"name": "ChatOpenAI", "kwargs": {"model_name": "gpt-4-turbo"}}
    emitter.on_chat_model_start(serialized, messages=[], run_id="m1")
    emitter.on_tool_start({"name": "fetch"}, "", run_id="t1")
    emitter.on_tool_end("data", run_id="t1")
    assert _ma(emitter.last).get("model_id") == "gpt-4-turbo"


def test_langchain_captured_model_cleared_after_emit(tmp_path):
    """Captured model does not leak into the following tool emit."""
    ledger = tmp_path / "l.jsonl"
    emitter = LangChainCapsuleEmitter(operator="org", developer="agent@v1", ledger=ledger, anchor=False)

    # First tool: capture model before it
    emitter.on_llm_start({"name": "ChatOpenAI", "kwargs": {"model_name": "gpt-4o"}}, prompts=[])
    emitter.on_tool_start({"name": "tool_a"}, "", run_id="a")
    emitter.on_tool_end("out_a", run_id="a")
    first_ma = _ma(emitter.last)
    assert first_ma.get("model_id") == "gpt-4o"

    # Second tool: no on_llm_start → no model on this capsule
    emitter.on_tool_start({"name": "tool_b"}, "", run_id="b")
    emitter.on_tool_end("out_b", run_id="b")
    second_ma = _ma(emitter.last)
    assert not second_ma.get("model_id"), "captured model should not persist to next tool"


def test_langchain_fallback_to_constructor_model_when_no_llm_start(tmp_path):
    """When on_llm_start never fires, falls back to constructor model=."""
    ledger = tmp_path / "l.jsonl"
    emitter = LangChainCapsuleEmitter(
        operator="org",
        developer="agent@v1",
        ledger=ledger,
        anchor=False,
        model={"provider": "anthropic", "model_id": "claude-haiku-4-5"},
    )
    emitter.on_tool_start({"name": "tool"}, "", run_id="x")
    emitter.on_tool_end("out", run_id="x")
    ma = _ma(emitter.last)
    assert ma.get("model_id") == "claude-haiku-4-5"
    assert ma.get("provider") == "anthropic"


def test_langchain_tool_error_clears_pending(tmp_path):
    """on_tool_error removes the pending entry without emitting."""
    ledger = tmp_path / "l.jsonl"
    emitter = LangChainCapsuleEmitter(operator="org", developer="agent@v1", ledger=ledger, anchor=False)
    emitter.on_tool_start({"name": "bad_tool"}, "", run_id="err")
    emitter.on_tool_error(ValueError("boom"), run_id="err")
    assert emitter.last is None
    assert read_ledger(ledger) == []


# ---------------------------------------------------------------------------
# LangChain: _extract_model_from_serialized unit tests
# ---------------------------------------------------------------------------

def test_extract_model_openai():
    d = {"name": "ChatOpenAI", "kwargs": {"model_name": "gpt-4o-mini"}}
    m = _extract_model_from_serialized(d)
    assert m == {"provider": "openai", "model_id": "gpt-4o-mini"}


def test_extract_model_anthropic_kwargs_model():
    d = {"name": "ChatAnthropic", "kwargs": {"model": "claude-3-haiku"}}
    m = _extract_model_from_serialized(d)
    assert m == {"provider": "anthropic", "model_id": "claude-3-haiku"}


def test_extract_model_from_id_tail():
    d = {"id": ["langchain", "chat_models", "mistralai", "ChatMistralAI"], "kwargs": {"model_name": "mistral-small"}}
    m = _extract_model_from_serialized(d)
    assert m is not None
    assert m["model_id"] == "mistral-small"
    assert m["provider"] == "mistral"


def test_extract_model_none_when_empty():
    assert _extract_model_from_serialized(None) is None
    assert _extract_model_from_serialized({}) is None


# ---------------------------------------------------------------------------
# CrewAI: model= at construction auto-fills capsule
# ---------------------------------------------------------------------------

def test_crewai_constructor_model_auto_fills(tmp_path):
    """model= at construction auto-fills every wrap() capsule."""
    ledger = tmp_path / "l.jsonl"
    emitter = CrewAICapsuleEmitter(
        operator="org",
        developer="agent@v1",
        ledger=ledger,
        anchor=False,
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    )

    def my_tool(x: str) -> str:
        return x.upper()

    wrapped = emitter.wrap(my_tool, action="uppercase")
    wrapped("hello")

    ma = _ma(emitter.last)
    assert ma.get("model_id") == "claude-sonnet-4-6"
    assert ma.get("provider") == "anthropic"


def test_crewai_wrap_callable_emits(tmp_path):
    """wrap() on a plain callable emits a capsule with correct action and I/O digests."""
    ledger = tmp_path / "l.jsonl"
    emitter = CrewAICapsuleEmitter(operator="org", developer="agent@v1", ledger=ledger, anchor=False)

    def add(a: int, b: int) -> int:
        return a + b

    wrapped = emitter.wrap(add, action="add_numbers")
    result = wrapped(1, 2)
    assert result == 3

    records = read_ledger(ledger)
    assert len(records) == 1
    aid = records[0].get("action_id", "")
    assert "add_numbers" in aid


def test_crewai_wrap_uses_function_name_when_no_action(tmp_path):
    """wrap() uses the callable's __name__ when action= not specified."""
    ledger = tmp_path / "l.jsonl"
    emitter = CrewAICapsuleEmitter(operator="org", developer="agent@v1", ledger=ledger, anchor=False)

    def my_function(x):
        return x

    emitter.wrap(my_function)("test")
    records = read_ledger(ledger)
    assert "my_function" in records[0].get("action_id", "")


# ---------------------------------------------------------------------------
# MCP: @emitter.tool decorator
# ---------------------------------------------------------------------------

def test_mcp_tool_decorator_emits(tmp_path):
    """@emitter.tool wraps a callable and emits a capsule per call."""
    ledger = tmp_path / "l.jsonl"
    emitter = MCPCapsuleEmitter(operator="org", developer="agent@v1", ledger=ledger, anchor=False)

    @emitter.tool("fetch_price")
    def fetch_price(ticker: str) -> float:
        return 42.0

    result = fetch_price(ticker="AAPL")
    assert result == 42.0

    records = read_ledger(ledger)
    assert len(records) == 1
    assert "fetch_price" in records[0].get("action_id", "")


def test_mcp_tool_no_auto_model_without_explicit(tmp_path):
    """MCP wraps tools, not LLMs — no auto-capture; model is None unless explicit."""
    ledger = tmp_path / "l.jsonl"
    emitter = MCPCapsuleEmitter(operator="org", developer="agent@v1", ledger=ledger, anchor=False)

    @emitter.tool("do_thing")
    def do_thing():
        return "done"

    do_thing()
    ma = _ma(emitter.last)
    assert not ma.get("model_id"), "MCP adapter must not fake model auto-capture"


def test_mcp_tool_explicit_model_via_constructor(tmp_path):
    """MCP adapter respects model= passed at construction."""
    ledger = tmp_path / "l.jsonl"
    emitter = MCPCapsuleEmitter(
        operator="org",
        developer="agent@v1",
        ledger=ledger,
        anchor=False,
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    )

    @emitter.tool("do_thing")
    def do_thing():
        return "done"

    do_thing()
    ma = _ma(emitter.last)
    assert ma.get("model_id") == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Hermes: after_tool emits capsule with effect
# ---------------------------------------------------------------------------

def test_hermes_after_tool_emits_with_effect(tmp_path):
    """after_tool emits a capsule with an effect block."""
    ledger = tmp_path / "l.jsonl"
    emitter = HermesCapsuleEmitter(operator="org", developer="agent@v1", ledger=ledger, anchor=False)
    result = emitter.after_tool("send_email", {"to": "a@b.com"}, {"sent": True})
    assert result.capsule.get("effect", {}).get("status") == "dispatched"
    assert "send_email" in result.capsule.get("action_id", "")


def test_hermes_constructor_model_auto_fills(tmp_path):
    """Hermes adapter respects model= at construction."""
    ledger = tmp_path / "l.jsonl"
    emitter = HermesCapsuleEmitter(
        operator="org",
        developer="agent@v1",
        ledger=ledger,
        anchor=False,
        model={"provider": "openai", "model_id": "gpt-4o"},
    )
    emitter.after_tool("tool", {}, {})
    ma = _ma(emitter.last)
    assert ma.get("model_id") == "gpt-4o"
