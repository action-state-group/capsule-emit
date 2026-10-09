# SPDX-License-Identifier: Apache-2.0
"""OpenAI Agents SDK listener tests — framework-free core + both shells + real SDK.

Sealing logic lives in OpenAIAgentsListenerCore, whose ``open_call``/``close_call``
take plain values, so the full behavior is exercised WITHOUT the SDK installed
(mirrors the CrewAI/LangChain/Agno listener test approach). The two shells are
covered first against duck-typed span/context doubles, then by importorskip'd
tests at the bottom that drive a REAL ``Runner.run`` with a scripted model
(``agents.testing.ScriptedModel``) — no API key, no network.

Covered:
- planned capsule before the tool runs (effect.status="planned"), verifies
- confirmed capsule after it, confirms-chained to the planned id, verifies
- error -> verdict="errored", effect.status="failed", chained (errors are evidence)
- exact pairing under CONCURRENT tool calls, for both shells (no FIFO assumption)
- never-raise guarantee: a broken ledger warns and never disturbs the agent run,
  on every path, for both shells
- the tool's own exception propagates unchanged through a real run
- digest-only privacy: raw argument/output values never reach the ledger
- float args canonicalize and chain (capsule-emit#135 riding the _base funnel)
- the processor's planned capsule declares that the args were NOT observable
  (the SDK assigns FunctionSpanData.input after on_span_start) rather than
  letting an empty payload pass for "no arguments"
- trace_include_sensitive_data=False -> payload absent, RECORDED as absent
- the hooks' outcome capsule declares that RunHooks has no on_tool_error, so a
  return is not evidence of success
- span-type filtering (agent/turn/task spans are not tool calls)
- max_pending bound holds (oldest evicted)
- the shells are genuine TracingProcessor / RunHooksBase subclasses
- observation only: neither shell writes anything back to the SDK
- an outcome with neither a response nor a parent degrades to effect.status
  "dispatched" (and says why) instead of claiming an underivable "confirmed"
"""
from __future__ import annotations

import asyncio
import json
import warnings

import pytest

from capsule_emit.adapters.openai_agents_listener import (
    ARGS_NOT_OBSERVABLE_NOTE,
    NO_ERROR_HOOK_NOTE,
    PAYLOAD_WITHHELD_NOTE,
    UNCHAINED_REASON,
    UNCONFIRMABLE_NOTE,
    OpenAIAgentsCapsuleHooks,
    OpenAIAgentsCapsuleProcessor,
    OpenAIAgentsListenerCore,
)
from capsule_emit.verification import verify_capsule as verify


@pytest.fixture(autouse=True)
def _no_egress(monkeypatch):
    """Hard fence: this module never lets a checkpoint leave the process."""
    monkeypatch.setenv("CAPSULE_WITNESS", "off")


# ---------------------------------------------------------------------------
# Helpers / doubles
# ---------------------------------------------------------------------------


