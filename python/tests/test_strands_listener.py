# SPDX-License-Identifier: Apache-2.0
"""Strands Agents hook-listener tests — framework-free core + real-SDK shell.

Sealing logic lives in StrandsListenerCore, whose handlers take duck-typed event
objects, so the full behavior is exercised WITHOUT strands installed (mirrors the
CrewAI/LangChain/agno listener test approach). The HookProvider shell and the
end-to-end agent loop are covered by importorskip'd tests at the bottom that drive
a REAL ``strands.Agent`` against a scripted no-network model.

Covered:
- planned capsule on BeforeToolCallEvent (effect.status="planned"), verifies
- confirmed capsule on AfterToolCallEvent, confirms-chained to the planned id
- error ToolResult and tool exception → verdict="errored", status="failed", chained
- hook cancellation (cancel_tool by some other layer) → verdict="blocked", chained
- pairing is by toolUseId, so concurrent tool calls never cross-chain
- the executor's retry loop → strands_attempt / strands_retry_of markers
- never-raise guarantee: a broken ledger warns and does not fail the agent turn,
  which in strands WOULD fail it (before-hook exceptions escape the executor's try)
- digest-only privacy: raw argument/output values never reach the ledger
- binary content blocks are projected, not fed to the digest
- float args fail closed (documents CURRENT main behavior; see #135)
- max_pending bound holds (oldest evicted)
- shell: HookProvider is satisfied structurally, registers on both event types,
  Agent(hooks=[...]) accepts it, and both the sync and async dispatchers drive it
- real SDK: full agent loop e2e, concurrent tools, error, cancel, retry, and a
  broken listener that does not take the agent down with it
"""
from __future__ import annotations

import asyncio
import json
import warnings

import pytest

from capsule_emit.adapters.strands_listener import (
    StrandsCapsuleListener,
    StrandsListenerCore,
)
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Helpers — duck-typed stand-ins for strands' hook event dataclasses
# ---------------------------------------------------------------------------

ARGS = {"po": "PO-1", "amount": "120.00"}


class _Event:
    """Minimal stand-in for Before/AfterToolCallEvent (attribute access only)."""

    def __init__(self, **kw):
        self.agent = kw.pop("agent", None)
        self.tool_use = kw.pop("tool_use", None)
        self.result = kw.pop("result", None)
        self.exception = kw.pop("exception", None)
        self.cancel_message = kw.pop("cancel_message", None)
        for k, v in kw.items():
            setattr(self, k, v)


def _tool_use(name="write_po", tool_use_id="tu-1", tool_input=None):
    return {"name": name, "toolUseId": tool_use_id, "input": ARGS if tool_input is None else tool_input}


def _ok_result(tool_use_id="tu-1", text="ok: PO-1"):
    return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]}


def _err_result(tool_use_id="tu-1", text="Error: gateway down"):
    return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": text}]}


def _core(tmp_path, **kw) -> StrandsListenerCore:
    return StrandsListenerCore(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        **kw,
    )


