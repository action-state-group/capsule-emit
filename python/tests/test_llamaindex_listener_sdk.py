# SPDX-License-Identifier: Apache-2.0
"""LlamaIndex listener tests against a real ``llama-index-core``: the span
handler shell and hermetic ``FunctionAgent`` runs. The framework-free core is
tested in ``test_llamaindex_listener.py``.
"""
from __future__ import annotations

import pytest
from test_llamaindex_listener import (  # helpers shared with the framework-free tests
    FakeToolCall,
    FakeToolCallResult,
    FakeToolOutput,
    _compute,
    _ledger,
)

from capsule_emit.adapters.llamaindex_listener import (
    LlamaIndexCapsuleListener,
)
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Shell: real llama-index
# ---------------------------------------------------------------------------

llama_index = pytest.importorskip("llama_index.core", reason="llama-index-core not installed")


class _FakeDispatcher:
    def __init__(self):
        self.span_handlers = []

    def add_span_handler(self, handler):
        self.span_handlers.append(handler)


def test_install_and_uninstall_on_a_dispatcher(tmp_path):
    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    )
    dispatcher = _FakeDispatcher()
    assert listener.install(dispatcher) is listener
    assert listener.span_handler in dispatcher.span_handlers

    listener.uninstall()
    assert listener.span_handler not in dispatcher.span_handlers
    listener.uninstall()  # idempotent


def test_span_handler_is_a_real_base_span_handler(tmp_path):
    from llama_index.core.instrumentation.span_handlers import BaseSpanHandler

    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    )
    handler = listener.span_handler
    assert isinstance(handler, BaseSpanHandler)
    assert listener.span_handler is handler  # built once


def test_span_handler_keeps_no_open_span_bookkeeping(tmp_path):
    """All three hooks return None so BaseSpanHandler's own dicts stay empty —
    otherwise a long-lived global handler would grow without bound."""
    import inspect

    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    )
    handler = listener.span_handler

    def _bound(**kwargs):
        def fn(ev):
            return None

        return inspect.signature(fn).bind(**kwargs)

    handler.span_enter(
        id_="span-1", bound_args=_bound(ev=FakeToolCall("t", {"a": "b"}, "tc-1")), instance=None
    )
    handler.span_exit(
        id_="span-1",
        bound_args=_bound(ev=FakeToolCall("t", {"a": "b"}, "tc-1")),
        instance=None,
        result=FakeToolCallResult("t", {"a": "b"}, "tc-1", FakeToolOutput("ok")),
    )

    assert handler.open_spans == {}
    assert handler.completed_spans == []
    assert len(listener.results) == 2
    assert listener.results[1].capsule["chain"]["parent_capsule_id"] == (
        listener.results[0].capsule_id
    )


def _scripted_llm(script):
    """The smallest FunctionCallingLLM that drives FunctionAgent with no key."""
    from llama_index.core.base.llms.types import (
        ChatMessage,
        ChatResponse,
        CompletionResponse,
        LLMMetadata,
    )
    from llama_index.core.llms.function_calling import FunctionCallingLLM
    from llama_index.core.llms.llm import ToolSelection

    class ScriptedLLM(FunctionCallingLLM):
        script: list = []
        turn: int = 0

        @property
        def metadata(self):
            return LLMMetadata(
                model_name="scripted-test-model",
                is_function_calling_model=True,
                is_chat_model=True,
            )

        def _response(self):
            calls = self.script[self.turn] if self.turn < len(self.script) else []
            self.turn += 1
            message = ChatMessage(role="assistant", content="" if calls else "done")
            message.additional_kwargs["tool_calls"] = calls
            return ChatResponse(message=message, delta=message.content or "")

        def chat(self, messages, **kw):
            return self._response()

        async def achat(self, messages, **kw):
            return self._response()

        async def astream_chat(self, messages, **kw):
            response = self._response()

            async def gen():
                yield response

            return gen()

        def complete(self, prompt, formatted=False, **kw):
            return CompletionResponse(text="done")

        async def acomplete(self, prompt, formatted=False, **kw):
            return CompletionResponse(text="done")

        def stream_chat(self, messages, **kw):
            raise NotImplementedError

        def stream_complete(self, prompt, formatted=False, **kw):
            raise NotImplementedError

        async def astream_complete(self, prompt, formatted=False, **kw):
            raise NotImplementedError

        def _prepare_chat_with_tools(self, tools, user_msg=None, chat_history=None, **kw):
            messages = list(chat_history or [])
            if user_msg:
                messages.append(
                    ChatMessage(role="user", content=user_msg)
                    if isinstance(user_msg, str)
                    else user_msg
                )
            return {"messages": messages}

        def get_tool_calls_from_response(self, response, error_on_no_tool_call=True, **kw):
            raw = response.message.additional_kwargs.get("tool_calls") or []
            return [
                ToolSelection(tool_id=c["id"], tool_name=c["name"], tool_kwargs=c["args"])
                for c in raw
            ]

    return ScriptedLLM(script=script)


@pytest.fixture
def _isolated_dispatcher():
    """A dispatcher the agent actually uses, cleaned up afterwards.

    The agent path dispatches through the root dispatcher, so the shell tests
    install there and remove the handler again — leaving a handler behind would
    make every later test in the session seal capsules.
    """
    from llama_index.core.instrumentation import get_dispatcher

    dispatcher = get_dispatcher()
    before = list(dispatcher.span_handlers)
    yield dispatcher
    dispatcher.span_handlers[:] = before


