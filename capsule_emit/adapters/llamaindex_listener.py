# SPDX-License-Identifier: Apache-2.0
"""LlamaIndex listener — planned/confirmed/failed capsules per agent tool call.

    from llama_index.core.agent.workflow import FunctionAgent
    from capsule_emit.adapters.llamaindex_listener import LlamaIndexCapsuleListener

    listener = LlamaIndexCapsuleListener(operator="acme-co", developer="my-agent@v1")
    listener.install()                      # global, one line, no agent changes

    agent = FunctionAgent(tools=[...], llm=...)
    await agent.run("...")

Requires ``pip install "capsule-emit[llamaindex]"``.

Fifth sibling in the listener family (``crewai_listener`` / ``langchain_listener`` /
``agno_listener`` / ``strands_listener``), with the same core/shell split and the same
two-record chain:

- before the tool runs → **planned** capsule (the commitment record)
- after it returns     → **confirmed** capsule, ``confirms``-chained to the planned
  one (response digest auto-derived)
- tool errored         → ``verdict="errored"``, ``effect.status="failed"``, chained

Which surface, and why
----------------------
LlamaIndex offers three plausible taps. Only one of them carries a tool payload in
the released ``llama-index-core==0.14.24`` / ``llama-index-instrumentation==0.6.0``
wheels, and it is not the one the name suggests. Verified by grep over the installed
wheel and by an executed probe (a real ``FunctionAgent`` run against a scripted
no-network LLM, two tool calls, one of them raising):

1. **Legacy ``CallbackManager``** (``llama_index/core/callbacks/``). ``CBEventType``
   still *declares* ``FUNCTION_CALL`` and ``AGENT_STEP`` (``callbacks/schema.py:43``,
   ``:47``), but **nothing in the wheel dispatches them** — ``grep -rn
   'CBEventType.FUNCTION_CALL'`` over ``site-packages`` matches only the two enum
   definitions. The surviving callback emitters are LLM / QUERY / EMBEDDING /
   RETRIEVE / CHUNKING / SYNTHESIZE / NODE_PARSING / TREE / SUB_QUESTION / RERANKING
   / TEMPLATING / EXCEPTION. In the probe, a ``CallbackManager`` installed on
   ``Settings`` saw **zero events** across the whole agent run. This surface is dead
   for tools.

2. **Instrumentation event handlers** (``BaseEventHandler.handle``). ``AgentToolCallEvent``
   exists (``core/instrumentation/events/agent.py:115``) and carries ``arguments`` +
   ``tool``, but it too has **zero dispatch sites**, and there is no corresponding
   *result* event at all. What the agent path actually emits is
   ``WorkflowStepOutputEvent`` (``workflows/runtime/types/step_function.py:71``) whose
   only payload field is ``output: str`` — a truncated human summary, not the call.
   In the probe this surface produced 9 ``WorkflowStepOutputEvent`` + 1
   ``SpanDropEvent`` + 1 ``WorkflowRunOutputEvent``, and no tool payload.

3. **Instrumentation span handlers** (``BaseSpanHandler``) — **the one we use.** The
   agent's tool step ``BaseWorkflowAgent.call_tool`` is wrapped by
   ``workflow._dispatcher.span(...)`` (``step_function.py:275``), so:

   - ``span_enter`` receives ``bound_args`` binding ``ev`` = a ``ToolCall``
     (``core/agent/workflow/workflow_events.py:98``) with ``tool_name``,
     ``tool_kwargs``, ``tool_id``;
   - ``span_exit`` receives ``result`` = a ``ToolCallResult`` (``:106``) with the same
     three fields plus ``tool_output: ToolOutput`` (``content``, ``raw_input``,
     ``raw_output``, ``is_error``) and ``return_direct``;
   - ``span_drop`` receives the exception.

   Full payload on both sides, exact pairing, and registration is one global call —
   ``get_dispatcher().add_span_handler(...)`` — with no change to the user's agent.

Pairing uses the **span id**, not ``tool_id``. Enter/exit/drop for one call share an
id_ by construction (``llama_index_instrumentation/dispatcher.py:357``), so the chain
is exact even when the agent fans several tool calls out concurrently and even if a
model reuses a ``tool_id``. The ``tool_id`` is still recorded, as evidence.

Detection is by **payload shape, not span name**. ``call_tool`` is defined on
``BaseWorkflowAgent`` and re-defined on ``AgentWorkflow``, and the span id is built
from ``func.__qualname__`` when the wrapped step is not a bound method — so the name
is already two different strings today. A span is ours if its bound arguments carry a
ToolCall-shaped object (``tool_name`` + ``tool_id`` + ``tool_kwargs``, and *no*
``tool_output``), and its outcome is ours if the result is ToolCallResult-shaped
(``tool_id`` + ``tool_output``). That shape test also keeps us off the two adjacent
spans that would otherwise double-count: ``BaseWorkflowAgent.aggregate_tool_results``
(enters *with* a ``ToolCallResult`` in bound args — excluded by the ``tool_output``
carve) and ``FunctionTool.acall`` (exits with a bare ``ToolOutput``, which has no
``tool_id`` — excluded by the ``tool_id`` carve).

Observation only — here, almost by construction
-----------------------------------------------
A span handler's return value feeds only the handler's own bookkeeping
(``span_handlers/base.py:100-140``); nothing it returns reaches the agent. The one
genuine in-path capability is that ``bound_args`` holds *live references* to the step's
arguments, so mutating ``bound_args.arguments["ev"].tool_kwargs`` would change the
call that then runs. **This listener never mutates ``bound_args``, the objects
reachable from it, or the result.** Recorded here so the capability is not lost and
nobody has to re-discover it to make the deny case — see the ``observation_mode``
proposal in capsule-emit #48. Every capsule is stamped
``observation_mode="event_stream"``.

Never-raises — and here it buys visibility, not safety
------------------------------------------------------
Unlike Strands (where a raising callback aborts the tool stream), the LlamaIndex
dispatcher already wraps every span-handler call in ``except BaseException: pass``
(``dispatcher.py:203``, ``:230``, ``:262``). So a careless listener cannot break the
agent — it can only **fail silently**, which for an evidence layer is the worse
failure. Our guard therefore exists to turn that silence into a visible
``RuntimeWarning``: a broken ledger or anchor endpoint warns once per capsule and is
skipped, and the run is unaffected either way.

Privacy: inputs and outputs are digested, never stored — the ledger carries
``agent_input_digest`` / ``agent_output_digest`` only. Payloads are projected into
canonical-JSON-safe shapes first (bytes and un-encodable objects become type markers);
raw floats are deliberately left alone so they still fail closed at the digest layer
(``FloatInDigestError`` → warning, no capsule, run unaffected).

All sealing logic lives in the framework-free :class:`LlamaIndexListenerCore` (fully
testable without llama-index installed); :class:`LlamaIndexCapsuleListener` is the
thin shell that binds it to a ``BaseSpanHandler`` and the dispatcher.
"""
from __future__ import annotations