def _listener(tmp_path, **kw) -> StrandsCapsuleListener:
    return StrandsCapsuleListener(
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


def _run(core, *, tool_use=None, result=None, exception=None, cancel_message=None, agent=None):
    """Drive one before/after pair through the core."""
    tu = tool_use or _tool_use()
    core.on_before_tool_call(_Event(tool_use=tu, agent=agent))
    core.on_after_tool_call(
        _Event(
            tool_use=tu,
            agent=agent,
            result=result if result is not None else _ok_result(tu["toolUseId"]),
            exception=exception,
            cancel_message=cancel_message,
        )
    )


# ---------------------------------------------------------------------------
# The two-record chain
# ---------------------------------------------------------------------------


def test_before_then_after_seals_planned_then_confirmed(tmp_path):
    core = _core(tmp_path)
    _run(core)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "confirmed"
    assert "write_po" in caps[0]["action_id"]
    assert all(verify(c).ok for c in caps)


def test_confirmed_chains_to_planned(tmp_path):
    core = _core(tmp_path)
    _run(core)
    caps = _ledger(tmp_path)
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert caps[1]["chain"]["relation"] == "confirms"


def test_planned_is_sealed_before_the_outcome_is_known(tmp_path):
    """The commitment record exists as soon as the before-event fires."""
    core = _core(tmp_path)
    core.on_before_tool_call(_Event(tool_use=_tool_use()))
    assert [c["effect"]["status"] for c in _ledger(tmp_path)] == ["planned"]


def test_confirmed_carries_response_digest(tmp_path):
    core = _core(tmp_path)
    _run(core)
    assert _ledger(tmp_path)[1]["effect"]["response_digest"]


def test_runtime_is_strands(tmp_path):
    core = _core(tmp_path)
    _run(core)
    assert all(_compute(c)["runtime"] == "strands" for c in _ledger(tmp_path))


def test_action_type_is_fyi(tmp_path):
    core = _core(tmp_path)
    _run(core)
    assert all(c["action_type"] == "fyi" for c in _ledger(tmp_path))


# ---------------------------------------------------------------------------
# Errors and cancellations are evidence
# ---------------------------------------------------------------------------


def test_error_status_result_seals_failed_chained(tmp_path):
    core = _core(tmp_path)
    _run(core, result=_err_result())
    caps = _ledger(tmp_path)
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["disposition"]["verdict_class"] == "errored"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert verify(caps[1]).ok


def test_exception_on_the_after_event_seals_failed_chained(tmp_path):
    """The executor catches the tool's exception and hands it to the after-event."""
    core = _core(tmp_path)
    _run(core, result=_err_result(), exception=RuntimeError("order gateway down"))
    caps = _ledger(tmp_path)
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["disposition"]["verdict_class"] == "errored"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_cancelled_tool_seals_a_blocked_capsule_chained(tmp_path):
    """A refusal that actually took effect is recorded, not dropped.

    The tool never ran, so the effect record stays "planned" (the spec's planned
    carve) and the refusal lives in the verdict — the same shape capsule_emit.gate
    uses for a blocked call. Using an ad-hoc "cancelled" effect status would derive
    effect_mode "dispatched_unconfirmed" and claim a dispatch that never happened.

    This listener never cancels anything itself; it records the cancellation some
    other layer performed via BeforeToolCallEvent.cancel_tool, and says so.
    """
    core = _core(tmp_path)
    _run(core, result=_err_result(text="denied by policy"), cancel_message="denied by policy")
    caps = _ledger(tmp_path)
    assert caps[1]["effect"]["status"] == "planned"
    assert caps[1]["disposition"]["verdict_class"] == "blocked"
    assert _compute(caps[1])["strands_cancelled_by_hook"] is True
    assert _compute(caps[1])["observation_mode"] == "event_stream"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert verify(caps[1]).ok


def test_cancel_message_wins_over_the_error_result_it_produces(tmp_path):
    """The executor builds an error ToolResult for a cancel; it is not a failure."""
    core = _core(tmp_path)
    _run(core, result=_err_result(text="tool cancelled by user"), cancel_message="tool cancelled by user")
    caps = _ledger(tmp_path)
    assert caps[1]["disposition"]["verdict_class"] == "blocked"
    assert caps[1]["effect"]["status"] != "failed"


# ---------------------------------------------------------------------------
# Pairing by toolUseId (the concurrent executor is the default)
# ---------------------------------------------------------------------------


def test_interleaved_concurrent_tool_calls_chain_to_their_own_planned(tmp_path):
    """ConcurrentToolExecutor interleaves events; toolUseId keying must be exact."""
    core = _core(tmp_path)
    a = _tool_use("get_price", "tu-A", {"sku": "A"})
    b = _tool_use("get_stock", "tu-B", {"sku": "B"})
    core.on_before_tool_call(_Event(tool_use=a))
    core.on_before_tool_call(_Event(tool_use=b))
    # B finishes first — the reverse of the start order
    core.on_after_tool_call(_Event(tool_use=b, result=_ok_result("tu-B", "stock: 4")))
    core.on_after_tool_call(_Event(tool_use=a, result=_ok_result("tu-A", "price: 12.00")))
    caps = _ledger(tmp_path)
    planned_a, planned_b, done_b, done_a = caps
    assert done_b["chain"]["parent_capsule_id"] == planned_b["capsule_id"]
    assert done_a["chain"]["parent_capsule_id"] == planned_a["capsule_id"]
    assert all(verify(c).ok for c in caps)


def test_same_tool_name_different_ids_do_not_cross_chain(tmp_path):
    core = _core(tmp_path)
    a = _tool_use("get_price", "tu-A", {"sku": "A"})
    b = _tool_use("get_price", "tu-B", {"sku": "B"})
    core.on_before_tool_call(_Event(tool_use=a))
    core.on_before_tool_call(_Event(tool_use=b))
    core.on_after_tool_call(_Event(tool_use=b, result=_ok_result("tu-B")))
    caps = _ledger(tmp_path)
    assert caps[2]["chain"]["parent_capsule_id"] == caps[1]["capsule_id"]


def test_after_without_a_matching_before_seals_unchained(tmp_path):
    core = _core(tmp_path)
    core.on_after_tool_call(_Event(tool_use=_tool_use(), result=_ok_result()))
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert not caps[0].get("chain", {}).get("parent_capsule_id")


def test_missing_tool_use_is_tolerated(tmp_path):
    """A malformed event must not raise; it seals what it honestly can."""
    core = _core(tmp_path)
    core.on_before_tool_call(_Event(tool_use=None))
    core.on_after_tool_call(_Event(tool_use=None, result=_ok_result(None)))
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert all(c["action_id"].startswith("tool/") for c in caps)
    # No toolUseId to pair on, so the outcome is honestly left unchained.
    assert not caps[1].get("chain", {}).get("parent_capsule_id")


def test_unchained_outcome_with_no_result_fails_closed(tmp_path):
    """A "confirmed" effect REQUIRES a response digest (§5.2).

    core.py:669-673 derives it from the output, or — failing that — from the
    confirms id. An event with neither a toolUseId to chain on nor a result to
    digest can supply neither, so the seal fails closed and warns rather than
    emitting an unbacked confirmation. (With a chain present the digest is
    derived from the parent id, which is why the paired case above seals.)
    """
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_before_tool_call(_Event(tool_use=None))
        core.on_after_tool_call(_Event(tool_use=None, result=None))
    assert any("failed to seal" in str(w.message) for w in caught)
    assert [c["effect"]["status"] for c in _ledger(tmp_path)] == ["planned"]


# ---------------------------------------------------------------------------
# The retry loop (strands' nearest thing to replay)
# ---------------------------------------------------------------------------


def test_retried_call_is_marked_not_claimed_as_fresh(tmp_path):
    """A second before-event for the same toolUseId is the executor re-running it."""
    core = _core(tmp_path)
    tu = _tool_use()
    core.on_before_tool_call(_Event(tool_use=tu))
    core.on_after_tool_call(_Event(tool_use=tu, result=_err_result()))
    core.on_before_tool_call(_Event(tool_use=tu))  # retry=True was set by some hook
    core.on_after_tool_call(_Event(tool_use=tu, result=_ok_result()))
    caps = _ledger(tmp_path)
    assert len(caps) == 4
    assert "strands_attempt" not in _compute(caps[0])
    assert _compute(caps[2])["strands_attempt"] == 2
    assert _compute(caps[2])["strands_retry_of"] == caps[0]["capsule_id"]
    assert _compute(caps[3])["strands_attempt"] == 2
    assert caps[3]["chain"]["parent_capsule_id"] == caps[2]["capsule_id"]
    assert all(verify(c).ok for c in caps)


def test_attempt_marker_can_be_turned_off(tmp_path):
    core = _core(tmp_path, include_attempt_marker=False)
    tu = _tool_use()
    for _ in range(2):
        core.on_before_tool_call(_Event(tool_use=tu))
        core.on_after_tool_call(_Event(tool_use=tu, result=_ok_result()))
    assert "strands_attempt" not in _compute(_ledger(tmp_path)[2])


def test_max_pending_bound_evicts_oldest(tmp_path):
    core = _core(tmp_path, max_pending=2)
    for i in range(5):
        core.on_before_tool_call(_Event(tool_use=_tool_use("get_price", f"tu-{i}", {"sku": str(i)})))
    assert len(core._pending) <= 2
    assert len(core._attempts) <= 2


# ---------------------------------------------------------------------------
# Never-raise guarantee
# ---------------------------------------------------------------------------


def test_seal_failure_warns_never_raises_on_before(tmp_path, monkeypatch):
    """A broken ledger must not fail the agent turn.

    In strands this matters more than in LangChain: the before-hook is invoked
    OUTSIDE the executor's try (_executor.py:177 vs try: at :209), so a raising
    before-callback aborts the whole tool stream.
    """
    core = _core(tmp_path)

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(core, "emit_capsule", _explode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_before_tool_call(_Event(tool_use=_tool_use()))
    assert any("failed to seal" in str(w.message) for w in caught)
    assert _ledger(tmp_path) == []


def test_seal_failure_warns_never_raises_on_after(tmp_path, monkeypatch):
    """The after-hook is invoked INSIDE the executor's try, whose handler calls
    the after-hook AGAIN (_executor.py:327) — a raising after-callback would fire
    twice and escape uncaught the second time."""
    core = _core(tmp_path)
    core.on_before_tool_call(_Event(tool_use=_tool_use()))

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(core, "emit_capsule", _explode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core.on_after_tool_call(_Event(tool_use=_tool_use(), result=_ok_result()))
    assert any("failed to seal" in str(w.message) for w in caught)


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
        _run(core)
    caps = _ledger(tmp_path)
    assert len(caps) == 1
    assert caps[0]["effect"]["status"] == "confirmed"
    assert not caps[0].get("chain", {}).get("parent_capsule_id")


def test_hostile_event_object_does_not_raise(tmp_path):
    class Hostile:
        @property
        def tool_use(self):
            raise RuntimeError("no tool_use for you")

    core = _core(tmp_path)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError):
            # getattr on a raising property propagates; documented limit, not a
            # silent swallow — strands never hands us an event like this.
            core.on_before_tool_call(Hostile())


# ---------------------------------------------------------------------------
# Privacy and payload projection
# ---------------------------------------------------------------------------


def test_raw_values_never_reach_the_ledger(tmp_path):
    core = _core(tmp_path)
    secret_in, secret_out = "PO-SECRET-42", "balance 991 EUR"
    _run(
        core,
        tool_use=_tool_use(tool_input={"po": secret_in}),
        result=_ok_result(text=secret_out),
    )
    body = (tmp_path / "ledger.jsonl").read_text()
    assert secret_in not in body
    assert secret_out not in body
    assert "agent_input_digest" in body


def test_binary_content_blocks_are_projected_not_digested(tmp_path):
    """An image-returning tool must still produce an outcome capsule."""
    core = _core(tmp_path)
    result = {
        "toolUseId": "tu-1",
        "status": "success",
        "content": [{"text": "chart"}, {"image": {"format": "png", "source": {"bytes": b"\x89PNG" * 8}}}],
    }
    # bytes nested under image.source.bytes — the whole block is replaced
    result["content"][1] = {"image": b"\x89PNG" * 8}
    _run(core, result=result)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["effect"]["status"] == "confirmed"
    assert verify(caps[1]).ok


def test_non_dict_result_is_passed_through(tmp_path):
    core = _core(tmp_path)
    _run(core, result="just a string")
    assert [c["effect"]["status"] for c in _ledger(tmp_path)] == ["planned", "confirmed"]


# ---------------------------------------------------------------------------
# Floats — CURRENT main behavior, documented honestly
# ---------------------------------------------------------------------------


def test_float_args_chain_post_135(tmp_path):
    """A raw float in the tool input seals and chains (#128 fixed by #135).

    The pre-#135 version of this test pinned the fail-closed behavior (planned
    capsule dropped, outcome unchained). #135 merged 2026-08-31: the adapter
    commitment path canonicalizes floats per RFC 8785 before digesting, so the
    same call now produces the full planned->confirmed pair with no warning.
    """
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _run(core, tool_use=_tool_use(tool_input={"amount": 120.5}))
    assert not any("failed to seal" in str(w.message) for w in caught)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "confirmed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_float_output_chains_post_135(tmp_path):
    core = _core(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _run(core, result={"toolUseId": "tu-1", "status": "success", "content": [{"json": {"px": 12.5}}]})
    assert not any("failed to seal" in str(w.message) for w in caught)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["effect"]["status"] == "confirmed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


# ---------------------------------------------------------------------------
# Model capture
# ---------------------------------------------------------------------------


def test_model_is_captured_from_the_agent(tmp_path):
    class _Model:
        def get_config(self):
            return {"model_id": "global.anthropic.claude-sonnet-4-6"}

    class _Agent:
        model = _Model()

    core = _core(tmp_path)
    _run(core, agent=_Agent())
    assert "claude-sonnet-4-6" in json.dumps(_ledger(tmp_path)[0]["model_attestation"])


def test_agent_without_a_model_is_tolerated(tmp_path):
    core = _core(tmp_path)
    _run(core, agent=object())
    assert len(_ledger(tmp_path)) == 2


def test_model_with_a_broken_get_config_is_tolerated(tmp_path):
    class _Model:
        def get_config(self):
            raise RuntimeError("nope")

    class _Agent:
        model = _Model()

    core = _core(tmp_path)
    _run(core, agent=_Agent())
    assert len(_ledger(tmp_path)) == 2


# ---------------------------------------------------------------------------
# Shell wiring
# ---------------------------------------------------------------------------


def test_listener_satisfies_the_hook_provider_protocol_structurally(tmp_path):
    pytest.importorskip("strands")
    from strands.hooks import HookProvider

    assert isinstance(_listener(tmp_path), HookProvider)


def test_register_hooks_registers_both_tool_events(tmp_path):
    pytest.importorskip("strands")
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookRegistry

    registry = HookRegistry()
    registry.add_hook(_listener(tmp_path))
    registered = registry._registered_callbacks
    assert BeforeToolCallEvent in registered
    assert AfterToolCallEvent in registered


def test_callbacks_are_synchronous_so_both_dispatchers_work(tmp_path):
    """invoke_callbacks (sync) raises if ANY callback is async (registry.py:377)."""
    pytest.importorskip("strands")
    from inspect import iscoroutinefunction

    listener = _listener(tmp_path)
    assert not iscoroutinefunction(listener.core.on_before_tool_call)
    assert not iscoroutinefunction(listener.core.on_after_tool_call)


def test_registry_async_dispatch_drives_the_core(tmp_path):
    """The tool path always uses invoke_callbacks_async (_executor.py:66 / :96).

    Real event dataclasses through the real dispatcher — the sync callbacks this
    provider registers are awaited-or-called correctly by it.
    """
    pytest.importorskip("strands")
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookRegistry

    registry = HookRegistry()
    registry.add_hook(_listener(tmp_path))
    tu = _tool_use()

    async def drive():
        await registry.invoke_callbacks_async(
            BeforeToolCallEvent(agent=None, selected_tool=None, tool_use=tu, invocation_state={})
        )
        await registry.invoke_callbacks_async(
            AfterToolCallEvent(
                agent=None,
                selected_tool=None,
                tool_use=tu,
                invocation_state={},
                result=_ok_result(tu["toolUseId"]),
            )
        )

    asyncio.run(drive())
    caps = _ledger(tmp_path)
    assert [c["effect"]["status"] for c in caps] == ["planned", "confirmed"]
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_registry_sync_dispatch_also_drives_the_core(tmp_path):
    """invoke_callbacks (sync) raises RuntimeError if ANY callback is async
    (registry.py:377). Registering sync callbacks keeps this surface open."""
    pytest.importorskip("strands")
    from strands.hooks import BeforeToolCallEvent, HookRegistry

    registry = HookRegistry()
    registry.add_hook(_listener(tmp_path))
    registry.invoke_callbacks(
        BeforeToolCallEvent(agent=None, selected_tool=None, tool_use=_tool_use(), invocation_state={})
    )
    assert [c["effect"]["status"] for c in _ledger(tmp_path)] == ["planned"]


# ---------------------------------------------------------------------------
# Real strands SDK — hermetic, scripted model, no network, no API key
# ---------------------------------------------------------------------------


def _fake_model_cls():
    from strands.models import Model

    class ScriptedModel(Model):
        """A Model that replays a fixed list of assistant messages.

        Modelled on the SDK's own test fixture
        (strands-py/tests/fixtures/mocked_model_provider.py in their repo) but
        written here so the test needs only the released wheel.
        """

        def __init__(self, script):
            self.script = list(script)
            self.index = 0

        def get_config(self):
            return {"model_id": "scripted-test-model"}

        def update_config(self, **_kw):
            pass

        async def structured_output(self, *_a, **_kw):  # pragma: no cover - unused
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, tool_choice=None, **_kw):
            message = self.script[self.index]
            self.index += 1
            yield {"messageStart": {"role": "assistant"}}
            stop_reason = "end_turn"
            for content in message["content"]:
                if "text" in content:
                    yield {"contentBlockStart": {"start": {}}}
                    yield {"contentBlockDelta": {"delta": {"text": content["text"]}}}
                    yield {"contentBlockStop": {}}
                if "toolUse" in content:
                    stop_reason = "tool_use"
                    use = content["toolUse"]
                    yield {
                        "contentBlockStart": {
                            "start": {"toolUse": {"name": use["name"], "toolUseId": use["toolUseId"]}}
                        }
                    }
                    yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(use["input"])}}}}
                    yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": stop_reason}}

    return ScriptedModel


