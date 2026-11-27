# SPDX-License-Identifier: Apache-2.0
"""LlamaIndex span listener tests — framework-free core + optional shell.

Sealing logic lives in LlamaIndexListenerCore, whose ``on_span_enter`` /
``on_span_exit`` / ``on_span_drop`` take plain duck-typed values, so the full
behavior is exercised WITHOUT llama-index installed (mirrors the
CrewAI/LangChain/agno/Strands listener test approach). The span-handler shell is
covered in ``test_llamaindex_listener_sdk.py``, which drives a REAL
``FunctionAgent`` against a scripted no-network model.

Covered:
- planned capsule on span enter (effect.status="planned"), verifies
- confirmed capsule on span exit, confirms-chained to the planned id, verifies
- ToolOutput.is_error → verdict="errored", effect.status="failed", chained
  (errors are evidence)
- span drop → verdict="errored", effect.status="failed", chained, error type recorded
- pairing is by SPAN id, not tool_id: interleaved concurrent calls chain correctly,
  and a model that reuses a tool_id still chains correctly
- shape-based detection: the two adjacent spans that would double-count
  (aggregate_tool_results, which enters with a ToolCallResult; FunctionTool.acall,
  which exits with a bare ToolOutput) are both ignored
- non-tool spans are ignored entirely
- never-raise guarantee: a broken ledger warns (RuntimeWarning) and seals nothing;
  the caller is never affected
- digest-only privacy: raw argument/output values never reach the ledger
- payload projection: bytes and un-encodable objects become markers; raw_input is
  not duplicated into the outcome capsule
- floats fail closed (no capsule) but do not crash
- model capture is root-scoped; capture_model=False disables it; explicit model= wins
- observation-only: bound args and the result object are never mutated
- max_pending bound holds for all three tables (oldest evicted)
- shell: install()/uninstall() on a real dispatcher, and a real FunctionAgent run
  produces the planned/confirmed chain end to end
"""
from __future__ import annotations

import json
import warnings

from capsule_emit.adapters.llamaindex_listener import (
    LlamaIndexCapsuleListener,
    LlamaIndexListenerCore,
)
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Duck-typed stand-ins for the llama-index payload objects
# ---------------------------------------------------------------------------


class FakeToolCall:
    """Shaped like ``llama_index.core.agent.workflow.workflow_events.ToolCall``."""

    def __init__(self, tool_name, tool_kwargs, tool_id):
        self.tool_name = tool_name
        self.tool_kwargs = tool_kwargs
        self.tool_id = tool_id


class FakeToolOutput:
    """Shaped like ``llama_index.core.tools.types.ToolOutput``."""

    def __init__(self, content, *, raw_input=None, raw_output=None, is_error=False):
        self.content = content
        self.raw_input = raw_input
        self.raw_output = raw_output
        self.is_error = is_error


class FakeToolCallResult:
    """Shaped like ``...workflow_events.ToolCallResult``."""

    def __init__(self, tool_name, tool_kwargs, tool_id, tool_output, return_direct=False):
        self.tool_name = tool_name
        self.tool_kwargs = tool_kwargs
        self.tool_id = tool_id
        self.tool_output = tool_output
        self.return_direct = return_direct