def _core(tmp_path, **kw) -> OpenAIAgentsListenerCore:
    return OpenAIAgentsListenerCore(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _processor(tmp_path, **kw) -> OpenAIAgentsCapsuleProcessor:
    return OpenAIAgentsCapsuleProcessor(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _hooks(tmp_path, **kw) -> OpenAIAgentsCapsuleHooks:
    return OpenAIAgentsCapsuleHooks(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _compute(cap):
    return cap.get("model_attestation", {}).get("compute_attestation", {})


class FakeSpanData:
    """Duck-type of FunctionSpanData (name/input/output/mcp_data + .type)."""

    def __init__(self, name="get_price", input=None, output=None, mcp_data=None, type="function"):
        self.name = name
        self.input = input
        self.output = output
        self.mcp_data = mcp_data
        self.type = type


class FakeSpan:
    """Duck-type of the SDK's Span (span_id / span_data / error)."""

    def __init__(self, span_id="span_1", span_data=None, error=None):
        self.span_id = span_id
        self.span_data = span_data if span_data is not None else FakeSpanData()
        self.error = error


class FakeToolContext:
    """Duck-type of ToolContext (tool_name / tool_arguments / tool_call_id)."""

    def __init__(self, tool_name="get_price", tool_arguments='{"symbol":"ACME"}', tool_call_id="c1"):
        self.tool_name = tool_name
        self.tool_arguments = tool_arguments
        self.tool_call_id = tool_call_id


class FakeTool:
    def __init__(self, name="get_price"):
        self.name = name


ARGS = {"symbol": "ACME", "qty": "2"}


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# The two-record chain (core)
# ---------------------------------------------------------------------------


def test_open_call_seals_a_planned_capsule(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1")
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[0]["effect"]["type"] == "get_price"


def test_close_call_chains_to_the_planned_capsule(tmp_path):
    core = _core(tmp_path)
    planned = core.open_call("get_price", ARGS, key="k1")
    core.close_call("get_price", output="26.25", key="k1")
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["chain"]["parent_capsule_id"] == planned
    assert caps[1]["chain"]["relation"] == "confirms"


def test_confirmed_effect_status_and_verdict(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1")
    core.close_call("get_price", output="26.25", key="k1")
    cap = _ledger(tmp_path)[1]
    assert cap["effect"]["status"] == "confirmed"
    assert cap["disposition"]["verdict_class"] == "executed"


def test_errored_close_seals_failed_and_chains(tmp_path):
    core = _core(tmp_path)
    planned = core.open_call("submit_order", ARGS, key="k1")
    core.close_call("submit_order", output="boom", key="k1", errored=True)
    cap = _ledger(tmp_path)[1]
    assert cap["effect"]["status"] == "failed"
    assert cap["disposition"]["verdict_class"] == "errored"
    assert cap["chain"]["parent_capsule_id"] == planned


def test_every_capsule_verifies(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1")
    core.close_call("get_price", output="26.25", key="k1")
    for cap in _ledger(tmp_path):
        assert verify(cap).ok, verify(cap).errors


def test_runtime_is_openai_agents(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1")
    assert _compute(_ledger(tmp_path)[0])["runtime"] == "openai-agents"


def test_action_type_is_fyi(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1")
    assert _ledger(tmp_path)[0]["action_type"] == "fyi"


def test_observation_mode_is_stamped_on_every_capsule(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1")
    core.close_call("get_price", output="x", key="k1")
    for cap in _ledger(tmp_path):
        assert _compute(cap)["observation_mode"] == "event_stream"


def test_confirmed_carries_a_response_digest(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1")
    core.close_call("get_price", output="26.25", key="k1")
    assert _ledger(tmp_path)[1]["effect"]["response_digest"]


def test_planned_commits_to_the_arguments_when_observable(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1")
    assert _compute(_ledger(tmp_path)[0])["agent_input_digest"]


def test_planned_declares_when_arguments_were_not_observable(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1", args_observable=False)
    comp = _compute(_ledger(tmp_path)[0])
    assert comp["args_observable"] is False
    assert comp["args_note"] == ARGS_NOT_OBSERVABLE_NOTE
    assert "agent_input_digest" not in comp


def test_unobservable_args_are_not_sealed_even_if_passed(tmp_path):
    """Absent is recorded as absent — never quietly backfilled."""
    core = _core(tmp_path)
    core.open_call("get_price", ARGS, key="k1", args_observable=False)
    assert "agent_input_digest" not in _compute(_ledger(tmp_path)[0])


# ---------------------------------------------------------------------------
# Pairing, concurrency, bounds
# ---------------------------------------------------------------------------


def test_interleaved_calls_chain_to_the_right_parent(tmp_path):
    core = _core(tmp_path)
    a = core.open_call("t", {"i": "a"}, key="A")
    b = core.open_call("t", {"i": "b"}, key="B")
    c = core.open_call("t", {"i": "c"}, key="C")
    # close out of order, exactly as concurrent tool tasks would
    core.close_call("t", output="c", key="C")
    core.close_call("t", output="a", key="A")
    core.close_call("t", output="b", key="B")
    caps = _ledger(tmp_path)
    parents = [cap["chain"]["parent_capsule_id"] for cap in caps[3:]]
    assert parents == [c, a, b]


def test_pending_is_emptied_as_calls_close(tmp_path):
    core = _core(tmp_path)
    core.open_call("t", {}, key="A")
    core.open_call("t", {}, key="B")
    assert core.pending == 2
    core.close_call("t", key="A")
    core.close_call("t", key="B")
    assert core.pending == 0


def test_close_without_a_matching_open_is_unchained_not_crashing(tmp_path):
    core = _core(tmp_path)
    core.close_call("t", output="x", key="nope")
    assert _ledger(tmp_path)[0].get("chain", {}).get("parent_capsule_id") is None


def test_max_pending_bound_evicts_oldest(tmp_path):
    core = _core(tmp_path, max_pending=2)
    core.open_call("t", {}, key="A")
    core.open_call("t", {}, key="B")
    core.open_call("t", {}, key="C")
    assert core.pending <= 2
    # A was evicted, so its outcome is honestly unchained rather than mis-chained
    core.close_call("t", key="A")
    assert _ledger(tmp_path)[-1].get("chain", {}).get("parent_capsule_id") is None


def test_forget_returns_and_drops_the_pending_id(tmp_path):
    core = _core(tmp_path)
    planned = core.open_call("t", {}, key="A")
    assert core.forget("A") == planned
    assert core.forget("A") is None


def test_calls_without_a_key_still_seal_both_records(tmp_path):
    core = _core(tmp_path)
    core.open_call("t", {}, key=None)
    core.close_call("t", output="x", key=None)
    assert len(_ledger(tmp_path)) == 2


# ---------------------------------------------------------------------------
# Privacy, floats, fail-closed
# ---------------------------------------------------------------------------


def test_raw_inputs_and_outputs_never_reach_the_ledger(tmp_path):
    core = _core(tmp_path)
    core.open_call("get_price", {"secret": "hunter2"}, key="k1")
    core.close_call("get_price", output={"token": "sk-live-abcdef"}, key="k1")
    blob = (tmp_path / "ledger.jsonl").read_text()
    assert "hunter2" not in blob
    assert "sk-live-abcdef" not in blob


def test_float_args_seal_and_chain(tmp_path):
    """capsule-emit#135: the _base funnel canonicalizes floats, so calls chain."""
    core = _core(tmp_path)
    planned = core.open_call("get_price", {"qty": 2.5, "rate": 0.1}, key="k1")
    core.close_call("get_price", output="ok", key="k1")
    caps = _ledger(tmp_path)
    assert planned is not None
    assert len(caps) == 2
    assert caps[1]["chain"]["parent_capsule_id"] == planned


def test_float_output_seals_and_chains(tmp_path):
    core = _core(tmp_path)
    planned = core.open_call("get_price", {"symbol": "ACME"}, key="k1")
    core.close_call("get_price", output={"price": 26.25}, key="k1")
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["chain"]["parent_capsule_id"] == planned


def test_nested_floats_in_arguments_chain(tmp_path):
    core = _core(tmp_path)
    planned = core.open_call("t", {"legs": [{"px": 1.5}, {"px": 2.25}]}, key="k1")
    core.close_call("t", output="ok", key="k1")
    assert _ledger(tmp_path)[1]["chain"]["parent_capsule_id"] == planned


def test_uncanonicalizable_payload_fails_closed_without_crashing(tmp_path):
    """NaN has no JCS form: warn, seal nothing for that record, run unaffected."""
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        planned = core.open_call("t", {"x": float("nan")}, key="k1")
    assert planned is None
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_outcome_still_seals_after_an_unsealable_planned(tmp_path):
    core = _core(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        core.open_call("t", {"x": float("inf")}, key="k1")
    core.close_call("t", output="ok", key="k1", planned_dropped=True)
    cap = _ledger(tmp_path)[-1]
    assert _compute(cap)["unchained_reason"] == UNCHAINED_REASON


def test_outcome_with_no_response_and_no_parent_degrades_to_dispatched(tmp_path):
    """§5.2: "confirmed" needs a response_digest. With no output and no parent
    there is nothing to derive one from, so the claim degrades and the evidence
    survives — rather than the seal raising and the record being lost."""
    core = _core(tmp_path)
    core.close_call("t", output=None, key="orphan")
    cap = _ledger(tmp_path)[0]
    assert cap["effect"]["status"] == "dispatched"
    assert _compute(cap)["outcome_unconfirmable"] is True
    assert _compute(cap)["outcome_note"] == UNCONFIRMABLE_NOTE


def test_no_response_but_a_parent_still_confirms(tmp_path):
    """The parent capsule id is itself a derivable response_digest source."""
    core = _core(tmp_path)
    core.open_call("t", {}, key="k1")
    core.close_call("t", output=None, key="k1")
    cap = _ledger(tmp_path)[1]
    assert cap["effect"]["status"] == "confirmed"
    assert cap["effect"]["response_digest"]
    assert "outcome_unconfirmable" not in _compute(cap)


def test_a_response_with_no_parent_still_confirms(tmp_path):
    core = _core(tmp_path)
    core.close_call("t", output="26.25", key="orphan")
    cap = _ledger(tmp_path)[0]
    assert cap["effect"]["status"] == "confirmed"
    assert "outcome_unconfirmable" not in _compute(cap)


def test_errored_outcome_never_degrades_to_dispatched(tmp_path):
    """A failure is always recordable — "failed" carries no digest requirement."""
    core = _core(tmp_path)
    core.close_call("t", output=None, key="orphan", errored=True)
    cap = _ledger(tmp_path)[0]
    assert cap["effect"]["status"] == "failed"
    assert cap["disposition"]["verdict_class"] == "errored"


def test_evicted_pending_outcome_is_still_sealed(tmp_path):
    """The bound must not cost us the record — the regression this path fixes."""
    core = _core(tmp_path, max_pending=2)
    core.open_call("t", {}, key="A")
    core.open_call("t", {}, key="B")
    core.open_call("t", {}, key="C")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.close_call("t", key="A")
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []
    assert _ledger(tmp_path)[-1]["effect"]["status"] == "dispatched"


def test_processor_withheld_payload_with_a_parent_still_confirms(tmp_path):
    """sensitive-data-off: the chain still supplies a derivable response_digest."""
    p = _processor(tmp_path)
    span = FakeSpan(span_id="s1")
    p.on_span_start(span)
    p.on_span_end(span)
    cap = _ledger(tmp_path)[1]
    assert cap["effect"]["status"] == "confirmed"
    assert "outcome_unconfirmable" not in _compute(cap)


def test_seal_failure_warns_and_never_raises(tmp_path, monkeypatch):
    core = _core(tmp_path)

    def boom(**_kw):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(core, "emit_capsule", boom)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert core.open_call("t", {}, key="k1") is None
        assert core.close_call("t", output="x", key="k1") is None
    assert len(caught) == 2
    assert all(issubclass(w.category, RuntimeWarning) for w in caught)


# ---------------------------------------------------------------------------
# The TracingProcessor shell
# ---------------------------------------------------------------------------


def test_processor_is_a_real_tracing_processor_subclass():
    agents_tracing = pytest.importorskip("agents.tracing")
    assert issubclass(OpenAIAgentsCapsuleProcessor, agents_tracing.TracingProcessor)


def test_processor_seals_planned_on_span_start(tmp_path):
    p = _processor(tmp_path)
    p.on_span_start(FakeSpan(span_id="s1"))
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "planned"


def test_processor_planned_declares_args_not_observable(tmp_path):
    p = _processor(tmp_path)
    p.on_span_start(FakeSpan(span_id="s1"))
    comp = _compute(_ledger(tmp_path)[0])
    assert comp["args_observable"] is False
    assert comp["args_note"] == ARGS_NOT_OBSERVABLE_NOTE


def test_processor_seals_confirmed_on_span_end_and_chains(tmp_path):
    p = _processor(tmp_path)
    span = FakeSpan(span_id="s1")
    p.on_span_start(span)
    span.span_data.input = '{"symbol":"ACME"}'
    span.span_data.output = "26.25"
    p.on_span_end(span)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["effect"]["status"] == "confirmed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_processor_seals_the_late_arriving_arguments_on_the_outcome(tmp_path):
    p = _processor(tmp_path)
    span = FakeSpan(span_id="s1")
    p.on_span_start(span)
    span.span_data.input = '{"symbol":"ACME"}'
    span.span_data.output = "26.25"
    p.on_span_end(span)
    assert _compute(_ledger(tmp_path)[1])["agent_input_digest"]


def test_processor_span_error_seals_failed(tmp_path):
    p = _processor(tmp_path)
    span = FakeSpan(span_id="s1")
    p.on_span_start(span)
    span.span_data.input = '{"x":"a"}'
    span.span_data.output = "An error occurred while running the tool."
    span.error = {"message": "Error running tool (non-fatal)",
                  "data": {"tool_name": "boom", "error": "tool exploded"}}
    p.on_span_end(span)
    cap = _ledger(tmp_path)[1]
    assert cap["effect"]["status"] == "failed"
    assert cap["disposition"]["verdict_class"] == "errored"


def test_processor_records_withheld_payload_as_absent(tmp_path):
    """trace_include_sensitive_data=False: absent is recorded, not passed as empty."""
    p = _processor(tmp_path)
    span = FakeSpan(span_id="s1")
    p.on_span_start(span)
    p.on_span_end(span)  # input/output still None
    comp = _compute(_ledger(tmp_path)[1])
    assert comp["payload_withheld"] is True
    assert comp["payload_note"] == PAYLOAD_WITHHELD_NOTE


def test_processor_does_not_flag_withheld_when_payload_is_present(tmp_path):
    p = _processor(tmp_path)
    span = FakeSpan(span_id="s1")
    p.on_span_start(span)
    span.span_data.input = '{"a":1}'
    span.span_data.output = "x"
    p.on_span_end(span)
    assert "payload_withheld" not in _compute(_ledger(tmp_path)[1])


def test_processor_ignores_non_function_spans(tmp_path):
    p = _processor(tmp_path)
    for kind in ("agent", "turn", "task", "generation", "response"):
        span = FakeSpan(span_id="s-" + kind, span_data=FakeSpanData(type=kind))
        p.on_span_start(span)
        p.on_span_end(span)
    assert _ledger(tmp_path) == []


def test_processor_ignores_traces(tmp_path):
    p = _processor(tmp_path)
    p.on_trace_start(object())
    p.on_trace_end(object())
    assert _ledger(tmp_path) == []


def test_processor_tolerates_a_foreign_span_object(tmp_path):
    p = _processor(tmp_path)
    p.on_span_start(object())
    p.on_span_end(object())
    assert _ledger(tmp_path) == []


def test_processor_pairs_concurrent_spans_by_span_id(tmp_path):
    p = _processor(tmp_path)
    spans = [FakeSpan(span_id=f"s{i}", span_data=FakeSpanData(name="slow")) for i in range(3)]
    for s in spans:  # all three start before any ends — the measured SDK order
        p.on_span_start(s)
    planned = [c["capsule_id"] for c in _ledger(tmp_path)]
    for s, tag in zip(reversed(spans), ("C", "B", "A")):
        s.span_data.output = tag
        p.on_span_end(s)
    outcomes = _ledger(tmp_path)[3:]
    assert [c["chain"]["parent_capsule_id"] for c in outcomes] == list(reversed(planned))


def test_processor_carries_mcp_data_when_present(tmp_path):
    p = _processor(tmp_path)
    span = FakeSpan(span_id="s1", span_data=FakeSpanData(mcp_data={"server": "files"}))
    p.on_span_start(span)
    span.span_data.output = "ok"
    p.on_span_end(span)
    assert _compute(_ledger(tmp_path)[1])["mcp_data"] == {"server": "files"}


def test_processor_shutdown_and_force_flush_are_safe_noops(tmp_path):
    p = _processor(tmp_path)
    p.shutdown()
    p.force_flush()
    assert _ledger(tmp_path) == []


def test_processor_never_raises_on_a_broken_ledger(tmp_path, monkeypatch):
    p = _processor(tmp_path)

    def boom(**_kw):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(p.core, "emit_capsule", boom)
    span = FakeSpan(span_id="s1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        p.on_span_start(span)
        p.on_span_end(span)
    assert len(caught) == 2


def test_processor_writes_nothing_back_to_the_span(tmp_path):
    """Observation only: the span object is unchanged after both callbacks."""
    p = _processor(tmp_path)
    span = FakeSpan(span_id="s1")
    before = (span.span_id, span.span_data.name, span.span_data.input,
              span.span_data.output, span.error)
    p.on_span_start(span)
    p.on_span_end(span)
    after = (span.span_id, span.span_data.name, span.span_data.input,
             span.span_data.output, span.error)
    assert before == after


def test_processor_exposes_last_and_results(tmp_path):
    p = _processor(tmp_path)
    p.on_span_start(FakeSpan(span_id="s1"))
    assert p.last is not None
    assert len(p.results) == 1


# ---------------------------------------------------------------------------
# The RunHooks shell
# ---------------------------------------------------------------------------


def test_hooks_is_a_real_run_hooks_subclass():
    lifecycle = pytest.importorskip("agents.lifecycle")
    assert issubclass(OpenAIAgentsCapsuleHooks, lifecycle.RunHooksBase)


def test_hooks_seal_planned_with_the_arguments(tmp_path):
    h = _hooks(tmp_path)
    _run(h.on_tool_start(FakeToolContext(), None, FakeTool()))
    cap = _ledger(tmp_path)[0]
    assert cap["effect"]["status"] == "planned"
    assert _compute(cap)["agent_input_digest"]
    assert _compute(cap).get("args_observable") is not False


def test_hooks_chain_the_outcome_to_the_planned(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeToolContext()
    _run(h.on_tool_start(ctx, None, FakeTool()))
    _run(h.on_tool_end(ctx, None, FakeTool(), "26.25"))
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert caps[1]["effect"]["status"] == "confirmed"


def test_hooks_outcome_declares_the_missing_error_hook(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeToolContext()
    _run(h.on_tool_start(ctx, None, FakeTool()))
    _run(h.on_tool_end(ctx, None, FakeTool(), "ok"))
    assert _compute(_ledger(tmp_path)[1])["verdict_note"] == NO_ERROR_HOOK_NOTE


def test_hooks_pair_concurrent_calls_by_tool_call_id(tmp_path):
    h = _hooks(tmp_path)
    ctxs = [FakeToolContext(tool_name="slow", tool_arguments=f'{{"tag":"{t}"}}',
                            tool_call_id=f"c{t}") for t in "ABC"]
    for c in ctxs:  # all starts precede any end — the measured SDK order
        _run(h.on_tool_start(c, None, FakeTool("slow")))
    planned = [c["capsule_id"] for c in _ledger(tmp_path)]
    for c in reversed(ctxs):
        _run(h.on_tool_end(c, None, FakeTool("slow"), "ok"))
    outcomes = _ledger(tmp_path)[3:]
    assert [c["chain"]["parent_capsule_id"] for c in outcomes] == list(reversed(planned))


def test_hooks_decode_json_arguments_into_a_mapping(tmp_path):
    """A decoded mapping lets the funnel canonicalize floats inside it."""
    h = _hooks(tmp_path)
    ctx = FakeToolContext(tool_arguments='{"qty": 2.5}')
    _run(h.on_tool_start(ctx, None, FakeTool()))
    assert _compute(_ledger(tmp_path)[0])["agent_input_digest"]


def test_hooks_tolerate_undecodable_arguments(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeToolContext(tool_arguments="not json at all")
    _run(h.on_tool_start(ctx, None, FakeTool()))
    assert _ledger(tmp_path)[0]["effect"]["status"] == "planned"


def test_hooks_fall_back_to_the_tool_name_on_a_plain_context(tmp_path):
    """Other local tool families hand over a plain RunContextWrapper."""
    h = _hooks(tmp_path)
    _run(h.on_tool_start(object(), None, FakeTool("web_search")))
    assert _ledger(tmp_path)[0]["effect"]["type"] == "web_search"


def test_hooks_capture_the_model_from_a_string_model(tmp_path):
    h = _hooks(tmp_path)

    class Agent:
        model = "gpt-5"

    _run(h.on_tool_start(FakeToolContext(), Agent(), FakeTool()))
    assert _ledger(tmp_path)[0]["model_attestation"]["model_id"] == "gpt-5"


def test_hooks_tolerate_an_agent_without_a_model(tmp_path):
    h = _hooks(tmp_path)
    _run(h.on_tool_start(FakeToolContext(), object(), FakeTool()))
    assert _ledger(tmp_path)[0]["effect"]["status"] == "planned"


def test_hooks_never_raise_on_a_broken_ledger(tmp_path, monkeypatch):
    h = _hooks(tmp_path)

    def boom(**_kw):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(h.core, "emit_capsule", boom)
    ctx = FakeToolContext()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _run(h.on_tool_start(ctx, None, FakeTool()))
        _run(h.on_tool_end(ctx, None, FakeTool(), "x"))
    assert len(caught) == 2


def test_hooks_write_nothing_back_to_the_context(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeToolContext()
    before = (ctx.tool_name, ctx.tool_arguments, ctx.tool_call_id)
    _run(h.on_tool_start(ctx, None, FakeTool()))
    _run(h.on_tool_end(ctx, None, FakeTool(), "x"))
    assert (ctx.tool_name, ctx.tool_arguments, ctx.tool_call_id) == before


def test_hooks_inherit_the_other_lifecycle_methods_as_noops(tmp_path):
    """A future SDK hook must not break this class."""
    lifecycle = pytest.importorskip("agents.lifecycle")
    h = _hooks(tmp_path)
    for name in ("on_llm_start", "on_llm_end", "on_agent_start", "on_agent_end", "on_handoff"):
        assert hasattr(h, name)
        assert getattr(type(h), name) is getattr(lifecycle.RunHooksBase, name)


def test_a_second_listener_on_the_same_ledger_warns(tmp_path):
    import capsule_emit.adapters.openai_agents_listener as mod

    mod._LEDGERS_SEEN.clear()
    _processor(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _hooks(tmp_path)
    assert any("two chains" in str(w.message) for w in caught)
    mod._LEDGERS_SEEN.clear()


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