import warnings
from collections import OrderedDict
from typing import Any

from ._base import CapsuleEmitterBase

__all__ = ["LlamaIndexCapsuleListener", "LlamaIndexListenerCore"]

# Depth cap when walking a span's parent chain to its root. The observed agent
# lineage is two levels (FunctionAgent.run -> call_tool); the cap only exists so a
# cyclic or pathological chain cannot spin.
_MAX_ROOT_WALK = 64


def _tool_call_fields(obj: Any) -> tuple[str, str | None, Any] | None:
    """``(tool_name, tool_id, tool_kwargs)`` if ``obj`` is ToolCall-shaped, else None.

    ToolCall-shaped means: has ``tool_name`` and ``tool_kwargs``, and does **not**
    have ``tool_output``. The negative carve is what keeps
    ``aggregate_tool_results`` — which enters with a ``ToolCallResult`` bound to the
    same parameter name — from being mistaken for a second tool dispatch.
    """
    if obj is None or hasattr(obj, "tool_output"):
        return None
    name = getattr(obj, "tool_name", None)
    if name is None or not hasattr(obj, "tool_kwargs"):
        return None
    raw_id = getattr(obj, "tool_id", None)
    return str(name), None if raw_id is None else str(raw_id), getattr(obj, "tool_kwargs", None)


