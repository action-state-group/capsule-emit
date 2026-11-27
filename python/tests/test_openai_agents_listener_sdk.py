# SPDX-License-Identifier: Apache-2.0
"""OpenAI Agents SDK listener tests against the real SDK: hermetic
``Runner.run`` with a scripted model (no API key, no network). The
framework-free core is tested in ``test_openai_agents_listener.py``.
"""
from __future__ import annotations

import asyncio
import warnings

import pytest
from test_openai_agents_listener import (  # helpers shared with the framework-free tests
    _compute,
    _hooks,
    _ledger,
    _processor,
)

from capsule_emit.adapters.openai_agents_listener import (
    NO_ERROR_HOOK_NOTE,
)
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Real SDK — hermetic Runner.run with a scripted model (no API key, no network)
# ---------------------------------------------------------------------------

agents = pytest.importorskip("agents")
_testing = pytest.importorskip("agents.testing")


@pytest.fixture
def _clean_processors():
    from agents.tracing import set_trace_processors

    set_trace_processors([])
    yield
    set_trace_processors([])


def _build_agent(tools):
    from agents import Agent
    from agents.testing import ScriptedModel, assistant_message, function_call

    def make(script):
        return Agent(name="probe", model=ScriptedModel(script), tools=tools)

    return make, function_call, assistant_message


@agents.function_tool
def get_price(symbol: str, qty: float) -> str:
    return f"{symbol}:{qty * 10.5}"


@agents.function_tool
def boom(x: str) -> str:
    raise ValueError("tool exploded")


def _script(*calls):
    from agents.testing import assistant_message, function_call

    return [[function_call(n, a, call_id=c) for n, a, c in calls], [assistant_message("done")]]


def _real_run(agent, *, hooks=None, sensitive=True):
    from agents import RunConfig, Runner

    return asyncio.run(
        Runner.run(agent, "go", hooks=hooks,
                   run_config=RunConfig(trace_include_sensitive_data=sensitive))
    )


def _agent(tools=None):
    from agents import Agent
    from agents.testing import ScriptedModel

    def build(script):
        return Agent(name="probe", model=ScriptedModel(script),
                     tools=tools if tools is not None else [get_price, boom])

    return build


def test_real_run_processor_seals_the_chain(tmp_path, _clean_processors):
    from agents.tracing import set_trace_processors

    p = _processor(tmp_path)
    set_trace_processors([p])
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.0}, "c1")))
    _real_run(agent)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "confirmed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_real_run_processor_planned_has_no_arguments(tmp_path, _clean_processors):
    """The measured SDK ordering, asserted against the real runtime."""
    from agents.tracing import set_trace_processors

    p = _processor(tmp_path)
    set_trace_processors([p])
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.0}, "c1")))
    _real_run(agent)
    comp = _compute(_ledger(tmp_path)[0])
    assert comp["args_observable"] is False
    assert "agent_input_digest" not in comp


def test_real_run_processor_detects_a_failing_tool(tmp_path, _clean_processors):
    """The verdict the hooks cannot see: failure_error_function absorbs the raise."""
    from agents.tracing import set_trace_processors

    p = _processor(tmp_path)
    set_trace_processors([p])
    agent = _agent()(_script(("boom", {"x": "a"}, "e1")))
    _real_run(agent)
    cap = _ledger(tmp_path)[1]
    assert cap["effect"]["status"] == "failed"
    assert cap["disposition"]["verdict_class"] == "errored"


def test_real_run_hooks_cannot_see_the_failure(tmp_path, _clean_processors):
    """The documented limit, asserted so a future SDK change trips this test."""
    h = _hooks(tmp_path)
    agent = _agent()(_script(("boom", {"x": "a"}, "e1")))
    _real_run(agent, hooks=h)
    cap = _ledger(tmp_path)[1]
    assert cap["effect"]["status"] == "confirmed"
    assert _compute(cap)["verdict_note"] == NO_ERROR_HOOK_NOTE


def test_real_run_hooks_seal_the_arguments_before_the_tool_runs(tmp_path, _clean_processors):
    h = _hooks(tmp_path)
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.0}, "c1")))
    _real_run(agent, hooks=h)
    caps = _ledger(tmp_path)
    assert caps[0]["effect"]["status"] == "planned"
    assert _compute(caps[0])["agent_input_digest"]