def _run_agent(listener, tools, calls):
    """Drive a real FunctionAgent to completion.

    ``FunctionAgent.run()`` schedules the workflow eagerly with
    ``asyncio.create_task``, so it must be *called* from inside a running loop,
    not merely awaited from one.
    """
    import asyncio

    from llama_index.core.agent.workflow import FunctionAgent

    async def _go():
        agent = FunctionAgent(
            tools=tools, llm=_scripted_llm([calls, []]), system_prompt="test agent"
        )
        await agent.run("go")

    asyncio.run(_go())


def test_real_function_agent_run_seals_a_planned_confirmed_chain(tmp_path, _isolated_dispatcher):
    from llama_index.core.tools import FunctionTool

    def get_price(sku: str) -> str:
        """Return the current price for a SKU."""
        return f"{sku} is 4225 cents"

    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    ).install(_isolated_dispatcher)

    _run_agent(
        listener,
        [FunctionTool.from_defaults(get_price)],
        [{"id": "call-1", "name": "get_price", "args": {"sku": "SKU-9"}}],
    )
    listener.uninstall()

    caps = _ledger(tmp_path)
    assert len(caps) == 2, [c["effect"]["status"] for c in caps]
    planned, confirmed = caps
    assert planned["effect"]["status"] == "planned"
    assert confirmed["effect"]["status"] == "confirmed"
    assert confirmed["chain"]["parent_capsule_id"] == planned["capsule_id"]
    assert _compute(planned)["llamaindex_tool_id"] == "call-1"
    assert all(verify(c).ok for c in caps)


def test_real_function_agent_captures_the_model(tmp_path, _isolated_dispatcher):
    from llama_index.core.tools import FunctionTool

    def get_price(sku: str) -> str:
        """Return the current price for a SKU."""
        return f"{sku} is 4225 cents"

    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    ).install(_isolated_dispatcher)

    _run_agent(
        listener,
        [FunctionTool.from_defaults(get_price)],
        [{"id": "call-1", "name": "get_price", "args": {"sku": "SKU-9"}}],
    )
    listener.uninstall()

    caps = _ledger(tmp_path)
    assert caps[0]["model_attestation"]["model_id"] == "scripted-test-model"


def test_real_function_agent_records_a_raising_tool_as_failed(tmp_path, _isolated_dispatcher):
    from llama_index.core.tools import FunctionTool

    def submit_order(sku: str) -> str:
        """Submit an order. Always fails here."""
        raise RuntimeError("ERP rejected the order")

    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    ).install(_isolated_dispatcher)

    _run_agent(
        listener,
        [FunctionTool.from_defaults(submit_order)],
        [{"id": "call-1", "name": "submit_order", "args": {"sku": "SKU-9"}}],
    )
    listener.uninstall()

    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[1]["disposition"]["verdict_class"] == "errored"
    assert caps[1]["effect"]["status"] == "failed"
    assert caps[1]["chain"]["parent_capsule_id"] == caps[0]["capsule_id"]


def test_real_parallel_tool_calls_pair_correctly(tmp_path, _isolated_dispatcher):
    """Two tools in one model turn run as concurrent tasks; the chain must hold."""
    from llama_index.core.tools import FunctionTool

    def get_price(sku: str) -> str:
        """Price."""
        return f"{sku} is 4225 cents"

    def get_stock(sku: str) -> str:
        """Stock."""
        return f"{sku} has 118 units"

    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    ).install(_isolated_dispatcher)

    _run_agent(
        listener,
        [FunctionTool.from_defaults(get_price), FunctionTool.from_defaults(get_stock)],
        [
            {"id": "call-1", "name": "get_price", "args": {"sku": "SKU-9"}},
            {"id": "call-2", "name": "get_stock", "args": {"sku": "SKU-9"}},
        ],
    )
    listener.uninstall()

    caps = _ledger(tmp_path)
    assert len(caps) == 4
    by_id = {c["capsule_id"]: c for c in caps}
    outcomes = [c for c in caps if c["effect"]["status"] in ("confirmed", "failed")]
    assert len(outcomes) == 2
    for outcome in outcomes:
        parent = by_id[outcome["chain"]["parent_capsule_id"]]
        assert parent["effect"]["status"] == "planned"
        # the chain must join records for the SAME tool, not just any planned one
        assert parent["effect"]["type"] == outcome["effect"]["type"]


def test_a_tool_the_model_invents_is_recorded_as_failed(tmp_path, _isolated_dispatcher):
    from llama_index.core.tools import FunctionTool

    def get_price(sku: str) -> str:
        """Price."""
        return f"{sku} is 4225 cents"

    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    ).install(_isolated_dispatcher)

    _run_agent(
        listener,
        [FunctionTool.from_defaults(get_price)],
        [{"id": "call-1", "name": "no_such_tool", "args": {}}],
    )
    listener.uninstall()

    caps = _ledger(tmp_path)
    assert len(caps) == 2
    assert caps[0]["effect"]["type"] == "no_such_tool"
    assert caps[1]["disposition"]["verdict_class"] == "errored"


def test_uninstalling_stops_the_sealing(tmp_path, _isolated_dispatcher):
    from llama_index.core.tools import FunctionTool

    def get_price(sku: str) -> str:
        """Price."""
        return f"{sku} is 4225 cents"

    listener = LlamaIndexCapsuleListener(
        operator="acme-co", developer="a@v1", ledger=tmp_path / "ledger.jsonl", anchor=False
    ).install(_isolated_dispatcher)
    listener.uninstall()

    _run_agent(
        listener,
        [FunctionTool.from_defaults(get_price)],
        [{"id": "call-1", "name": "get_price", "args": {"sku": "SKU-9"}}],
    )

    assert _ledger(tmp_path) == []
