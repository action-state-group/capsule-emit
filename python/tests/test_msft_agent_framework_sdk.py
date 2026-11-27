# SPDX-License-Identifier: Apache-2.0
"""Microsoft Agent Framework middleware tests against the real SDK: the two
shells, the end-to-end agent loop and the shipped demo. The framework-free
core is tested in ``test_msft_agent_framework.py``.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from test_msft_agent_framework import (  # helpers shared with the framework-free tests
    _core,
    _ledger,
)

from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Shells — real SDK from here down
# ---------------------------------------------------------------------------

af = pytest.importorskip("agent_framework", reason="needs capsule-emit[msft-agent-framework]")

from capsule_emit.adapters.msft_agent_framework import (  # noqa: E402
    CapsuleFunctionMiddleware,
    CapsuleRunMiddleware,
    capsule_middleware,
)

PRICES = {"SKU-1": "12.00 USD"}


def get_price(sku: str) -> str:
    """Look up the list price for a SKU."""
    return PRICES.get(sku, "unknown")


def submit_order(po: str) -> str:
    """Submit a purchase order (wired to fail)."""
    raise RuntimeError(f"order system rejected {po}")


def _scripted_client(script, model="scripted-demo-model", provider="scripted"):
    """A keyless chat client with the framework's real function-calling loop."""
    from agent_framework import (
        BaseChatClient,
        ChatMiddlewareLayer,
        ChatResponse,
        Content,
        FunctionInvocationLayer,
        Message,
    )

    class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
        OTEL_PROVIDER_NAME = provider

        def __init__(self, turns, **kw):
            super().__init__(**kw)
            self.model = model
            self.turns = list(turns)
            self.index = 0

        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            async def _turn():
                turn = self.turns[min(self.index, len(self.turns) - 1)]
                self.index += 1
                if isinstance(turn, str):
                    contents = [Content.from_text(turn)]
                else:
                    contents = [
                        Content.from_function_call(call_id=cid, name=name, arguments=args)
                        for name, args, cid in turn
                    ]
                return ChatResponse(
                    messages=[Message(role="assistant", contents=contents)],
                    response_id=f"scripted-{self.index}",
                )

            return _turn()

    return ScriptedChatClient(script)


def _agent(tmp_path, script, tools, extra=(), ledger_name="ledger.jsonl", **core_kw):
    from agent_framework import Agent

    mw = capsule_middleware(
        operator="acme-co",
        developer="my-agent@v1",
        ledger=tmp_path / ledger_name,
        anchor=False,
        **core_kw,
    )
    return Agent(
        _scripted_client(script),
        "you are a procurement assistant",
        name="procurement",
        tools=list(tools),
        middleware=[*mw, *extra],
    ), mw


def test_the_two_shells_land_on_their_own_seams(tmp_path):
    from agent_framework import AgentMiddleware, FunctionMiddleware
    from agent_framework._middleware import categorize_middleware

    mw = capsule_middleware(operator="o", developer="d", ledger=tmp_path / "l.jsonl", anchor=False)
    run_mw, fn_mw = mw
    assert isinstance(run_mw, (CapsuleRunMiddleware, AgentMiddleware))
    assert isinstance(fn_mw, (CapsuleFunctionMiddleware, FunctionMiddleware))
    # The framework's own router, not our assertion about it.
    categorized = categorize_middleware(mw)
    assert categorized["agent"] == [run_mw]
    assert categorized["function"] == [fn_mw]
    assert categorized["chat"] == []
    # Both halves share one core, so one ledger and one results history.
    assert run_mw.core is fn_mw.core


def test_shells_reject_core_and_kwargs_together(tmp_path):
    core = _core(tmp_path)
    with pytest.raises(TypeError, match="not both"):
        CapsuleFunctionMiddleware(core, operator="o")
    with pytest.raises(TypeError, match="not both"):
        CapsuleRunMiddleware(core, operator="o")


