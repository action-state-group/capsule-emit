# SPDX-License-Identifier: Apache-2.0
"""OpenAI Agents SDK listener — planned/confirmed/failed capsules per tool call.

Two registration surfaces, because **neither one alone is complete**. Both are
first-class, documented extension points of the SDK; neither needs a fork::

    from agents import Agent, Runner
    from agents.tracing import add_trace_processor
    from capsule_emit.adapters.openai_agents_listener import (
        OpenAIAgentsCapsuleProcessor,   # a real TracingProcessor
        OpenAIAgentsCapsuleHooks,       # a real RunHooks
    )

    processor = OpenAIAgentsCapsuleProcessor(operator="acme-co", developer="my-agent@v1")
    add_trace_processor(processor)                       # global, no per-run wiring

    hooks = OpenAIAgentsCapsuleHooks(operator="acme-co", developer="my-agent@v1")
    await Runner.run(agent, "...", hooks=hooks)          # per-run, richer payload

Requires ``pip install "capsule-emit[openai-agents]"``.

Fifth sibling in the listener family (``crewai_listener`` / ``langchain_listener``
/ ``agno_listener`` / the proposed ``strands_listener``), with the same core/shell
split and the same two-record chain:

- before the tool runs → **planned** capsule (the commitment record)
- after it returns     → **confirmed** capsule, ``confirms``-chained to the planned one
- tool raised / errored → ``verdict="errored"``, ``effect.status="failed"``, chained

Which surface carries the tool arguments? (measured, not assumed)
-----------------------------------------------------------------
Verified by running a real ``Runner.run`` against the released
``openai-agents==0.22.0`` wheel with a scripted model (``agents.testing``), a
probe ``TracingProcessor`` and a probe ``RunHooks`` registered together. The
observed event order for one tool call is::

    span_start(FunctionSpanData)  input=None          <-- args NOT here yet
    hook_start                    tool_arguments='{"symbol":"ACME","qty":2.5}'
    hook_end                      result='ACME:26.25'
    span_end(FunctionSpanData)    input='{"symbol":...}'  output='ACME:26.25'

Two facts fall out of that, and they pull in opposite directions:

**1. The hooks carry the arguments; the tracing processor does not carry them in
time.** ``span_data.input`` is assigned *inside* the ``with function_span(...)``
block, after the span has already started and therefore after ``on_span_start``
has already fired (``agents/run_internal/tool_execution.py:1805`` opens the span,
``:1819`` assigns the input). So a processor's planned capsule — the one that must
be sealed *before* the tool runs to be a commitment at all — cannot commit to the
arguments. ``RunHooks.on_tool_start`` receives a ``ToolContext`` exposing
``tool_arguments`` and ``tool_call_id`` (``agents/lifecycle.py:70-82``) *before*
execution, ungated.

Worse for the processor: both assignments are guarded by
``if self.config.trace_include_sensitive_data:`` (``tool_execution.py:1818`` and
``:1854``). That flag defaults to the env var
``OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA`` (default ``"true"``,
``agents/run_config.py:52-55``) and is a single ``RunConfig`` field away from
``False`` — at which point ``span_data.input`` and ``.output`` stay ``None`` for
the whole span and the processor sees **no payload at all**. The hooks are
unaffected by that flag. Measured both ways.

**2. The processor carries the verdict; the hooks cannot.** ``RunHooksBase``
defines exactly ``on_llm_start/end``, ``on_agent_start/end``, ``on_handoff``,
``on_tool_start`` and ``on_tool_end`` — **there is no ``on_tool_error``**
(``agents/lifecycle.py``, full method list). A ``@function_tool`` that raises is
caught by ``failure_error_function`` (default
``agents/tool.py:1863 default_tool_error_function``), which converts the
exception into an ordinary string result and hands it to the model; the run does
not raise. ``on_tool_end`` therefore fires with ``result="An error occurred while
running the tool. …"`` — a plain ``str``, indistinguishable from a tool that
legitimately returned that text. Sniffing for that prefix would be a heuristic,
and a heuristic is not evidence. The span, by contrast, is authoritative:
``tool.py:2679`` attaches ``SpanError(message="Error running tool (non-fatal)",
data={"tool_name": …, "error": …})``, and ``span.error`` is a plain dict readable
at ``on_span_end``.

So: **hooks for argument fidelity, processor for verdict fidelity.** This module
ships both, each standalone and each honest about what it could not see, rather
than picking one and quietly under-reporting. The recon that scoped this work read
``on_tool_start``'s payload as "a complete capsule payload"; that is true of the
arguments and not of the outcome, and the difference is what the two shells encode.

  ===================  ==========================  =========================
  what                 ``…CapsuleHooks``           ``…CapsuleProcessor``
  ===================  ==========================  =========================
  args on *planned*    yes (pre-execution)         no — not yet assigned
  args at all          yes, ungated                only if sensitive data on
  outcome payload      yes                         only if sensitive data on
  error vs. success    **no** — no error hook      **yes** — ``span.error``
  pairing key          ``tool_call_id`` (exact)    ``span_id`` (exact)
  registration         ``Runner.run(hooks=…)``     ``add_trace_processor(…)``
  ===================  ==========================  =========================

Run both and you get two independent chains for the same call — deliberately not
auto-merged, because correlating them would mean matching on
``(tool_name, arguments)``, which is ambiguous for two concurrent identical calls
with different outcomes. A guessed correlation is not evidence either. The
constructor warns once when a second listener binds the same ledger path, so the
duplication is a decision rather than a surprise.

Concurrency
-----------
A model turn can emit several tool calls and the SDK runs them concurrently: the
probe above with three calls in one turn produced three ``span_start`` events
before any ``hook_start``, and all three ``hook_start`` events before any
``hook_end``. No FIFO pairing is safe. Both shells pair on an exact identity —
``span.span_id`` for the processor, ``ToolContext.tool_call_id`` for the hooks —
so interleaved calls chain to the right parent.

Observation only, deliberately
------------------------------
The SDK has in-path denial surfaces (``agents/tool_guardrails.py``, and the tool
approval flow reachable from ``ToolContext``). **This listener touches none of
them.** It reads events and writes nothing back. Deny belongs to the gate layer,
not to the evidence layer; see the ``observation_mode`` proposal in capsule-emit
#48. Every capsule sealed here is stamped ``observation_mode="event_stream"``
so no reader attributes an enforcement decision to it.

Never-raises
------------
``TracingProcessor``'s own contract asks for it ("Handle errors gracefully to
prevent disrupting agent execution", ``processor_interface.py``), and the hooks
are awaited on the tool path where an exception would abort the turn. Every
sealing path here is individually guarded: a broken ledger or anchor endpoint
warns and is skipped, and can never turn a working tool call into a failed one.
The tool's *own* exception, where one escapes, propagates unchanged.

Privacy: inputs and outputs are digested, never stored — the ledger carries
``agent_input_digest`` / ``agent_output_digest`` only. Floats in tool payloads are
canonicalized to RFC 8785 decimal strings by the ``_base`` funnel (capsule-emit
#135), so a ``{"qty": 2.5}`` argument seals and chains rather than failing closed.

All sealing logic lives in the framework-free :class:`OpenAIAgentsListenerCore`
(fully testable without the SDK installed); the two shells bind it to the SDK's
two extension points.
"""
from __future__ import annotations