def _turn(*, tool_calls=(), text=None):
    content = [{"toolUse": {"name": n, "toolUseId": i, "input": a}} for n, i, a in tool_calls]
    if text is not None:
        content.append({"text": text})
    return {"role": "assistant", "content": content}


@pytest.fixture
def strands_agent(tmp_path, monkeypatch):
    """Factory for a real strands Agent wired to the listener, fully offline."""
    pytest.importorskip("strands")
    monkeypatch.setenv("CAPSULE_WITNESS", "off")

    def build(script, tools, extra_hooks=()):
        from strands import Agent

        listener = _listener(tmp_path)
        model = _fake_model_cls()(script)
        agent = Agent(model=model, tools=list(tools), hooks=[listener, *extra_hooks])
        return agent, listener

    return build


def _price_tool():
    from strands import tool

    @tool
    def get_price(sku: str) -> str:
        """Return the price for a SKU."""
        return f"price for {sku}: 12.00 USD"

    return get_price


def _stock_tool():
    from strands import tool

    @tool
    def get_stock(sku: str) -> str:
        """Return the stock level for a SKU."""
        return f"stock for {sku}: 4 units"

    return get_stock


def _boom_tool():
    from strands import tool

    @tool
    def submit_order(po: str) -> str:
        """Submit a purchase order."""
        raise RuntimeError("order gateway down")

    return submit_order