def test_real_run_float_arguments_chain(tmp_path, _clean_processors):
    """post-#135: a raw float from the model's JSON args seals and chains."""
    h = _hooks(tmp_path)
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.5}, "c1")))
    _real_run(agent, hooks=h)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_real_run_concurrent_tool_calls_chain_pairwise(tmp_path, _clean_processors):
    from agents.tracing import set_trace_processors

    p = _processor(tmp_path)
    set_trace_processors([p])
    agent = _agent()(_script(
        ("get_price", {"symbol": "A", "qty": 1.0}, "c1"),
        ("get_price", {"symbol": "B", "qty": 2.0}, "c2"),
        ("get_price", {"symbol": "C", "qty": 3.0}, "c3"),
    ))
    _real_run(agent)
    caps = _ledger(tmp_path)
    assert len(caps) == 6
    planned = {c["capsule_id"] for c in caps if c["effect"]["status"] == "planned"}
    outcomes = [c for c in caps if c["effect"]["status"] == "confirmed"]
    assert len(planned) == 3 and len(outcomes) == 3
    parents = {c["chain"]["parent_capsule_id"] for c in outcomes}
    assert parents == planned  # each outcome chained to a distinct planned capsule


def test_real_run_hooks_concurrent_tool_calls_chain_pairwise(tmp_path, _clean_processors):
    h = _hooks(tmp_path)
    agent = _agent()(_script(
        ("get_price", {"symbol": "A", "qty": 1.0}, "c1"),
        ("get_price", {"symbol": "B", "qty": 2.0}, "c2"),
        ("get_price", {"symbol": "C", "qty": 3.0}, "c3"),
    ))
    _real_run(agent, hooks=h)
    caps = _ledger(tmp_path)
    planned = {c["capsule_id"] for c in caps if c["effect"]["status"] == "planned"}
    parents = {c["chain"]["parent_capsule_id"] for c in caps
               if c["effect"]["status"] == "confirmed"}
    assert len(planned) == 3
    assert parents == planned


def test_real_run_sensitive_data_off_records_the_absence(tmp_path, _clean_processors):
    from agents.tracing import set_trace_processors

    p = _processor(tmp_path)
    set_trace_processors([p])
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.0}, "c1")))
    _real_run(agent, sensitive=False)
    comp = _compute(_ledger(tmp_path)[1])
    assert comp["payload_withheld"] is True
    assert "agent_output_digest" not in comp


def test_real_run_hooks_unaffected_by_sensitive_data_off(tmp_path, _clean_processors):
    h = _hooks(tmp_path)
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.0}, "c1")))
    _real_run(agent, hooks=h, sensitive=False)
    caps = _ledger(tmp_path)
    assert _compute(caps[0])["agent_input_digest"]
    assert _compute(caps[1])["agent_output_digest"]


def test_real_run_broken_listener_does_not_break_the_run(tmp_path, _clean_processors):
    """The never-raises guarantee, against the real runtime."""
    from agents.tracing import set_trace_processors

    p = _processor(tmp_path)

    def bad(**_kw):
        raise RuntimeError("ledger on fire")

    p.core.emit_capsule = bad  # type: ignore[method-assign]
    set_trace_processors([p])
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.0}, "c1")))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _real_run(agent)
    assert result.final_output == "done"


def test_real_run_broken_hooks_do_not_break_the_run(tmp_path, _clean_processors):
    h = _hooks(tmp_path)

    def bad(**_kw):
        raise RuntimeError("ledger on fire")

    h.core.emit_capsule = bad  # type: ignore[method-assign]
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.0}, "c1")))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _real_run(agent, hooks=h)
    assert result.final_output == "done"


def test_real_run_tool_exception_is_still_absorbed_by_the_sdk(tmp_path, _clean_processors):
    """The listener adds no exception of its own; the SDK's own policy still holds."""
    h = _hooks(tmp_path)
    agent = _agent()(_script(("boom", {"x": "a"}, "e1")))
    result = _real_run(agent, hooks=h)
    assert result.final_output == "done"


def test_real_run_raw_payloads_never_reach_the_ledger(tmp_path, _clean_processors):
    h = _hooks(tmp_path)
    agent = _agent()(_script(("get_price", {"symbol": "SECRETSYM", "qty": 2.0}, "c1")))
    _real_run(agent, hooks=h)
    assert "SECRETSYM" not in (tmp_path / "ledger.jsonl").read_text()


def test_real_run_add_trace_processor_accepts_the_processor(tmp_path, _clean_processors):
    """The shelf's acceptance test: registration through the documented API."""
    from agents.tracing import add_trace_processor

    p = _processor(tmp_path)
    add_trace_processor(p)  # must not raise
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.0}, "c1")))
    _real_run(agent)
    assert len(_ledger(tmp_path)) == 2


def test_real_run_every_capsule_verifies(tmp_path, _clean_processors):
    h = _hooks(tmp_path)
    agent = _agent()(_script(("get_price", {"symbol": "ACME", "qty": 2.5}, "c1")))
    _real_run(agent, hooks=h)
    caps = _ledger(tmp_path)
    assert caps
    for cap in caps:
        assert verify(cap).ok, verify(cap).errors