import json
import warnings
from collections import OrderedDict
from typing import Any

from ._base import CapsuleEmitterBase

__all__ = [
    "OpenAIAgentsCapsuleHooks",
    "OpenAIAgentsCapsuleProcessor",
    "OpenAIAgentsListenerCore",
]

#: Stamped on an outcome capsule whose planned capsule could not be sealed, so
#: the missing chain link is self-describing in the ledger rather than silent.
UNCHAINED_REASON = (
    "planned capsule could not be sealed for this tool call; this outcome "
    "record has no parent and is not evidence of a committed plan"
)

#: Stamped on a processor planned capsule. The arguments are genuinely not
#: readable at on_span_start (tool_execution.py:1805 vs :1819) — this says so
#: rather than letting an empty agent_input pass for "the tool took no arguments".
ARGS_NOT_OBSERVABLE_NOTE = (
    "the OpenAI Agents SDK assigns FunctionSpanData.input after the span has "
    "started, so the tool arguments are not readable at on_span_start; this "
    "planned capsule commits to the tool identity, not to its arguments. Use "
    "OpenAIAgentsCapsuleHooks for a planned record that commits to the arguments."
)

#: Stamped when trace_include_sensitive_data is off and the span therefore
#: carried no payload. Absent is recorded as absent, never as empty.
PAYLOAD_WITHHELD_NOTE = (
    "FunctionSpanData.input/.output were None at on_span_end: this run set "
    "RunConfig.trace_include_sensitive_data=False (or "
    "OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=false), so the SDK never "
    "recorded the payload on the span. The payload is absent, not empty."
)