def test_real_agent_tool_call_e2e(tmp_path, strands_agent):
    """Full agent loop through the REAL executor and hook registry, no LLM."""
    agent, _ = strands_agent(
        [_turn(tool_calls=[("get_price", "tu-1", {"sku": "SKU-9"})]), _turn(text="done")],
        [_price_tool()],
    )
    result = agent("what is the price of SKU-9?")
    assert "done" in str(result)
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[0]["effect"]["status"] == "planned"
    assert caps[1]["effect"]["status"] == "confirmed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert all(_compute(c)["runtime"] == "strands" for c in caps)
    assert "scripted-test-model" in json.dumps(caps[0]["model_attestation"])
    assert all(verify(c).ok for c in caps)


def test_real_agent_concurrent_tools_chain_correctly(tmp_path, strands_agent):
    """Two tool calls in one turn run as concurrent asyncio tasks."""
    agent, _ = strands_agent(
        [
            _turn(
                tool_calls=[
                    ("get_price", "tu-A", {"sku": "SKU-A"}),
                    ("get_stock", "tu-B", {"sku": "SKU-B"}),
                ]
            ),
            _turn(text="done"),
        ],
        [_price_tool(), _stock_tool()],
    )
    agent("price and stock please")
    caps = _ledger(tmp_path)
    assert len(caps) == 4
    by_id = {c["capsule_id"]: c for c in caps}
    outcomes = [c for c in caps if c["effect"]["status"] == "confirmed"]
    assert len(outcomes) == 2
    for outcome in outcomes:
        parent = by_id[outcome["chain"]["parent_capsule_id"]]
        assert parent["effect"]["status"] == "planned"
        assert parent["action_id"].split("/")[0] == outcome["action_id"].split("/")[0]
    assert all(verify(c).ok for c in caps)