class FakeLLM:
    """Shaped like a llama-index LLM: ``.metadata.model_name``."""

    class _Meta:
        def __init__(self, name):
            self.model_name = name

    def __init__(self, name="gpt-test-1"):
        self.metadata = self._Meta(name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _core(tmp_path, **kw) -> LlamaIndexListenerCore:
    return LlamaIndexListenerCore(
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


def _call(core, span_id, name="get_price", kwargs=None, tool_id="tc-1"):
    core.on_span_enter(span_id, {"ev": FakeToolCall(name, kwargs or {"sku": "SKU-9"}, tool_id)})


def _ok_result(core, span_id, name="get_price", tool_id="tc-1", content="4225 cents", **kw):
    core.on_span_exit(
        span_id,
        FakeToolCallResult(name, {"sku": "SKU-9"}, tool_id, FakeToolOutput(content), **kw),
    )


def _err_result(core, span_id, name="get_price", tool_id="tc-1", content="boom"):
    core.on_span_exit(
        span_id,
        FakeToolCallResult(
            name, {"sku": "SKU-9"}, tool_id, FakeToolOutput(content, is_error=True)
        ),
    )


def _compute(cap):
    return cap.get("model_attestation", {}).get("compute_attestation", {})


# ---------------------------------------------------------------------------
# The two-record chain
# ---------------------------------------------------------------------------


def test_span_enter_seals_a_planned_capsule(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1")

    caps = _ledger(tmp_path)
    assert len(caps) == 1
    cap = caps[0]
    assert cap["effect"]["status"] == "planned"
    assert cap["effect"]["type"] == "get_price"
    assert cap["action_id"].startswith("get_price/")
    assert cap["action_type"] == "fyi"
    assert verify(cap).ok


def test_span_exit_seals_a_confirmed_capsule_chained_to_the_planned_one(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1")
    _ok_result(core, "span-1")

    caps = _ledger(tmp_path)
    assert len(caps) == 2
    planned, confirmed = caps
    assert planned["effect"]["status"] == "planned"
    assert confirmed["effect"]["status"] == "confirmed"
    assert confirmed["chain"]["parent_capsule_id"] == planned["capsule_id"]
    assert confirmed["chain"]["relation"] == "confirms"
    assert confirmed["assurance"]["ledger_mode"] == "chained"
    assert all(verify(c).ok for c in caps)


def test_tool_error_is_evidence_not_silence(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1", name="submit_order")
    _err_result(core, "span-1", name="submit_order", content="ERP rejected the order")

    caps = _ledger(tmp_path)
    assert len(caps) == 2
    failed = caps[1]
    assert failed["disposition"]["verdict_class"] == "errored"
    assert failed["effect"]["status"] == "failed"
    assert failed["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert verify(failed).ok


def test_span_drop_seals_a_failed_capsule_with_the_error_type(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1", name="submit_order")
    core.on_span_drop("span-1", ValueError("tool lookup exploded"))

    caps = _ledger(tmp_path)
    assert len(caps) == 2
    dropped = caps[1]
    assert dropped["disposition"]["verdict_class"] == "errored"
    assert dropped["effect"]["status"] == "failed"
    assert dropped["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert _compute(dropped)["llamaindex_span_dropped"] is True
    assert _compute(dropped)["llamaindex_error_type"] == "ValueError"
    assert verify(dropped).ok


def test_span_drop_with_no_error_object_still_seals(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1")
    core.on_span_drop("span-1", None)

    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert _compute(caps[1])["llamaindex_error_type"] == "unknown"


def test_span_drop_for_a_span_we_never_planned_is_a_no_op(tmp_path):
    core = _core(tmp_path)
    core.on_span_drop("never-seen", RuntimeError("x"))
    assert _ledger(tmp_path) == []


def test_return_direct_is_recorded(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1", name="order_receipt")
    _ok_result(core, "span-1", name="order_receipt", return_direct=True)

    caps = _ledger(tmp_path)
    assert _compute(caps[1])["llamaindex_return_direct"] is True


def test_tool_id_is_recorded_on_both_records(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1", tool_id="call-abc")
    _ok_result(core, "span-1", tool_id="call-abc")

    caps = _ledger(tmp_path)
    assert [_compute(c)["llamaindex_tool_id"] for c in caps] == ["call-abc", "call-abc"]


def test_runtime_and_observation_mode_are_stamped(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1")
    _ok_result(core, "span-1")

    for cap in _ledger(tmp_path):
        assert _compute(cap)["runtime"] == "llamaindex"
        assert _compute(cap)["observation_mode"] == "event_stream"


# ---------------------------------------------------------------------------
# Pairing is by span id
# ---------------------------------------------------------------------------


def test_interleaved_concurrent_calls_chain_to_the_right_planned_capsule(tmp_path):
    """The agent fans a model turn's tool calls out concurrently, so enters and
    exits for different tools interleave. Span-id pairing must survive that."""
    core = _core(tmp_path)
    _call(core, "span-A", name="get_price", tool_id="tc-A")
    _call(core, "span-B", name="get_stock", tool_id="tc-B")
    _ok_result(core, "span-B", name="get_stock", tool_id="tc-B")
    _ok_result(core, "span-A", name="get_price", tool_id="tc-A")

    caps = _ledger(tmp_path)
    assert len(caps) == 4
    planned_a, planned_b, confirmed_b, confirmed_a = caps
    assert planned_a["effect"]["type"] == "get_price"
    assert planned_b["effect"]["type"] == "get_stock"
    assert confirmed_b["chain"]["parent_capsule_id"] == planned_b["capsule_id"]
    assert confirmed_a["chain"]["parent_capsule_id"] == planned_a["capsule_id"]


def test_a_reused_tool_id_still_chains_correctly(tmp_path):
    """tool_id comes from the model and is not guaranteed unique. The span id is."""
    core = _core(tmp_path)
    _call(core, "span-1", tool_id="same")
    _call(core, "span-2", tool_id="same")
    _ok_result(core, "span-2", tool_id="same")
    _ok_result(core, "span-1", tool_id="same")

    caps = _ledger(tmp_path)
    planned_1, planned_2, confirmed_2, confirmed_1 = caps
    assert confirmed_2["chain"]["parent_capsule_id"] == planned_2["capsule_id"]
    assert confirmed_1["chain"]["parent_capsule_id"] == planned_1["capsule_id"]


def test_an_exit_with_no_matching_enter_seals_an_unchained_outcome(tmp_path):
    """A listener installed mid-run sees the exit but not the enter. Record the
    outcome rather than dropping it, and do not claim a chain we do not have."""
    core = _core(tmp_path)
    _ok_result(core, "span-orphan")

    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "confirmed"
    assert "chain" not in caps[0] or not caps[0].get("chain", {}).get("parent_capsule_id")


# ---------------------------------------------------------------------------
# Shape-based detection: the adjacent spans that must NOT double-count
# ---------------------------------------------------------------------------


def test_aggregate_tool_results_span_is_ignored(tmp_path):
    """That step enters with a ToolCallResult bound to the same parameter name.
    Without the ``tool_output`` carve it would seal a second planned capsule."""
    core = _core(tmp_path)
    result = FakeToolCallResult("get_price", {"sku": "S"}, "tc-1", FakeToolOutput("ok"))
    core.on_span_enter("span-agg", {"ev": result})
    assert _ledger(tmp_path) == []


def test_bare_tool_output_exit_is_ignored(tmp_path):
    """``FunctionTool.acall`` exits with a bare ToolOutput (no tool_id). Without
    the ``tool_id`` carve it would seal a duplicate outcome for every call."""
    core = _core(tmp_path)
    _call(core, "span-1")
    core.on_span_exit("span-inner", FakeToolOutput("4225 cents"))

    caps = _ledger(tmp_path)
    assert len(caps) == 1  # only the planned one
    assert caps[0]["effect"]["status"] == "planned"


def test_non_tool_spans_are_ignored(tmp_path):
    core = _core(tmp_path)
    core.on_span_enter("span-llm", {"messages": ["hi"], "tools": []})
    core.on_span_exit("span-llm", {"some": "dict"})
    core.on_span_enter("span-none", None)
    core.on_span_exit("span-none", None)
    assert _ledger(tmp_path) == []


def test_a_tool_call_bound_under_any_parameter_name_is_found(tmp_path):
    """Detection is by shape, so the parameter does not have to be called ``ev``."""
    core = _core(tmp_path)
    core.on_span_enter("span-1", {"ctx": object(), "something_else": FakeToolCall("t", {}, "i")})
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["type"] == "t"


# ---------------------------------------------------------------------------
# Never-raises
# ---------------------------------------------------------------------------


def test_a_seal_failure_warns_and_never_raises(tmp_path, monkeypatch):
    """A sealing failure must be visible, and must never reach the caller.

    The dispatcher wraps every span-handler call in ``except BaseException: pass``
    (dispatcher.py:203/230/262), so an unguarded listener cannot break the agent —
    it can only fail *silently*, which for an evidence layer is worse. The guard
    exists to turn that silence into a warning.
    """
    core = _core(tmp_path)

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(core, "emit_capsule", _explode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _call(core, "span-1")          # must not raise
        _ok_result(core, "span-1")     # must not raise
        core.on_span_drop("span-2", RuntimeError("x"))

    messages = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    assert messages, "a sealing failure must warn, not pass silently"
    assert all("failed to seal a capsule" in m for m in messages)
    assert _ledger(tmp_path) == []


def test_a_failed_planned_seal_does_not_fabricate_a_chain(tmp_path, monkeypatch):
    """If the planned capsule never sealed, the outcome must not claim a parent."""
    core = _core(tmp_path)
    calls = {"n": 0}
    real = core.emit_capsule

    def _fail_first(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ledger disk full")
        return real(**kw)

    monkeypatch.setattr(core, "emit_capsule", _fail_first)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _call(core, "span-1")
        _ok_result(core, "span-1")

    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "confirmed"
    assert not caps[0].get("chain", {}).get("parent_capsule_id")


# ---------------------------------------------------------------------------
# Privacy / payload projection
# ---------------------------------------------------------------------------


def test_raw_payloads_never_reach_the_ledger(tmp_path):
    core = _core(tmp_path)
    secret_in = "SSN-078-05-1120"
    secret_out = "account-balance-is-a-secret"
    _call(core, "span-1", kwargs={"ssn": secret_in})
    _ok_result(core, "span-1", content=secret_out)

    raw = (tmp_path / "ledger.jsonl").read_text()
    assert secret_in not in raw
    assert secret_out not in raw
    caps = _ledger(tmp_path)
    assert _compute(caps[0])["agent_input_digest"]
    assert _compute(caps[1])["agent_output_digest"]


def test_bytes_payloads_are_projected_not_dropped(tmp_path):
    core = _core(tmp_path)
    _call(core, "span-1", kwargs={"blob": b"\x00\x01\x02\x03"})
    caps = _ledger(tmp_path)
    assert len(caps) == 1  # digest succeeded rather than failing closed
    assert _compute(caps[0])["agent_input_digest"]


def test_unencodable_objects_become_type_markers(tmp_path):
    class Weird:
        pass

    core = _core(tmp_path)
    _call(core, "span-1", kwargs={"obj": Weird()})
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert _compute(caps[0])["agent_input_digest"]


def test_raw_input_is_not_duplicated_into_the_outcome_capsule(tmp_path):
    """raw_input is the input, already digested on the planned capsule. Digesting
    it again under agent_output_digest would put the same bytes under two names."""
    core = _core(tmp_path)
    _call(core, "span-1")
    out = FakeToolOutput("ok", raw_input={"sku": "SKU-9"}, raw_output="ok")
    core.on_span_exit("span-1", FakeToolCallResult("get_price", {}, "tc-1", out))

    caps = _ledger(tmp_path)
    # same content on both sides would otherwise collide; assert the digests differ
    assert _compute(caps[0])["agent_input_digest"] != _compute(caps[1])["agent_output_digest"]


def test_floats_chain_as_of_070(tmp_path):
    """As of 0.7.0 (#135) floats canonicalize to decimal strings before
    digesting, so a float kwarg seals a normal capsule chain rather than being
    refused. Pre-0.7.0 this warned and dropped the planned capsule."""
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _call(core, "span-1", kwargs={"price": 42.25})
    caps = _ledger(tmp_path)
    assert any(c["effect"]["status"] == "planned" for c in caps)
    assert not any("failed to seal" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# Model capture
# ---------------------------------------------------------------------------


def test_model_is_captured_from_an_llm_span_under_the_same_root(tmp_path):
    core = _core(tmp_path)
    core.observe_span_instance("root-1", None, None)
    core.observe_span_instance("step-1", "root-1", FakeLLM("gpt-test-1"))
    core.observe_span_instance("tool-1", "root-1", None)
    _call(core, "tool-1")

    caps = _ledger(tmp_path)
    assert caps[0]["model_attestation"]["model_id"] == "gpt-test-1"


def test_model_from_a_different_root_does_not_leak(tmp_path):
    core = _core(tmp_path)
    core.observe_span_instance("root-A", None, None)
    core.observe_span_instance("llm-A", "root-A", FakeLLM("model-A"))
    core.observe_span_instance("root-B", None, None)
    core.observe_span_instance("tool-B", "root-B", None)
    _call(core, "tool-B")

    caps = _ledger(tmp_path)
    assert caps[0].get("model_attestation", {}).get("model_id") != "model-A"


def test_capture_model_false_disables_capture(tmp_path):
    core = _core(tmp_path, capture_model=False)
    core.observe_span_instance("root-1", None, None)
    core.observe_span_instance("llm-1", "root-1", FakeLLM("gpt-test-1"))
    core.observe_span_instance("tool-1", "root-1", None)
    _call(core, "tool-1")

    caps = _ledger(tmp_path)
    assert caps[0].get("model_attestation", {}).get("model_id") != "gpt-test-1"


def test_explicit_model_is_used_when_nothing_was_observed(tmp_path):
    core = _core(tmp_path, model={"provider": "acme", "model_id": "acme-1"})
    _call(core, "span-1")
    caps = _ledger(tmp_path)
    assert caps[0]["model_attestation"]["model_id"] == "acme-1"


def test_an_instance_with_no_model_metadata_is_skipped_not_guessed(tmp_path):
    core = _core(tmp_path)
    core.observe_span_instance("root-1", None, None)
    core.observe_span_instance("x-1", "root-1", object())
    core.observe_span_instance("tool-1", "root-1", None)
    _call(core, "tool-1")

    caps = _ledger(tmp_path)
    assert "model_attestation" not in caps[0] or "model_id" not in caps[0].get(
        "model_attestation", {}
    )


# ---------------------------------------------------------------------------
# Observation only
# ---------------------------------------------------------------------------


def test_the_listener_never_mutates_bound_args_or_the_result(tmp_path):
    core = _core(tmp_path)
    kwargs = {"sku": "SKU-9"}
    call = FakeToolCall("get_price", kwargs, "tc-1")
    arguments = {"ev": call}
    core.on_span_enter("span-1", arguments)

    assert arguments == {"ev": call}
    assert call.tool_kwargs is kwargs and kwargs == {"sku": "SKU-9"}
    assert call.tool_name == "get_price" and call.tool_id == "tc-1"

    output = FakeToolOutput("4225 cents")
    result = FakeToolCallResult("get_price", kwargs, "tc-1", output)
    core.on_span_exit("span-1", result)

    assert result.tool_output is output
    assert output.content == "4225 cents" and output.is_error is False
    assert result.return_direct is False


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_pending_table_is_bounded(tmp_path):
    core = _core(tmp_path, max_pending=4)
    for i in range(20):
        _call(core, f"span-{i}", tool_id=f"tc-{i}")
    assert len(core._pending) <= 4


def test_parent_and_model_tables_are_bounded(tmp_path):
    core = _core(tmp_path, max_pending=4)
    for i in range(20):
        core.observe_span_instance(f"root-{i}", None, FakeLLM(f"m-{i}"))
    assert len(core._parent) <= 4
    assert len(core._model_by_root) <= 4


def test_the_root_walk_terminates_on_a_cycle(tmp_path):
    core = _core(tmp_path)
    core.observe_span_instance("a", "b", None)
    core.observe_span_instance("b", "a", None)
    _call(core, "a")  # must not hang
    assert len(_ledger(tmp_path)) == 1


# ---------------------------------------------------------------------------
# The listener wrapper
# ---------------------------------------------------------------------------


def test_listener_exposes_last_and_results(tmp_path):
    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    )
    _call(listener.core, "span-1")
    _ok_result(listener.core, "span-1")

    assert len(listener.results) == 2
    assert listener.last is listener.results[-1]
    assert listener.last.capsule_id == listener.core.last.capsule_id


def test_uninstall_before_install_is_a_no_op(tmp_path):
    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    )
    listener.uninstall()  # must not raise
