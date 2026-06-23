# SPDX-License-Identifier: Apache-2.0
# Copyright 2024 Action State Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import pytest
from agent_action_capsule import verify

from capsule_emit import read_ledger
from capsule_emit.adapters.crewai import CrewAICapsuleEmitter
from capsule_emit.adapters.hermes import HermesCapsuleEmitter
from capsule_emit.adapters.mcp import MCPCapsuleEmitter


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "ledger.jsonl"


def make_emitter(cls, tmp_ledger, **kwargs):
    return cls(
        operator="test-org",
        developer="agent@v1",
        ledger=tmp_ledger,
        anchor=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# MCP adapter
# ---------------------------------------------------------------------------


def test_mcp_tool_decorator_kwargs(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool("do_thing")
    def fn(x, y):
        return {"sum": x + y}

    fn(x=1, y=2)

    assert emitter.last is not None
    assert verify(emitter.last.capsule).ok
    assert len(read_ledger(tmp_ledger)) == 1


def test_mcp_tool_decorator_args_positional(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool("do_thing")
    def fn(x, y):
        return {"sum": x + y}

    fn(1, 2)

    assert verify(emitter.last.capsule).ok


def test_mcp_tool_decorator_no_args(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool("no_args_action")
    def fn():
        return {"status": "ok"}

    fn()

    assert verify(emitter.last.capsule).ok
    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "agent_output_digest" in ca


def test_mcp_tool_decorator_none_return(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool("none_return")
    def fn():
        return None

    fn()

    assert verify(emitter.last.capsule).ok


def test_mcp_tool_decorator_large_return(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool("large_return")
    def fn():
        return {"data": "x" * 50_000}

    fn()

    assert verify(emitter.last.capsule).ok


def test_mcp_tool_raises_no_partial_ledger(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool("raises_action")
    def fn():
        raise ValueError("tool failed")

    assert len(read_ledger(tmp_ledger)) == 0

    with pytest.raises(ValueError, match="tool failed"):
        fn()

    assert len(read_ledger(tmp_ledger)) == 0


def test_mcp_tool_action_defaults_to_fn_name(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool()
    def my_function():
        return {"result": 1}

    my_function()

    capsule = emitter.last.capsule
    assert capsule["action_id"].startswith("my_function/")
    assert verify(capsule).ok


def test_mcp_emit_capsule_direct(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    result = emitter.emit_capsule(
        "my_action",
        tool_input={"k": "v"},
        tool_output={"result": 1},
    )

    assert verify(result.capsule).ok
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert "agent_input_digest" in ca
    assert "agent_output_digest" in ca


def test_mcp_results_accumulate(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool("accumulate")
    def fn():
        return {"n": 1}

    fn()
    fn()

    assert len(emitter.results) == 2


# ---------------------------------------------------------------------------
# CrewAI adapter
# ---------------------------------------------------------------------------


def test_crewai_wrap_callable_kwargs(tmp_ledger):
    emitter = make_emitter(CrewAICapsuleEmitter, tmp_ledger)

    def fn(x):
        return {"val": x}

    wrapped = emitter.wrap(fn)
    wrapped(x=1)

    assert verify(emitter.last.capsule).ok


def test_crewai_wrap_callable_positional(tmp_ledger):
    emitter = make_emitter(CrewAICapsuleEmitter, tmp_ledger)

    def fn(x):
        return {"val": x}

    wrapped = emitter.wrap(fn)
    wrapped(1)

    assert verify(emitter.last.capsule).ok


def test_crewai_wrap_action_override(tmp_ledger):
    emitter = make_emitter(CrewAICapsuleEmitter, tmp_ledger)

    def fn():
        return {"result": "ok"}

    wrapped = emitter.wrap(fn, action="custom_action")
    wrapped()

    capsule = emitter.last.capsule
    assert capsule["action_id"].startswith("custom_action/")
    assert verify(capsule).ok


def test_crewai_wrap_raises_no_partial_ledger(tmp_ledger):
    emitter = make_emitter(CrewAICapsuleEmitter, tmp_ledger)

    def fn():
        raise RuntimeError("crewai tool failed")

    wrapped = emitter.wrap(fn)

    with pytest.raises(RuntimeError, match="crewai tool failed"):
        wrapped()

    assert len(read_ledger(tmp_ledger)) == 0


def test_crewai_wrap_large_output(tmp_ledger):
    emitter = make_emitter(CrewAICapsuleEmitter, tmp_ledger)

    def fn():
        return {"result": "y" * 50_000}

    wrapped = emitter.wrap(fn)
    wrapped()

    assert verify(emitter.last.capsule).ok


def test_crewai_wrap_none_output(tmp_ledger):
    emitter = make_emitter(CrewAICapsuleEmitter, tmp_ledger)

    def fn():
        return None

    wrapped = emitter.wrap(fn)
    wrapped()

    assert verify(emitter.last.capsule).ok


# ---------------------------------------------------------------------------
# Hermes adapter
# ---------------------------------------------------------------------------


def test_hermes_after_tool_basic(tmp_ledger):
    emitter = make_emitter(HermesCapsuleEmitter, tmp_ledger)

    result = emitter.after_tool("my_tool", {"k": "v"}, {"result": "ok"})

    assert verify(result.capsule).ok
    ca = result.capsule["model_attestation"]["compute_attestation"]
    assert "agent_input_digest" in ca
    assert "agent_output_digest" in ca


def test_hermes_after_tool_none_input(tmp_ledger):
    emitter = make_emitter(HermesCapsuleEmitter, tmp_ledger)

    result = emitter.after_tool("tool", None, {"result": "ok"})

    assert verify(result.capsule).ok


def test_hermes_after_tool_none_output_confirmed(tmp_ledger):
    emitter = make_emitter(HermesCapsuleEmitter, tmp_ledger)

    result = emitter.after_tool(
        "tool",
        {"in": 1},
        None,
        verdict="executed",
        effect_status="dispatched",
    )

    assert verify(result.capsule).ok


def test_hermes_after_tool_confirmed_status(tmp_ledger):
    emitter = make_emitter(HermesCapsuleEmitter, tmp_ledger)

    result = emitter.after_tool(
        "confirm_tool",
        {"in": 1},
        {"result": 1},
        verdict="confirmed",
        effect_status="confirmed",
    )

    assert verify(result.capsule).ok


def test_hermes_large_input(tmp_ledger):
    emitter = make_emitter(HermesCapsuleEmitter, tmp_ledger)

    result = emitter.after_tool("large_tool", {"data": "z" * 100_000}, {"status": "ok"})

    assert verify(result.capsule).ok


# ---------------------------------------------------------------------------
# LangChain adapter
# ---------------------------------------------------------------------------


def test_langchain_on_tool_end_basic(tmp_ledger):
    pytest.importorskip("langchain_core")
    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    emitter = make_emitter(LangChainCapsuleEmitter, tmp_ledger)
    emitter.on_tool_start({"name": "search"}, "query", run_id="r1")
    emitter.on_tool_end("result", run_id="r1")

    assert emitter.last is not None
    assert verify(emitter.last.capsule).ok


def test_langchain_on_tool_error_discards_pending(tmp_ledger):
    pytest.importorskip("langchain_core")
    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    emitter = make_emitter(LangChainCapsuleEmitter, tmp_ledger)
    emitter.on_tool_start({"name": "search"}, "query", run_id="r2")
    emitter.on_tool_error(ValueError("err"), run_id="r2")

    assert emitter.last is None
    assert len(read_ledger(tmp_ledger)) == 0


def test_langchain_unknown_run_id_tool_end(tmp_ledger):
    pytest.importorskip("langchain_core")
    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    emitter = make_emitter(LangChainCapsuleEmitter, tmp_ledger)
    emitter.on_tool_end("output", run_id="unknown")

    assert emitter.last is not None
    assert emitter.last.capsule["action_id"].startswith("tool/")


def test_langchain_on_tool_start_no_serialized(tmp_ledger):
    pytest.importorskip("langchain_core")
    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    emitter = make_emitter(LangChainCapsuleEmitter, tmp_ledger)
    emitter.on_tool_start(None, "input", run_id="r3")
    emitter.on_tool_end("output", run_id="r3")

    assert emitter.last is not None
    assert emitter.last.capsule["action_id"].startswith("tool/")
    assert verify(emitter.last.capsule).ok


def test_langchain_large_output(tmp_ledger):
    pytest.importorskip("langchain_core")
    from capsule_emit.adapters.langchain import LangChainCapsuleEmitter

    emitter = make_emitter(LangChainCapsuleEmitter, tmp_ledger)
    emitter.on_tool_start({"name": "fetch"}, "url", run_id="r4")
    emitter.on_tool_end("x" * 50_000, run_id="r4")

    assert verify(emitter.last.capsule).ok


# ---------------------------------------------------------------------------
# Conformance — every adapter capsule verifies
# ---------------------------------------------------------------------------


def test_all_adapters_produce_conforming_capsules(tmp_ledger):
    mcp_emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @mcp_emitter.tool("mcp_conform")
    def mcp_fn():
        return {"ok": True}

    mcp_fn()

    crewai_emitter = make_emitter(CrewAICapsuleEmitter, tmp_ledger)

    def crewai_fn():
        return {"ok": True}

    emitter_wrapped = crewai_emitter.wrap(crewai_fn)
    emitter_wrapped()

    hermes_emitter = make_emitter(HermesCapsuleEmitter, tmp_ledger)
    hermes_emitter.after_tool("hermes_conform", {"in": 1}, {"out": 1})

    capsules = [
        mcp_emitter.last.capsule,
        crewai_emitter.last.capsule,
        hermes_emitter.last.capsule,
    ]

    for capsule in capsules:
        assert verify(capsule).ok


# ---------------------------------------------------------------------------
# Auto-capture IO digests
# ---------------------------------------------------------------------------


def test_mcp_auto_capture_io_digests(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)

    @emitter.tool("capture_test")
    def fn(query):
        return {"items": [1, 2, 3]}

    fn(query="hello")

    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "agent_input_digest" in ca
    assert "agent_output_digest" in ca


def test_crewai_auto_capture_io_digests(tmp_ledger):
    emitter = make_emitter(CrewAICapsuleEmitter, tmp_ledger)

    def fn(query):
        return {"items": [1, 2, 3]}

    wrapped = emitter.wrap(fn)
    wrapped(query="hello")

    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "agent_input_digest" in ca
    assert "agent_output_digest" in ca


def test_hermes_auto_capture_io_digests(tmp_ledger):
    emitter = make_emitter(HermesCapsuleEmitter, tmp_ledger)
    emitter.after_tool("digest_tool", {"query": "hello"}, {"items": [1, 2, 3]})

    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "agent_input_digest" in ca
    assert "agent_output_digest" in ca


# ---------------------------------------------------------------------------
# Regression: IO digests present even when no model= is supplied
# ---------------------------------------------------------------------------


def test_mcp_io_digests_present_without_model(tmp_ledger):
    """Regression: agent_input_digest + agent_output_digest must be committed
    regardless of whether a ``model`` is supplied.

    Previously a silent-drop bug caused digests to be omitted when the caller
    did not pass ``model=``.  This test pins the fixed behaviour.
    """
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)  # no model=

    @emitter.tool("no_model_action")
    def fn(query: str) -> dict:
        return {"result": query.upper()}

    fn(query="hello")

    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "agent_input_digest" in ca, "input digest missing without model="
    assert "agent_output_digest" in ca, "output digest missing without model="


# ---------------------------------------------------------------------------
# Adapter disposition threading
# ---------------------------------------------------------------------------


def test_mcp_emit_capsule_human_disposed(tmp_ledger):
    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)
    result = emitter.emit_capsule(
        "approve_action",
        tool_input={"decision": "yes"},
        tool_output={"approved": True},
        human_disposed=True,
        approver="human",
        decision="accept",
        verdict="confirmed",
        effect={"type": "approve_action", "status": "confirmed"},
    )
    disp = result.capsule["disposition"]
    assert disp["human_disposed"] is True
    assert disp["approver"] == "human"
    assert verify(result.capsule).ok


def test_mcp_emit_capsule_human_disposed_wrong_approver_raises(tmp_ledger):
    from agent_action_capsule.contracts import InvariantError

    emitter = make_emitter(MCPCapsuleEmitter, tmp_ledger)
    with pytest.raises(InvariantError, match="requires approver"):
        emitter.emit_capsule(
            "approve_action",
            human_disposed=True,
            approver="policy",
        )
