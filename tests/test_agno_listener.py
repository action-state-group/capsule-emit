# SPDX-License-Identifier: Apache-2.0
"""Agno tool-hook listener tests — framework-free core + optional shell.

Sealing logic lives in AgnoListenerCore, whose ``wrap_call`` takes a plain
callable as the continuation, so the full behavior is exercised WITHOUT agno
installed (mirrors the CrewAI/LangChain listener test approach). The hook shell
is covered by importorskip'd tests at the bottom that drive REAL agno
``FunctionCall.execute()`` / ``.aexecute()``.

Covered:
- planned capsule before the call (effect.status="planned"), verifies
- confirmed capsule after it, confirms-chained to the planned id, verifies
- error → verdict="errored", effect.status="failed", chained (errors are evidence)
- the continuation is called with the tool's arguments, once, and its value is
  the value the caller gets back
- never-raise guarantee: a broken ledger warns and does not fail the tool call,
  which in agno WOULD fail the tool (a raising hook is reported as tool failure)
- the tool's own exception still propagates unchanged
- digest-only privacy: raw argument/output values never reach the ledger
- replay marker for a repeated identical call; off via include_replay_marker
- max_seen bound holds (oldest evicted)
- float args fail closed but do not crash the agent run
- async path (wrap_call_async) for arun/aexecute
- shell: agno's duck-typed hook argument names bind as expected, sync + async,
  success + error, and a real agno cache hit re-runs the hook without re-running
  the tool
"""
from __future__ import annotations

import asyncio
import json
import warnings

import pytest