def _tool_result_fields(obj: Any) -> tuple[str, str | None, Any] | None:
    """``(tool_name, tool_id, tool_output)`` if ``obj`` is ToolCallResult-shaped.

    Requires ``tool_output`` *and* ``tool_id``. The ``tool_id`` requirement is what
    excludes ``FunctionTool.acall``'s bare ``ToolOutput`` return, which would
    otherwise seal a duplicate outcome capsule for every call.
    """
    if obj is None or not hasattr(obj, "tool_output") or not hasattr(obj, "tool_id"):
        return None
    name = getattr(obj, "tool_name", None) or "tool"
    raw_id = getattr(obj, "tool_id", None)
    return str(name), None if raw_id is None else str(raw_id), getattr(obj, "tool_output", None)


def _json_safe(value: Any, _depth: int = 0) -> Any:
    """Project a payload into something the JCS digest can canonicalize.

    ``str``/``bool``/``int``/``None`` and nested dict/list/tuple of those pass through.
    ``bytes`` become ``"<omitted:N bytes>"``. Anything else becomes ``"<TypeName>"`` —
    a marker saying *an object of this type was here*, which is honest and
    canonicalizable, where the object itself is neither.

    ``float`` is deliberately **not** converted. Raw floats are meant to fail closed at
    the digest layer (``FloatInDigestError``); silently stringifying them here would
    launder a payload the spec refuses to digest. Family behaviour, unchanged.
    """
    if _depth > 12:
        return "<max-depth>"
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<omitted:{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, _depth + 1) for v in value]
    return f"<{type(value).__name__}>"


def _tool_output_payload(tool_output: Any) -> Any:
    """Project a ``ToolOutput`` into the evidence we digest.

    ``content`` is the string the agent actually fed back to the model, so it is the
    load-bearing field. ``is_error`` is kept because the verdict is derived from it and
    a reader should be able to see the two agree. ``raw_output`` is kept only when it
    survives :func:`_json_safe`. ``raw_input`` is deliberately dropped — it is the
    *input*, already digested on the planned capsule, and including it here would put
    the same bytes under two different digests.
    """
    if tool_output is None:
        return None
    if not hasattr(tool_output, "content") and not hasattr(tool_output, "is_error"):
        return _json_safe(tool_output)
    payload: dict[str, Any] = {
        "content": _json_safe(getattr(tool_output, "content", None)),
        "is_error": bool(getattr(tool_output, "is_error", False)),
    }
    raw_output = getattr(tool_output, "raw_output", None)
    if raw_output is not None:
        payload["raw_output"] = _json_safe(raw_output)
    return payload


def _model_from_instance(instance: Any) -> dict[str, str] | None:
    """``{provider, model_id}`` off an LLM-ish span instance, or None.

    Duck-typed so this module stays importable without llama-index. Every LlamaIndex
    LLM exposes ``metadata.model_name`` (``core/base/llms/types.py``, ``LLMMetadata``);
    the provider name is the class's module leaf, matching the sibling adapters. An
    object that answers neither is skipped rather than guessed at.
    """
    if instance is None:
        return None
    metadata = getattr(instance, "metadata", None)
    model_id = getattr(metadata, "model_name", None) if metadata is not None else None
    if not model_id:
        return None
    provider = type(instance).__module__.rsplit(".", 1)[-1] or "unknown"
    return {"provider": str(provider), "model_id": str(model_id)}