#: Stamped on every hooks-sealed outcome capsule. See the module docstring:
#: RunHooks has no on_tool_error, so this surface cannot certify success.
NO_ERROR_HOOK_NOTE = (
    "RunHooks exposes no on_tool_error; a tool whose exception was absorbed by "
    "failure_error_function returns an ordinary string here. This outcome "
    "records that the tool returned, not that it succeeded. The TracingProcessor "
    "surface reads span.error and can tell the difference."
)

#: Stamped when an outcome could not be sealed as "confirmed" because there was
#: neither an observed response nor a parent to chain to. §5.2 requires a
#: response_digest for a confirmed effect and the core derives it from the output
#: or, failing that, from the confirms target; with neither, "confirmed" would be
#: an unsupportable claim. "dispatched" is the reserved status for exactly that
#: state -- it went out, the outcome is unconfirmed -- and it keeps the record in
#: the ledger instead of dropping the only evidence the call ever happened.
UNCONFIRMABLE_NOTE = (
    "the tool returned no payload and this record has no parent capsule, so no "
    "response_digest could be derived; effect.status is 'dispatched' (outcome "
    "unconfirmed) rather than 'confirmed', which would claim more than was observed"
)

_LEDGERS_SEEN: set[str] = set()


def _warn_on_duplicate_ledger(ledger: Any) -> None:
    """Warn once when a second listener in this process binds the same ledger.

    Running the processor and the hooks together is legitimate and documented,
    but it produces two independent chains for every tool call. That should be a
    decision, not a discovery made later while reading the ledger.
    """
    try:
        key = str(ledger)
    except Exception:  # noqa: BLE001 — a warning is never worth an exception
        return
    if key in _LEDGERS_SEEN:
        warnings.warn(
            f"capsule-emit: a second OpenAI Agents listener is bound to ledger {key!r}. "
            "The TracingProcessor and RunHooks surfaces seal independently, so each "
            "tool call will produce two chains. This is supported (they observe "
            "different things — see the module docstring) but is rarely what you want; "
            "use one, or give them separate ledgers.",
            RuntimeWarning,
            stacklevel=3,
        )
        return
    _LEDGERS_SEEN.add(key)


def _decode_arguments(raw: Any) -> Any:
    """Best-effort decode of the SDK's JSON argument string into a mapping.

    ``ToolContext.tool_arguments`` and ``FunctionSpanData.input`` are both the
    raw JSON *string* the model produced. Decoding gives the digest a structured
    payload (and lets the ``_base`` funnel canonicalize floats inside it); a
    string that will not decode is committed verbatim, which is still exact
    evidence of what the model sent.
    """
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — malformed args are evidence too
        return raw


def _model_from_agent(agent: Any) -> dict[str, str] | None:
    """Pull ``{provider, model_id}`` off an Agents SDK ``Agent``, if it has one.

    Duck-typed on purpose: this module stays importable without the SDK.
    ``Agent.model`` is either a ``str`` model name or a ``Model`` instance
    (``agents/agent.py``); the provider is the model class's module leaf, and a
    plain string carries no provider at all. A model that answers neither is
    skipped rather than guessed at.
    """
    model = getattr(agent, "model", None)
    if model is None:
        return None
    if isinstance(model, str):
        return {"provider": "unknown", "model_id": model}
    model_id = getattr(model, "model", None) or getattr(model, "model_id", None)
    provider = type(model).__module__.rsplit(".", 1)[-1] or None
    if not model_id and not provider:
        return None
    return {
        "provider": str(provider or "unknown"),
        "model_id": str(model_id or provider),
    }


def _span_error_payload(error: Any) -> Any:
    """Project a ``SpanError`` TypedDict into something worth digesting."""
    if isinstance(error, dict):
        data = error.get("data")
        detail = data.get("error") if isinstance(data, dict) else None
        return {
            "message": error.get("message"),
            "error": detail if detail is not None else data,
        }
    return None if error is None else str(error)