from capsule_emit.adapters.agno_listener import AgnoCapsuleListener, AgnoListenerCore
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _core(tmp_path, **kw) -> AgnoListenerCore:
    return AgnoListenerCore(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _listener(tmp_path, **kw) -> AgnoCapsuleListener:
    return AgnoCapsuleListener(
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


ARGS = {"po": "PO-1", "amount": "120.00"}


def _ok(**kwargs):
    return f"ok: {kwargs.get('po')}"


# ---------------------------------------------------------------------------
# The two-record chain
# ---------------------------------------------------------------------------


def test_wrap_call_seals_planned_then_confirmed(tmp_path):
    core = _core(tmp_path)
    core.wrap_call("write_po", _ok, ARGS)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "confirmed"
    assert "write_po" in caps[0]["action_id"]
    assert all(verify(c).ok for c in caps)


def test_confirmed_chains_to_planned(tmp_path):
    core = _core(tmp_path)
    core.wrap_call("write_po", _ok, ARGS)
    caps = _ledger(tmp_path)
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_planned_is_sealed_before_the_tool_runs(tmp_path):
    """The commitment record exists while the tool is still executing."""
    core = _core(tmp_path)
    during = {}

    def slow(**_kw):
        during["ledger_len"] = len(_ledger(tmp_path))
        return "done"

    core.wrap_call("write_po", slow, ARGS)
    assert during["ledger_len"] == 1  # planned already sealed, confirmed not yet


def test_confirmed_carries_response_digest(tmp_path):
    core = _core(tmp_path)
    core.wrap_call("write_po", _ok, ARGS)
    confirmed = _ledger(tmp_path)[1]
    assert confirmed["effect"]["response_digest"]


def test_runtime_is_agno(tmp_path):
    core = _core(tmp_path)
    core.wrap_call("write_po", _ok, ARGS)
    assert all(_compute(c)["runtime"] == "agno" for c in _ledger(tmp_path))


def test_action_type_is_fyi(tmp_path):
    core = _core(tmp_path)
    core.wrap_call("write_po", _ok, ARGS)
    assert all(c["action_type"] == "fyi" for c in _ledger(tmp_path))


# ---------------------------------------------------------------------------
# Continuation semantics
# ---------------------------------------------------------------------------


def test_continuation_receives_arguments_and_runs_once(tmp_path):
    core = _core(tmp_path)
    calls = []

    def spy(**kwargs):
        calls.append(kwargs)
        return "r"

    core.wrap_call("write_po", spy, ARGS)
    assert calls == [ARGS]


def test_wrap_call_returns_the_tool_result(tmp_path):
    core = _core(tmp_path)
    assert core.wrap_call("write_po", _ok, ARGS) == "ok: PO-1"


def test_empty_arguments_are_tolerated(tmp_path):
    core = _core(tmp_path)
    assert core.wrap_call("ping", lambda **_kw: "pong", None) == "pong"
    assert len(_ledger(tmp_path)) == 2


# ---------------------------------------------------------------------------
# Errors are evidence
# ---------------------------------------------------------------------------


def test_tool_error_seals_failed_chained(tmp_path):
    core = _core(tmp_path)

    def boom(**_kw):
        raise RuntimeError("order gateway down")

    with pytest.raises(RuntimeError):
        core.wrap_call("submit_order", boom, ARGS)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["disposition"]["verdict_class"] == "errored"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert verify(caps[1]).ok


def test_tool_exception_propagates_unchanged(tmp_path):
    """The listener seals the failure; it does not swallow or rewrap it."""
    core = _core(tmp_path)
    sentinel = RuntimeError("order gateway down")

    def boom(**_kw):
        raise sentinel

    with pytest.raises(RuntimeError) as excinfo:
        core.wrap_call("submit_order", boom, ARGS)
    assert excinfo.value is sentinel


# ---------------------------------------------------------------------------
# Never-raise guarantee
# ---------------------------------------------------------------------------


def test_seal_failure_warns_never_raises(tmp_path, monkeypatch):
    """A broken ledger must not fail the tool call.

    In agno this matters more than in LangChain: a hook that raises is caught
    by the same try as the entrypoint and reported as the TOOL's failure, so a
    listener exception would take a working tool down with it.
    """
    core = _core(tmp_path)

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(core, "emit_capsule", _explode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert core.wrap_call("write_po", _ok, ARGS) == "ok: PO-1"  # tool still ran
    assert any("failed to seal" in str(w.message) for w in caught)
    assert _ledger(tmp_path) == []


def test_seal_failure_on_error_path_still_propagates_tool_error(tmp_path, monkeypatch):
    core = _core(tmp_path)

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(core, "emit_capsule", _explode)

    def boom(**_kw):
        raise ValueError("gateway down")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        with pytest.raises(ValueError):
            core.wrap_call("submit_order", boom, ARGS)


def test_unsealable_planned_still_seals_an_unchained_outcome(tmp_path, monkeypatch):
    """A planned capsule that failed to seal must not fabricate a chain link."""
    core = _core(tmp_path)
    real = core.emit_capsule
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real(**kw)

    monkeypatch.setattr(core, "emit_capsule", flaky)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        core.wrap_call("write_po", _ok, ARGS)
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "confirmed"
    assert not caps[0].get("chain", {}).get("parent_capsule_id")


def test_float_args_fail_closed_but_do_not_crash(tmp_path):
    """A raw float in the arguments fails the PLANNED digest, not the tool.

    The outcome capsule still seals (its payload holds no float) and is left
    unchained rather than pointing at a capsule that does not exist.
    """
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert core.wrap_call("write_po", _ok, {"amount": 120.5}) == "ok: None"
    assert any("failed to seal" in str(w.message) for w in caught)
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "confirmed"
    assert not caps[0].get("chain", {}).get("parent_capsule_id")


def test_float_output_fails_closed_leaving_only_the_planned_record(tmp_path):
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert core.wrap_call("write_po", lambda **_kw: 12.5, {"po": "PO-1"}) == 12.5
    assert any("failed to seal" in str(w.message) for w in caught)
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "planned"


def test_unfingerprintable_arguments_do_not_crash(tmp_path):
    class Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    core = _core(tmp_path)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        assert core.wrap_call("write_po", lambda **_kw: "r", {"x": Hostile()}) == "r"


# ---------------------------------------------------------------------------
# Digest-only privacy
# ---------------------------------------------------------------------------


def test_raw_inputs_and_outputs_never_reach_the_ledger(tmp_path):
    core = _core(tmp_path)
    core.wrap_call(
        "write_po",
        lambda **_kw: "RESULT-SECRET-VALUE",
        {"po": "SECRET-PO-VALUE", "note": "confidential"},
    )
    raw = (tmp_path / "ledger.jsonl").read_text()
    assert "SECRET-PO-VALUE" not in raw
    assert "RESULT-SECRET-VALUE" not in raw
    assert "confidential" not in raw
    caps = _ledger(tmp_path)
    assert _compute(caps[0])["agent_input_digest"]
    assert _compute(caps[1])["agent_output_digest"]


# ---------------------------------------------------------------------------
# Replay marker (agno's tool cache re-runs hooks without re-running tools)
# ---------------------------------------------------------------------------


def test_repeat_call_is_marked_as_a_possible_replay(tmp_path):
    core = _core(tmp_path)
    core.wrap_call("get_price", _ok, {"po": "PO-1"})
    first_confirmed = _ledger(tmp_path)[1]["capsule_id"]
    core.wrap_call("get_price", _ok, {"po": "PO-1"})
    repeat = _ledger(tmp_path)[3]
    assert _compute(repeat)["agno_replay_of"] == first_confirmed
    assert "cached" in _compute(repeat)["agno_replay_note"]
    assert verify(repeat).ok


def test_distinct_arguments_are_not_marked_as_replay(tmp_path):
    core = _core(tmp_path)
    core.wrap_call("get_price", _ok, {"po": "PO-1"})
    core.wrap_call("get_price", _ok, {"po": "PO-2"})
    assert "agno_replay_of" not in _compute(_ledger(tmp_path)[3])


def test_same_arguments_to_a_different_tool_are_not_a_replay(tmp_path):
    core = _core(tmp_path)
    core.wrap_call("get_price", _ok, {"po": "PO-1"})
    core.wrap_call("write_po", _ok, {"po": "PO-1"})
    assert "agno_replay_of" not in _compute(_ledger(tmp_path)[3])


def test_replay_marker_can_be_disabled(tmp_path):
    core = _core(tmp_path, include_replay_marker=False)
    core.wrap_call("get_price", _ok, {"po": "PO-1"})
    core.wrap_call("get_price", _ok, {"po": "PO-1"})
    assert "agno_replay_of" not in _compute(_ledger(tmp_path)[3])


def test_failed_calls_are_not_remembered_as_replay_sources(tmp_path):
    core = _core(tmp_path)

    def boom(**_kw):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        core.wrap_call("submit_order", boom, {"po": "PO-1"})
    core.wrap_call("submit_order", _ok, {"po": "PO-1"})
    assert "agno_replay_of" not in _compute(_ledger(tmp_path)[3])


def test_max_seen_bound_evicts_oldest(tmp_path):
    core = _core(tmp_path, max_seen=2)
    for i in range(4):
        core.wrap_call("get_price", _ok, {"po": f"PO-{i}"})
    assert len(core._seen) == 2


# ---------------------------------------------------------------------------
# Async path
# ---------------------------------------------------------------------------


def test_async_wrap_call_seals_the_chain(tmp_path):
    core = _core(tmp_path)

    async def atool(**kwargs):
        return f"a:{kwargs.get('po')}"

    out = asyncio.run(core.wrap_call_async("write_po", atool, ARGS))
    assert out == "a:PO-1"
    caps = _ledger(tmp_path)
    assert [c["effect"]["status"] for c in caps] == ["planned", "confirmed"]
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_async_wrap_call_seals_failure(tmp_path):
    core = _core(tmp_path)

    async def aboom(**_kw):
        raise RuntimeError("async gateway down")

    with pytest.raises(RuntimeError):
        asyncio.run(core.wrap_call_async("submit_order", aboom, ARGS))
    caps = _ledger(tmp_path)
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["disposition"]["verdict_class"] == "errored"


# ---------------------------------------------------------------------------
# Shell wiring (no agno needed — the hook protocol is structural)
# ---------------------------------------------------------------------------


def test_hook_signature_declares_agnos_duck_typed_names():
    """Agno fills hook args by parameter NAME; renaming silently breaks wiring."""
    from inspect import signature

    params = set(signature(_listener_hook_signature_probe()).parameters)
    assert {"function_name", "function_call", "arguments", "agent"} <= params


def _listener_hook_signature_probe():
    return AgnoCapsuleListener(
        operator="o", developer="d", ledger="/dev/null", anchor=False
    ).hook


def test_hook_drives_the_core(tmp_path):
    listener = _listener(tmp_path)
    out = listener.hook(
        function_name="write_po", function_call=_ok, arguments=ARGS, agent=None
    )
    assert out == "ok: PO-1"
    assert [c["effect"]["status"] for c in _ledger(tmp_path)] == ["planned", "confirmed"]
    assert listener.last is not None
    assert len(listener.results) == 2


def test_hook_captures_model_from_agent(tmp_path):
    class _Model:
        id = "claude-x"
        provider = "anthropic"

    class _Agent:
        model = _Model()

    listener = _listener(tmp_path)
    listener.hook(
        function_name="write_po", function_call=_ok, arguments=ARGS, agent=_Agent()
    )
    att = _ledger(tmp_path)[0]["model_attestation"]
    assert json.dumps(att).find("claude-x") != -1


def test_hook_tolerates_agent_without_a_model(tmp_path):
    listener = _listener(tmp_path)
    listener.hook(
        function_name="write_po", function_call=_ok, arguments=ARGS, agent=object()
    )
    assert len(_ledger(tmp_path)) == 2


def test_async_hook_is_a_coroutine_function(tmp_path):
    from inspect import iscoroutinefunction

    assert iscoroutinefunction(_listener(tmp_path).async_hook)


def test_hook_identity_is_stable_across_accesses(tmp_path):
    """The object handed to Agent(tool_hooks=[...]) must stay the same object.

    Agno stores the callable in a list the caller may later compare against or
    remove from; a property minting a fresh closure per access would break that.
    """
    listener = _listener(tmp_path)
    assert listener.hook is listener.hook
    assert listener.async_hook is listener.async_hook


# ---------------------------------------------------------------------------
# Real agno (only when the extra is installed)
# ---------------------------------------------------------------------------


def _agno_function(entrypoint, hook, name=None):
    from agno.tools.function import Function

    fn = Function.from_callable(entrypoint)
    if name:
        fn.name = name
    fn.tool_hooks = [hook]
    return fn


def test_real_agno_tool_call_e2e(tmp_path):
    """Drive agno's own hook chain — success path, planned + confirmed."""
    pytest.importorskip("agno")
    from agno.tools.function import FunctionCall

    listener = _listener(tmp_path)

    def get_price(sku: str) -> str:
        """Return the price for a SKU."""
        return f"price for {sku}: 12.00 USD"

    fc = FunctionCall(
        function=_agno_function(get_price, listener.hook), arguments={"sku": "SKU-9"}
    )
    res = fc.execute()
    assert res.status == "success"
    assert "SKU-9" in res.result
    caps = _ledger(tmp_path)
    assert len(caps) == 2  # through the REAL agno hook chain
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "confirmed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert all(verify(c).ok for c in caps)


def test_real_agno_tool_error_e2e(tmp_path):
    """Agno reports the failure; the listener has already sealed it as evidence."""
    pytest.importorskip("agno")
    from agno.tools.function import FunctionCall

    listener = _listener(tmp_path)

    def submit_order(po: str) -> str:
        """Submit a purchase order."""
        raise RuntimeError("order gateway down")

    fc = FunctionCall(
        function=_agno_function(submit_order, listener.hook), arguments={"po": "PO-7"}
    )
    res = fc.execute()
    assert res.status == "failure"
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["disposition"]["verdict_class"] == "errored"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_real_agno_broken_listener_does_not_fail_the_tool(tmp_path, monkeypatch):
    """The never-raise guarantee, proven against agno's own error handling."""
    pytest.importorskip("agno")
    from agno.tools.function import FunctionCall

    listener = _listener(tmp_path)

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(listener.core, "emit_capsule", _explode)

    def get_price(sku: str) -> str:
        """Return the price for a SKU."""
        return f"price for {sku}: 12.00 USD"

    fc = FunctionCall(
        function=_agno_function(get_price, listener.hook), arguments={"sku": "SKU-9"}
    )
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        res = fc.execute()
    assert res.status == "success"  # a raising hook would have made this "failure"
    assert "SKU-9" in res.result


def test_real_agno_cache_hit_reruns_the_hook_without_rerunning_the_tool(tmp_path):
    """The documented reason the replay marker exists.

    On an agno cache hit the hook chain runs again but the entrypoint does not,
    so a second planned/confirmed pair is sealed for a tool that never ran. The
    listener marks the repeat rather than claiming a fresh execution.
    """
    pytest.importorskip("agno")
    from agno.tools.function import FunctionCall

    listener = _listener(tmp_path)
    runs = {"n": 0}

    def get_price(sku: str) -> str:
        """Return the price for a SKU."""
        runs["n"] += 1
        return f"price for {sku}: 12.00 USD"

    fn = _agno_function(get_price, listener.hook)
    fn.cache_results = True
    fn.cache_dir = str(tmp_path / "agno-cache")

    for _ in range(2):
        FunctionCall(function=fn, arguments={"sku": "SKU-9"}).execute()

    assert runs["n"] == 1  # agno served the second call from cache
    caps = _ledger(tmp_path)
    assert len(caps) == 4  # but the hook ran twice, so four capsules exist
    assert _compute(caps[3])["agno_replay_of"] == caps[1]["capsule_id"]


def test_real_agno_async_tool_call_e2e(tmp_path):
    """The async hook against agno's async chain."""
    pytest.importorskip("agno")
    from agno.tools.function import FunctionCall

    listener = _listener(tmp_path)

    async def get_price(sku: str) -> str:
        """Return the price for a SKU."""
        return f"price for {sku}: 12.00 USD"

    fc = FunctionCall(
        function=_agno_function(get_price, listener.async_hook),
        arguments={"sku": "SKU-9"},
    )
    res = asyncio.run(fc.aexecute())
    assert res.status == "success"
    caps = _ledger(tmp_path)
    assert [c["effect"]["status"] for c in caps] == ["planned", "confirmed"]
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_real_agno_agent_level_tool_hooks_accepts_the_hook(tmp_path):
    """Agent(tool_hooks=[...]) is the documented registration surface."""
    pytest.importorskip("agno")
    from agno.agent import Agent

    listener = _listener(tmp_path)
    agent = Agent(tool_hooks=[listener.hook])
    assert agent.tool_hooks == [listener.hook]