def test_end_to_end_clean_run_seals_a_run_pair_and_a_tool_pair(tmp_path):
    agent, mw = _agent(
        tmp_path,
        [[("get_price", {"sku": "SKU-1"}, "c1")], "SKU-1 is 12.00 USD."],
        [get_price],
    )
    response = asyncio.run(agent.run("price for SKU-1?"))
    assert response.text == "SKU-1 is 12.00 USD."

    caps = _ledger(tmp_path)
    assert [c["effect"]["status"] for c in caps] == [
        "planned",  # run planned
        "planned",  # get_price planned
        "confirmed",  # get_price confirmed
        "confirmed",  # run confirmed
    ]
    assert all(verify(c).ok for c in caps)
    assert caps[2]["chain"]["parent_capsule_id"] == caps[1]["capsule_id"]
    assert caps[3]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]
    # The model reached the tool capsule through the run seam's ContextVar.
    assert caps[1]["model_attestation"]["model_id"] == "scripted-demo-model"
    assert len(mw[0].results) == 4


def test_end_to_end_concurrent_tool_calls_do_not_cross_chain(tmp_path):
    agent, _ = _agent(
        tmp_path,
        [
            [("get_price", {"sku": "SKU-1"}, "c1"), ("get_price", {"sku": "SKU-2"}, "c2")],
            "done",
        ],
        [get_price],
    )
    asyncio.run(agent.run("both prices"))

    caps = _ledger(tmp_path)
    planned = {c["capsule_id"] for c in caps if c["effect"]["status"] == "planned"}
    confirmed = [c for c in caps if c["effect"]["status"] == "confirmed"]
    parents = [c["chain"]["parent_capsule_id"] for c in confirmed]
    assert len(parents) == len(set(parents))  # no two outcomes share a commitment
    assert set(parents) <= planned


def test_end_to_end_tool_exception_is_errored_and_the_run_still_completes(tmp_path):
    agent, _ = _agent(
        tmp_path,
        [[("submit_order", {"po": "PO-7"}, "c1")], "That order failed."],
        [submit_order],
    )
    response = asyncio.run(agent.run("submit PO-7"))
    assert response.text == "That order failed."

    caps = _ledger(tmp_path)
    errored = [c for c in caps if c["disposition"]["verdict_class"] == "errored"]
    assert len(errored) == 1
    assert errored[0]["effect"]["status"] == "failed"
    assert all(verify(c).ok for c in caps)


def test_end_to_end_fail_closed_deny_is_recorded_and_still_propagates(tmp_path):
    from agent_framework import FunctionMiddleware, MiddlewareFailure

    class DenyOrders(FunctionMiddleware):
        async def process(self, context, call_next):
            raise MiddlewareFailure("purchase orders require human approval")

    agent, _ = _agent(
        tmp_path,
        [[("submit_order", {"po": "PO-8"}, "c1")], "unreachable"],
        [submit_order],
        [DenyOrders()],
    )
    # Swallowing MiddlewareFailure would turn a fail-closed abort back into a running
    # loop (_middleware.py:116) — the adapter re-raises everything it observes.
    with pytest.raises(MiddlewareFailure):
        asyncio.run(agent.run("submit PO-8"))

    blocked = [c for c in _ledger(tmp_path) if c["disposition"]["verdict_class"] == "blocked"]
    # Both seams saw the refusal: the tool call and the run it aborted.
    assert {c["model_attestation"]["compute_attestation"]["agent_framework_seam"] for c in blocked} == {
        "function",
        "agent",
    }
    for cap in blocked:
        assert cap["effect"]["status"] == "planned"
        assert cap["model_attestation"]["compute_attestation"]["agent_framework_blocked_by"] == (
            "MiddlewareFailure"
        )
        assert verify(cap).ok