class OpenAIAgentsListenerCore(CapsuleEmitterBase):
    """Framework-free core of the OpenAI Agents listener.

    :meth:`open_call` and :meth:`close_call` take plain values keyed by an opaque
    call key, so the whole behavior is exercised in tests without the SDK
    installed. The two shells route real events here.

    Sealing map (all through ``emit_capsule()``; capsule envelope invariant):

    - before the tool runs      → ``effect.status="planned"``
    - clean return             → ``effect.status="confirmed"``, chained
    - span error / exception   → ``verdict="errored"``, ``effect.status="failed"``, chained

    Args:
        max_pending: Bound on remembered planned-capsule ids (default 256), so a
            long run that sees opens without matching closes cannot grow the
            table without bound.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer,
            ledger, anchor, anchor_url, anchor_wait, model, max_results).
    """

    #: ``runtime`` stamped on every capsule from either shell.
    RUNTIME = "openai-agents"

    def __init__(self, *, max_pending: int = 256, **base_kw: Any) -> None:
        super().__init__(**base_kw)
        self._max_pending = max_pending
        # call key (span_id | tool_call_id) -> planned capsule id awaiting its outcome
        self._pending: OrderedDict[str, str] = OrderedDict()

    # -- helpers -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising (never break the agent run)."""
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: OpenAI Agents listener failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    @staticmethod
    def _compute(*notes: dict[str, Any] | None) -> dict[str, Any]:
        """Merge marker dicts, always stamping the observation mode."""
        merged: dict[str, Any] = {"observation_mode": "event_stream"}
        for note in notes:
            if note:
                merged.update(note)
        return merged

    def _remember(self, key: str | None, planned_id: str | None) -> None:
        if key is None or planned_id is None:
            return
        while len(self._pending) >= self._max_pending:
            self._pending.popitem(last=False)  # evict oldest
        self._pending[key] = planned_id

    # -- the records -------------------------------------------------------

    def open_call(
        self,
        tool_name: str,
        arguments: Any = None,
        *,
        key: str | None = None,
        args_observable: bool = True,
        model: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str | None:
        """Commitment record, sealed before the tool runs. Returns its id.

        ``args_observable=False`` means the surface genuinely could not read the
        arguments at this point (the processor's case); the capsule then carries
        no ``agent_input`` and says so via :data:`ARGS_NOT_OBSERVABLE_NOTE`,
        rather than letting an empty payload pass for "no arguments".
        """
        note = None if args_observable else {"args_observable": False,
                                             "args_note": ARGS_NOT_OBSERVABLE_NOTE}
        result = self._seal(
            action=tool_name,
            tool_input=arguments if args_observable else None,
            effect={"type": tool_name, "status": "planned"},
            action_type="fyi",
            runtime=self.RUNTIME,
            model=model,
            extra_compute=self._compute(note, extra),
        )
        planned_id = None if result is None else result.capsule_id
        self._remember(key, planned_id)
        return planned_id

    def close_call(
        self,
        tool_name: str,
        output: Any = None,
        *,
        key: str | None = None,
        planned_id: str | None = None,
        errored: bool = False,
        arguments: Any = None,
        model: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
        planned_dropped: bool = False,
    ) -> str | None:
        """Outcome record, chained to the planned capsule. Returns its id.

        ``planned_dropped`` marks the record as a known orphan (the planned seal
        was attempted and failed) rather than letting ``chain: null`` pass for an
        ordinary record — see :data:`UNCHAINED_REASON`.
        """
        if planned_id is None and key is not None:
            planned_id = self._pending.pop(key, None)
        orphan = {"unchained_reason": UNCHAINED_REASON} if planned_dropped else None

        # §5.2: a "confirmed" effect REQUIRES a response_digest, which the core
        # derives from agent_output or, failing that, from the confirms target.
        # With neither there is nothing to derive it from, and asking for
        # "confirmed" anyway makes the seal raise -- which would drop the record
        # entirely and lose the only evidence the call happened. Degrade the
        # claim, not the evidence.
        unconfirmable = not errored and output is None and planned_id is None
        if errored:
            status = "failed"
        elif unconfirmable:
            status = "dispatched"
        else:
            status = "confirmed"
        limit = {"outcome_unconfirmable": True, "outcome_note": UNCONFIRMABLE_NOTE} \
            if unconfirmable else None

        result = self._seal(
            action=tool_name,
            tool_input=arguments,
            tool_output=output,
            verdict="errored" if errored else "executed",
            effect={"type": tool_name, "status": status},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime=self.RUNTIME,
            model=model,
            extra_compute=self._compute(orphan, limit, extra),
        )
        return None if result is None else result.capsule_id

    def forget(self, key: str | None) -> str | None:
        """Drop and return a pending planned id (used when a span never closes)."""
        if key is None:
            return None
        return self._pending.pop(key, None)

    @property
    def pending(self) -> int:
        """How many opened calls are still awaiting an outcome."""
        return len(self._pending)


# The SDK's TracingProcessor is a real ABC and RunHooks a real (generic) class, so
# unlike the Strands HookProvider Protocol there is nothing structural to satisfy.
# Importing them at module scope would make this module unimportable without the
# SDK — and the whole sealing core is meant to be testable without it. Falling
# back to `object` keeps both classes defined either way; when the SDK *is*
# installed the shells are genuine subclasses, so `isinstance(x, TracingProcessor)`
# holds and `add_trace_processor()` gets exactly what its signature asks for.
try:  # pragma: no cover - both branches exercised, but only one per environment
    from agents.tracing import TracingProcessor as _TracingProcessorBase
except Exception:  # noqa: BLE001
    _TracingProcessorBase = object  # type: ignore[assignment, misc]

try:  # pragma: no cover
    from agents.lifecycle import RunHooksBase as _RunHooksBase
except Exception:  # noqa: BLE001
    _RunHooksBase = object  # type: ignore[assignment, misc]


class OpenAIAgentsCapsuleProcessor(_TracingProcessorBase):  # type: ignore[misc, valid-type]
    """A ``TracingProcessor`` that seals one capsule per tool-call span boundary.

    Register it globally — no per-run wiring, no fork::

        from agents.tracing import add_trace_processor
        processor = OpenAIAgentsCapsuleProcessor(operator="acme-co", developer="my-agent@v1")
        add_trace_processor(processor)

    ``add_trace_processor`` is additive (it keeps the SDK's own exporter);
    ``set_trace_processors([processor])`` replaces the list. Both are exported
    from ``agents.tracing`` and from the top-level ``agents`` package
    (``agents/__init__.py:234``, ``:248``).

    Only ``FunctionSpanData`` spans are consumed — agent, turn, task, generation
    and response spans are ignored. Filtering is on the public
    ``span.span_data.type == "function"`` property rather than ``isinstance``, so
    the core stays import-free.

    This is the surface that can tell a failed tool call from a successful one
    (``span.error``), and the surface that cannot commit to the arguments in the
    planned record. See the module docstring for the measured reason.

    Accepts the same configuration as :class:`OpenAIAgentsListenerCore` and
    exposes the core as :attr:`core` (``processor.core.last`` / ``.results``).
    """

    #: The span type this processor consumes (``FunctionSpanData.type``).
    SPAN_TYPE = "function"

    def __init__(self, **core_kw: Any) -> None:
        _warn_on_duplicate_ledger(core_kw.get("ledger", "ledger.jsonl"))
        self.core = OpenAIAgentsListenerCore(**core_kw)

    # -- TracingProcessor interface ---------------------------------------

    def on_trace_start(self, trace: Any) -> None:
        """Traces are not sealed — a capsule is a tool call, not a workflow."""

    def on_trace_end(self, trace: Any) -> None:
        """Traces are not sealed — a capsule is a tool call, not a workflow."""

    def on_span_start(self, span: Any) -> None:
        """Function span opened → planned capsule (the commitment record).

        The arguments are not on the span yet (``tool_execution.py:1819`` runs
        after the span has started), so the planned record commits to the tool
        identity and says so. Reads ``span`` and writes nothing back.
        """
        data = self._function_span_data(span)
        if data is None:
            return
        self.core.open_call(
            self._tool_name(data),
            key=self._key(span),
            args_observable=False,
        )

    def on_span_end(self, span: Any) -> None:
        """Function span closed → confirmed / failed capsule, chained.

        ``span.error`` is authoritative for the verdict. ``span_data.input`` and
        ``.output`` are populated here *if* the run allowed sensitive data; when
        they are not, the capsule records the absence explicitly.
        """
        data = self._function_span_data(span)
        if data is None:
            return
        key = self._key(span)
        planned_id = self.core.forget(key)
        error = getattr(span, "error", None)
        raw_input = getattr(data, "input", None)
        raw_output = getattr(data, "output", None)
        withheld = raw_input is None and raw_output is None
        note: dict[str, Any] = {}
        if withheld:
            note["payload_withheld"] = True
            note["payload_note"] = PAYLOAD_WITHHELD_NOTE
        mcp_data = getattr(data, "mcp_data", None)
        if mcp_data:
            note["mcp_data"] = mcp_data
        self.core.close_call(
            self._tool_name(data),
            output=_span_error_payload(error) if error is not None else raw_output,
            planned_id=planned_id,
            errored=error is not None,
            arguments=_decode_arguments(raw_input),
            extra=note or None,
        )

    def shutdown(self) -> None:
        """Nothing to flush — every capsule is sealed synchronously on the event."""

    def force_flush(self) -> None:
        """Nothing to flush — every capsule is sealed synchronously on the event."""

    # -- helpers -----------------------------------------------------------

    @classmethod
    def _function_span_data(cls, span: Any) -> Any | None:
        """The span's data if this is a function span, else None (never raises)."""
        try:
            data = span.span_data
            if getattr(data, "type", None) != cls.SPAN_TYPE:
                return None
            return data
        except Exception:  # noqa: BLE001 — a foreign span is not worth an exception
            return None

    @staticmethod
    def _tool_name(data: Any) -> str:
        return str(getattr(data, "name", None) or "tool")

    @staticmethod
    def _key(span: Any) -> str | None:
        raw = getattr(span, "span_id", None)
        return None if raw is None else str(raw)

    # convenience passthroughs, mirroring the sibling listeners
    @property
    def last(self) -> Any:
        """The most recent EmitResult, or None."""
        return self.core.last

    @property
    def results(self) -> list[Any]:
        """All EmitResults sealed this session."""
        return self.core.results


class OpenAIAgentsCapsuleHooks(_RunHooksBase):  # type: ignore[misc, valid-type]
    """``RunHooks`` that seal one capsule per tool call, with the arguments.

    Pass per run — a documented ``Runner.run`` kwarg, no fork::

        hooks = OpenAIAgentsCapsuleHooks(operator="acme-co", developer="my-agent@v1")
        result = await Runner.run(agent, "...", hooks=hooks)

    This is the surface that commits to the arguments *before* the tool runs
    (``ToolContext.tool_arguments`` at ``on_tool_start``) and the surface that
    cannot distinguish an errored tool from a successful one — there is no
    ``on_tool_error`` in ``RunHooksBase``, and ``failure_error_function`` turns a
    raising tool into an ordinary string result. Every outcome capsule sealed
    here therefore carries :data:`NO_ERROR_HOOK_NOTE`: it records that the tool
    *returned*, not that it succeeded. Pair with
    :class:`OpenAIAgentsCapsuleProcessor` if you need the verdict.

    Only the two tool callbacks are overridden; every other ``RunHooksBase``
    method keeps its inherited no-op body, so a future SDK release that adds
    hooks does not break this class.

    Accepts the same configuration as :class:`OpenAIAgentsListenerCore` and
    exposes the core as :attr:`core`.
    """

    def __init__(self, **core_kw: Any) -> None:
        _warn_on_duplicate_ledger(core_kw.get("ledger", "ledger.jsonl"))
        self.core = OpenAIAgentsListenerCore(**core_kw)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        """``ToolContext`` → planned capsule carrying the arguments.

        ``context`` is only *typically* a ``ToolContext``; other local tool
        families hand over a plain ``RunContextWrapper``
        (``agents/lifecycle.py:76-82``). Everything is therefore read
        defensively, with the tool object as the fallback for the name.
        """
        name = self._tool_name(context, tool)
        self.core.open_call(
            name,
            _decode_arguments(getattr(context, "tool_arguments", None)),
            key=self._key(context),
            args_observable=True,
            model=_model_from_agent(agent),
        )

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: object) -> None:
        """Tool returned → outcome capsule, chained to the planned one.

        Sealed as ``confirmed``/``executed`` because that is all this surface can
        honestly observe; :data:`NO_ERROR_HOOK_NOTE` records the limit on the
        capsule itself.
        """
        name = self._tool_name(context, tool)
        self.core.close_call(
            name,
            output=result,
            key=self._key(context),
            errored=False,
            model=_model_from_agent(agent),
            extra={"verdict_note": NO_ERROR_HOOK_NOTE},
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _tool_name(context: Any, tool: Any) -> str:
        return str(
            getattr(context, "tool_name", None) or getattr(tool, "name", None) or "tool"
        )

    @staticmethod
    def _key(context: Any) -> str | None:
        raw = getattr(context, "tool_call_id", None)
        return None if raw is None else str(raw)

    # convenience passthroughs, mirroring the sibling listeners
    @property
    def last(self) -> Any:
        """The most recent EmitResult, or None."""
        return self.core.last

    @property
    def results(self) -> list[Any]:
        """All EmitResults sealed this session."""
        return self.core.results