def test_real_agent_tool_error_e2e(tmp_path, strands_agent):
    agent, _ = strands_agent(
        [_turn(tool_calls=[("submit_order", "tu-1", {"po": "PO-7"})]), _turn(text="done")],
        [_boom_tool()],
    )
    agent("submit PO-7")
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["disposition"]["verdict_class"] == "errored"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_real_agent_cancelled_tool_e2e(tmp_path, strands_agent):
    """Some OTHER layer cancels in path; the listener records that it happened."""
    pytest.importorskip("strands")
    from strands.hooks import BeforeToolCallEvent

    class Denier:
        def register_hooks(self, registry, **_kw):
            registry.add_callback(BeforeToolCallEvent, self.deny)

        def deny(self, event):
            event.cancel_tool = "denied by policy"

    agent, _ = strands_agent(
        [_turn(tool_calls=[("submit_order", "tu-1", {"po": "PO-7"})]), _turn(text="done")],
        [_boom_tool()],
        extra_hooks=[Denier()],
    )
    agent("submit PO-7")
    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["disposition"]["verdict_class"] == "blocked"
    assert caps[1]["effect"]["status"] == "planned"
    assert _compute(caps[1])["strands_cancelled_by_hook"] is True
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    assert verify(caps[1]).ok


def test_real_agent_retry_loop_is_marked(tmp_path, strands_agent):
    """A hook sets AfterToolCallEvent.retry once; the executor re-runs the call."""
    pytest.importorskip("strands")
    from strands.hooks import AfterToolCallEvent

    class RetryOnce:
        def __init__(self):
            self.done = False

        def register_hooks(self, registry, **_kw):
            registry.add_callback(AfterToolCallEvent, self.maybe_retry)

        def maybe_retry(self, event):
            if not self.done:
                self.done = True
                event.retry = True

    agent, _ = strands_agent(
        [_turn(tool_calls=[("get_price", "tu-1", {"sku": "SKU-9"})]), _turn(text="done")],
        [_price_tool()],
        extra_hooks=[RetryOnce()],
    )
    agent("price of SKU-9")
    caps = _ledger(tmp_path)
    assert len(caps) == 4  # two attempts, two records each
    assert "strands_attempt" not in _compute(caps[0])
    assert _compute(caps[2])["strands_attempt"] == 2
    assert _compute(caps[2])["strands_retry_of"] == caps[0]["capsule_id"]
    assert all(verify(c).ok for c in caps)


def test_real_agent_survives_a_broken_listener(tmp_path, strands_agent, monkeypatch):
    """The never-raise guarantee, proven against the real executor.

    A raising before-callback escapes the executor's try entirely, so without
    the guard this agent turn would die rather than return.
    """
    agent, listener = strands_agent(
        [_turn(tool_calls=[("get_price", "tu-1", {"sku": "SKU-9"})]), _turn(text="done")],
        [_price_tool()],
    )

    def _explode(**_kw):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(listener.core, "emit_capsule", _explode)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = agent("price of SKU-9")
    assert "done" in str(result)
    assert any("failed to seal" in str(w.message) for w in caught)
    assert _ledger(tmp_path) == []


def test_real_agent_accepts_the_listener_via_the_public_kwarg(tmp_path, strands_agent):
    """Agent(hooks=[...]) — the registration surface, no fork, no monkeypatch."""
    agent, listener = strands_agent([_turn(text="hi")], [])
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

    assert BeforeToolCallEvent in agent.hooks._registered_callbacks
    assert AfterToolCallEvent in agent.hooks._registered_callbacks
    assert listener.core.last is None