class LlamaIndexListenerCore(CapsuleEmitterBase):
    """Framework-free core of the LlamaIndex span listener.

    :meth:`on_span_enter`, :meth:`on_span_exit` and :meth:`on_span_drop` take plain
    values, so the whole behaviour is exercised in tests without llama-index
    installed. :class:`LlamaIndexCapsuleListener` is the shell that routes real spans
    here.

    Sealing map (all through ``emit_capsule()``; capsule envelope invariant):

    - tool span entered              → ``effect.status="planned"``
    - result with ``is_error=False`` → ``effect.status="confirmed"``, chained
    - result with ``is_error=True``  → ``verdict="errored"``, ``effect.status="failed"``, chained
    - span dropped (raised)          → ``verdict="errored"``, ``effect.status="failed"``, chained

    Args:
        capture_model: Auto-stamp the model on tool capsules (default True). See
            :meth:`observe_span_instance` for exactly what "the model" means here and
            where it can mis-attribute. Set False and pass ``model=`` to record a
            model you are certain of instead.
        max_pending: Bound on the remembered planned-capsule ids, parent links and
            per-root models (default 256), so a long run that sees enters without
            matching exits cannot grow any table without bound.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer, ledger,
            anchor, anchor_url, anchor_wait, model, max_results).
    """

    def __init__(
        self,
        *,
        capture_model: bool = True,
        max_pending: int = 256,
        **base_kw: Any,
    ) -> None:
        super().__init__(**base_kw)
        self._capture_model = capture_model
        self._max_pending = max_pending
        # span id -> (planned capsule id | None, tool_name, tool_id)
        self._pending: OrderedDict[str, tuple[str | None, str, str | None]] = OrderedDict()
        # span id -> parent span id (for resolving a span's root)
        self._parent: OrderedDict[str, str | None] = OrderedDict()
        # root span id -> most recently observed model under that root
        self._model_by_root: OrderedDict[str, dict[str, str]] = OrderedDict()

    # -- helpers -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising.

        The dispatcher swallows handler exceptions silently; this converts that
        silence into a warning a human can see. See the module docstring.
        """
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: LlamaIndex listener failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    def _bound(self, table: OrderedDict[str, Any]) -> None:
        while len(table) >= self._max_pending:
            table.popitem(last=False)  # evict oldest

    def _root_of(self, span_id: str) -> str:
        """Walk the recorded parent links to the outermost known span."""
        current = span_id
        for _ in range(_MAX_ROOT_WALK):
            parent = self._parent.get(current)
            if parent is None or parent == current:
                return current
            current = parent
        return current

    def _model_for(self, span_id: str) -> dict[str, str] | None:
        if not self._capture_model:
            return None
        return self._model_by_root.get(self._root_of(span_id))

    def _extra(self, tool_id: str | None, **more: Any) -> dict[str, Any]:
        """Compute-attestation add-ons carried by every capsule this listener seals."""
        extra: dict[str, Any] = {"observation_mode": "event_stream"}
        if tool_id is not None:
            extra["llamaindex_tool_id"] = tool_id
        extra.update(more)
        return extra

    # -- the span callbacks ------------------------------------------------

    def observe_span_instance(self, span_id: str, parent_span_id: str | None, instance: Any) -> None:
        """Record a span's parent link and, if it is an LLM span, its model.

        The model stamped on a tool capsule is defined as *the most recent model
        observed under the same root span as that tool call*. That is a statement
        about what was observed, not an inference: the ``call_tool`` span carries
        ``instance=None`` (the wrapped step is not a bound method, so the dispatcher
        takes its ``func.__qualname__`` branch, ``dispatcher.py:359``), so there is no
        agent or LLM handle at the tool boundary itself. In the observed lineage the
        LLM span and the tool span are siblings under one run span
        (``FunctionAgent.run``), and the LLM span always precedes the tool span.

        Where this can mis-attribute: a multi-agent workflow whose sub-agents use
        *different* LLMs under one root, running concurrently. Then "most recent under
        this root" may not be the model that chose this particular tool call. Pass
        ``capture_model=False`` plus an explicit ``model=`` if that distinction
        matters to you.
        """
        self._bound(self._parent)
        self._parent[span_id] = parent_span_id
        if not self._capture_model:
            return
        model = _model_from_instance(instance)
        if model is None:
            return
        root = self._root_of(span_id)
        self._bound(self._model_by_root)
        self._model_by_root[root] = model

    def on_span_enter(self, span_id: str, arguments: Any) -> None:
        """A span started → planned capsule if its bound args carry a ToolCall.

        ``arguments`` is the mapping of bound argument name → value (i.e.
        ``inspect.BoundArguments.arguments``). The listener reads it and writes
        nothing back to it or to the objects inside it.
        """
        fields = self._first_tool_call(arguments)
        if fields is None:
            return
        tool_name, tool_id, tool_kwargs = fields
        result = self._seal(
            action=tool_name,
            tool_input=_json_safe(tool_kwargs),
            effect={"type": tool_name, "status": "planned"},
            action_type="fyi",
            runtime="llamaindex",
            model=self._model_for(span_id),
            extra_compute=self._extra(tool_id),
        )
        planned_id = None if result is None else result.capsule_id
        self._bound(self._pending)
        self._pending[span_id] = (planned_id, tool_name, tool_id)

    def on_span_exit(self, span_id: str, result: Any) -> None:
        """A span returned → confirmed / failed capsule, chained, if it is a tool span."""
        fields = _tool_result_fields(result)
        pending = self._pending.pop(span_id, None)
        if fields is None:
            # Not a tool outcome. If we had opened a planned capsule for this span we
            # would be leaving it unchained, which should not happen given the shape
            # tests — but drop the entry rather than leak it.
            return
        tool_name, tool_id, tool_output = fields
        planned_id = pending[0] if pending is not None else None
        if pending is not None and tool_id is None:
            tool_id = pending[2]
        is_error = bool(getattr(tool_output, "is_error", False))
        self._seal(
            action=tool_name,
            tool_output=_tool_output_payload(tool_output),
            verdict="errored" if is_error else "executed",
            effect={"type": tool_name, "status": "failed" if is_error else "confirmed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime="llamaindex",
            model=self._model_for(span_id),
            extra_compute=self._extra(
                tool_id,
                **(
                    {"llamaindex_return_direct": True}
                    if getattr(result, "return_direct", False)
                    else {}
                ),
            ),
        )

    def on_span_drop(self, span_id: str, err: BaseException | None) -> None:
        """A span raised → failed capsule, chained, if we had opened one for it.

        Only fires for spans we planned. In the observed agent path the tool step
        absorbs tool exceptions into ``ToolOutput.is_error`` and still exits normally,
        so this covers the step itself failing (tool lookup, event write), not the
        ordinary "the tool raised" case — which arrives through :meth:`on_span_exit`.
        """
        pending = self._pending.pop(span_id, None)
        if pending is None:
            return
        planned_id, tool_name, tool_id = pending
        self._seal(
            action=tool_name,
            tool_output={"error": "unknown" if err is None else str(err)},
            verdict="errored",
            effect={"type": tool_name, "status": "failed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime="llamaindex",
            model=self._model_for(span_id),
            extra_compute=self._extra(
                tool_id,
                llamaindex_span_dropped=True,
                llamaindex_error_type="unknown" if err is None else type(err).__name__,
            ),
        )

    @staticmethod
    def _first_tool_call(arguments: Any) -> tuple[str, str | None, Any] | None:
        """The first ToolCall-shaped bound argument, or None."""
        if not arguments:
            return None
        try:
            values = arguments.values() if hasattr(arguments, "values") else list(arguments)
        except Exception:  # noqa: BLE001 — a weird bound-args mapping is not worth an exception
            return None
        for value in values:
            fields = _tool_call_fields(value)
            if fields is not None:
                return fields
        return None


class LlamaIndexCapsuleListener:
    """Seals one capsule per LlamaIndex agent tool-call boundary.

    Install it globally — no fork, no monkeypatch, no change to the agent::

        listener = LlamaIndexCapsuleListener(operator="acme-co", developer="my-agent@v1")
        listener.install()

    or hand the span handler to a dispatcher yourself::

        from llama_index.core.instrumentation import get_dispatcher
        get_dispatcher().add_span_handler(listener.span_handler)

    Accepts the same configuration as :class:`LlamaIndexListenerCore` and exposes the
    core as :attr:`core` (``listener.core.last`` / ``.results``).

    llama-index is imported lazily, inside :attr:`span_handler`, so this listener —
    and its whole sealing core — stays importable and testable without the SDK.
    ``BaseSpanHandler`` is a concrete pydantic base rather than a Protocol, so the
    handler subclass is built on first use instead of at module import.
    """

    def __init__(self, **core_kw: Any) -> None:
        self.core = LlamaIndexListenerCore(**core_kw)
        self._span_handler: Any = None
        self._installed_on: list[Any] = []

    @property
    def span_handler(self) -> Any:
        """The ``BaseSpanHandler`` bound to this listener's core (built on first use)."""
        if self._span_handler is None:
            self._span_handler = _make_span_handler(self.core)
        return self._span_handler

    def install(self, dispatcher: Any = None) -> LlamaIndexCapsuleListener:
        """Register the span handler on ``dispatcher`` (default: the root dispatcher).

        Returns self, so ``listener = LlamaIndexCapsuleListener(...).install()`` works.
        """
        if dispatcher is None:
            try:
                from llama_index.core.instrumentation import get_dispatcher
            except ImportError as exc:  # pragma: no cover - only without llama-index
                raise ImportError(
                    "LlamaIndexCapsuleListener needs llama-index-core. "
                    'Install with: pip install "capsule-emit[llamaindex]"'
                ) from exc
            dispatcher = get_dispatcher()
        dispatcher.add_span_handler(self.span_handler)
        self._installed_on.append(dispatcher)
        return self

    def uninstall(self) -> None:
        """Remove the span handler from every dispatcher :meth:`install` put it on.

        The dispatcher exposes ``add_span_handler`` but no remove, so this mutates the
        public ``span_handlers`` list directly. Idempotent, and never raises if the
        handler is already gone.
        """
        for dispatcher in self._installed_on:
            handlers = getattr(dispatcher, "span_handlers", None)
            if handlers is None:
                continue
            try:
                while self._span_handler in handlers:
                    handlers.remove(self._span_handler)
            except Exception:  # noqa: BLE001 — teardown never breaks a caller
                pass
        self._installed_on = []

    # convenience passthroughs, mirroring the sibling listeners
    @property
    def last(self) -> Any:
        """The most recent EmitResult, or None."""
        return self.core.last

    @property
    def results(self) -> list[Any]:
        """All EmitResults sealed this session."""
        return self.core.results