def test_end_to_end_graceful_termination_blocks_the_call_but_not_the_run(tmp_path):
    from agent_framework import FunctionMiddleware, MiddlewareTermination

    class StopOrders(FunctionMiddleware):
        async def process(self, context, call_next):
            raise MiddlewareTermination("policy: stopping the loop")

    agent, _ = _agent(
        tmp_path,
        [[("submit_order", {"po": "PO-9"}, "c1")], "Stopped."],
        [submit_order],
        [StopOrders()],
    )
    asyncio.run(agent.run("submit PO-9"))

    caps = _ledger(tmp_path)
    blocked = [c for c in caps if c["disposition"]["verdict_class"] == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["model_attestation"]["compute_attestation"]["agent_framework_seam"] == "function"
    # The run itself completed — MiddlewareTermination stops the loop gracefully.
    assert caps[-1]["effect"]["status"] == "confirmed"


def test_end_to_end_a_broken_emitter_does_not_corrupt_the_tool_result(tmp_path):
    from agent_framework import Agent

    broken = tmp_path / "ledger.jsonl"
    broken.mkdir()
    mw = capsule_middleware(
        operator="acme-co", developer="my-agent@v1", ledger=broken, anchor=False
    )
    agent = Agent(
        _scripted_client([[("get_price", {"sku": "SKU-1"}, "c1")], "SKU-1 is 12.00 USD."]),
        "you are a procurement assistant",
        name="procurement",
        tools=[get_price],
        middleware=mw,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        response = asyncio.run(agent.run("price for SKU-1?"))
    assert response.text == "SKU-1 is 12.00 USD."


def test_a_raising_middleware_silently_corrupts_the_tool_result(tmp_path):
    """The hazard the never-raises guarantee exists to avoid.

    An ordinary exception from function middleware is absorbed into a tool-error result
    (_tools.py:1641) — the run keeps going and the model reads an error that the tool
    never produced. Nothing crashes, so nothing surfaces.
    """
    from agent_framework import Agent, FunctionMiddleware

    seen: list = []

    class Careless(FunctionMiddleware):
        async def process(self, context, call_next):
            await call_next()
            seen.append(context.result)
            raise ValueError("a bug in someone's observability layer")

    agent = Agent(
        _scripted_client([[("get_price", {"sku": "SKU-1"}, "c1")], "done"]),
        "you are a procurement assistant",
        name="procurement",
        tools=[get_price],
        middleware=[Careless()],
    )
    asyncio.run(agent.run("price for SKU-1?"))
    # The tool DID produce 12.00 USD ...
    assert "12.00 USD" in str(seen[0][0].to_dict())
    # ... and the loop still completed, having replaced it with an error result.


def test_two_agents_on_different_models_do_not_cross_attribute(tmp_path):
    from agent_framework import Agent

    def build(model, ledger_name):
        mw = capsule_middleware(
            operator="acme-co",
            developer="my-agent@v1",
            ledger=tmp_path / ledger_name,
            anchor=False,
        )
        return Agent(
            _scripted_client(
                [[("get_price", {"sku": "SKU-1"}, "c1")], "done"], model=model, provider=model
            ),
            "assistant",
            name=model,
            tools=[get_price],
            middleware=mw,
        )

    async def both():
        await asyncio.gather(
            build("model-a", "a.jsonl").run("go"), build("model-b", "b.jsonl").run("go")
        )

    asyncio.run(both())

    for name, model in (("a.jsonl", "model-a"), ("b.jsonl", "model-b")):
        caps = [json.loads(x) for x in (tmp_path / name).read_text().splitlines()]
        assert {c["model_attestation"]["model_id"] for c in caps} == {model}


def test_shipped_demo_runs_hermetically_and_every_capsule_verifies():
    demo = Path(__file__).parent.parent.parent / "examples" / "msft-agent-framework" / "demo.py"
    proc = subprocess.run(
        [sys.executable, str(demo)], capture_output=True, text=True, timeout=180
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all capsules verify offline" in proc.stdout
    assert "FAIL" not in proc.stdout