def _make_span_handler(core: LlamaIndexListenerCore) -> Any:
    """Build a ``BaseSpanHandler`` that routes tool spans into ``core``.

    Defined here rather than at module scope because ``BaseSpanHandler`` is a concrete
    pydantic model, not a Protocol — subclassing it at import time would make this
    module unimportable without llama-index and would take the sealing core down with
    it.

    All three hooks return ``None``. That is deliberate: ``BaseSpanHandler`` only
    records a span in ``open_spans`` when ``new_span`` returns something truthy, and
    only ``del``\\ s it on exit when ``prepare_to_exit_span`` does
    (``span_handlers/base.py:100-140``). Returning ``None`` throughout keeps that
    bookkeeping empty and unbounded-growth-free, and keeps this listener's own bounded
    tables the single source of pairing state.
    """
    try:
        from llama_index.core.instrumentation.span_handlers import BaseSpanHandler
    except ImportError as exc:  # pragma: no cover - only without llama-index
        raise ImportError(
            "LlamaIndexCapsuleListener needs llama-index-core. "
            'Install with: pip install "capsule-emit[llamaindex]"'
        ) from exc

    class _CapsuleSpanHandler(BaseSpanHandler):  # type: ignore[misc, valid-type]
        """Routes LlamaIndex tool spans into a capsule-emit listener core."""

        @classmethod
        def class_name(cls) -> str:
            return "CapsuleEmitSpanHandler"

        def new_span(
            self,
            id_: str,
            bound_args: Any,
            instance: Any = None,
            parent_span_id: str | None = None,
            tags: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> None:
            core.observe_span_instance(id_, parent_span_id, instance)
            core.on_span_enter(id_, getattr(bound_args, "arguments", None))
            return None

        def prepare_to_exit_span(
            self,
            id_: str,
            bound_args: Any,
            instance: Any = None,
            result: Any = None,
            **kwargs: Any,
        ) -> None:
            core.on_span_exit(id_, result)
            return None

        def prepare_to_drop_span(
            self,
            id_: str,
            bound_args: Any,
            instance: Any = None,
            err: BaseException | None = None,
            **kwargs: Any,
        ) -> None:
            core.on_span_drop(id_, err)
            return None

    return _CapsuleSpanHandler()
