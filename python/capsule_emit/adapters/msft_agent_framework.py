# SPDX-License-Identifier: Apache-2.0
"""Microsoft Agent Framework middleware — planned/confirmed capsules per tool call and per run.

    from agent_framework import Agent
    from capsule_emit.adapters.msft_agent_framework import capsule_middleware

    agent = Agent(client, "you are helpful", tools=[...],
                  middleware=capsule_middleware(operator="acme-co", developer="my-agent@v1"))

Requires ``pip install "capsule-emit[msft-agent-framework]"`` (alias:
``capsule-emit[agent-framework]``).

Fifth sibling in the listener family (``crewai_listener`` / ``langchain_listener`` /
``agno_listener`` / ``strands_listener``), with the same core/shell split and the same
two-record chain:

- before the work runs → **planned** capsule (the commitment record)
- after it returns     → **confirmed** capsule, ``confirms``-chained to the planned one
- it raised            → ``verdict="errored"``, ``effect.status="failed"``, chained
- another middleware terminated or failed the call → ``verdict="blocked"``, effect left
  ``"planned"``, chained — a refusal that took effect is evidence, not silence

Registration is Agent Framework's own public extension point and needs no fork
--------------------------------------------------------------------------------
Verified against the released ``agent-framework-core==1.16.0`` wheel (the extra pins
it) — file/line references below are wheel paths under ``agent_framework/``, not the
GitHub tree:

- ``Agent.__init__(client, ..., middleware=...)`` — ``_agents.py:1885``, the ``middleware``
  keyword at ``:1896``; and per-run ``Agent.run(..., middleware=...)`` — ``_agents.py:1852``,
  the keyword at ``:1858``. Both accept a single middleware or a sequence
  (``_middleware.py:1690 _as_middleware_list``).
- ``categorize_middleware`` (``_middleware.py:1708``) routes each object by ``isinstance``:
  ``AgentMiddleware`` → agent seam, ``FunctionMiddleware`` → function seam,
  ``ChatMiddleware`` → chat seam. **The isinstance order is agent-first**, so one object
  must not inherit two of the ABCs — it would be silently categorized as agent-only.
  That is why this module ships two objects, not one.
- ``AgentMiddleware`` / ``FunctionMiddleware`` are abstract base classes with a single
  ``async def process(self, context, call_next)`` — ``_middleware.py:535`` and ``:594``.
  Both are public exports of ``agent_framework`` (``__init__.py:399``, ``:479``).

What each seam sees
-------------------
- ``FunctionInvocationContext`` (``_middleware.py:270``): ``function`` (a ``FunctionTool``),
  ``arguments`` (a ``BaseModel`` or ``Mapping``), ``session``, ``metadata``, ``result``,
  ``kwargs``, ``tools``. After ``await call_next()`` the ``result`` is the value the
  *final handler* produced — in the function-calling loop that is
  ``FunctionTool.invoke(...)``, which returns a **list of ``Content``**, not the tool's
  bare return value. (Observed, not documented: a ``def add(a, b) -> int`` returning ``5``
  arrives as ``[Content(type="text", text="5")]``.)
- ``AgentContext`` (``_middleware.py:154``): ``agent``, ``messages``, ``session``,
  ``options``, ``stream``, ``metadata``, ``result`` (an ``AgentResponse``, or a
  ``ResponseStream`` when ``stream=True``).

Observation only, deliberately
------------------------------
This surface is ``in_path``: a middleware can substitute ``context.result``, mutate the
live tool list via ``FunctionInvocationContext.add_tools`` /``remove_tools``
(``_middleware.py:354``/``:391``), raise ``MiddlewareTermination`` to stop the loop
gracefully or ``MiddlewareFailure`` to abort it fail-closed. **This middleware does none
of those.** It reads the context, seals, and always calls ``call_next()``; it never writes
``context.result``, never touches the tool list, and never originates a control-flow
exception. Deny belongs to the gate layer, not to the evidence layer; see the
``observation_mode`` proposal in capsule-emit #48. Capsules from this adapter are stamped
``observation_mode="in_path_wrapper"`` so a reader knows the seam *could* have intervened
and this component chose not to.

Every exception that comes back out of ``call_next()`` is re-raised unchanged. That is
load-bearing for ``MiddlewareFailure`` in particular: the framework's own docstring says
"Middleware must not catch ``MiddlewareFailure`` (let it propagate through
``call_next()``): swallowing it converts a fail-closed abort back into a running — and
possibly unguarded — loop" (``_middleware.py:116``).

Never-raises, and why it is load-bearing here
---------------------------------------------
The two seams punish a raising middleware differently, and both punishments are silent:

- **Function seam.** An ordinary exception from function middleware is *absorbed* into a
  tool-error result and the function-calling loop keeps going
  (``_tools.py:1640-1641`` ``except Exception as exc: return _function_execution_error_result(...)``).
  A careless evidence layer therefore turns a working tool call into a tool error that the
  model reads and acts on — the agent misbehaves and nothing surfaces as a crash.
- **Agent seam.** ``AgentMiddlewarePipeline.execute`` suppresses only
  ``MiddlewareTermination`` (``_middleware.py:1080``); every other exception propagates out
  of ``Agent.run`` and fails the whole run.

So every sealing path here is individually guarded: a broken ledger or anchor endpoint
warns and is skipped, and can never affect the agent.

The blocked case, and what this seam honestly cannot see
--------------------------------------------------------
When a *downstream* middleware raises ``MiddlewareTermination`` or ``MiddlewareFailure``,
it surfaces here as an exception out of ``call_next()``. The refusal is somebody else's,
and it took effect, so it is sealed: ``verdict="blocked"``, ``effect.status`` left at
``"planned"``.

``"planned"`` is deliberate and conservative. ``MiddlewareTermination`` can be raised
either *before* a downstream middleware calls ``call_next()`` (a cache hit, a policy deny —
the tool body never ran) or *after* it (stop the loop, but this tool did run). **From this
seam the two are indistinguishable**, and the reserved effect-status set is
planned/dispatched/confirmed/failed/reverted (§5.2) — an unknown status derives
``effect_mode="dispatched_unconfirmed"``, which would claim a dispatch that may not have
happened. Recording ``"planned"`` under-claims rather than over-claims, and the compute
attestation carries ``agent_framework_effect_unobservable=True`` plus
``agent_framework_result_present`` (whether ``context.result`` was set by the time the
exception reached us) so a reader can see exactly what was and was not observed.

Ordering matters and is the caller's to get right: middleware earlier in the list wraps
middleware later in it. A guard placed *before* this middleware denies a call that this
middleware never sees at all — and therefore never records. Put the capsule middleware
first if you want its refusals on the record.

Model capture
-------------
``AgentContext`` carries the agent, and ``RawAgent`` stores its client at
``self.client`` (``_agents.py:885``) with the model id read from ``client.model``
(``_agents.py:902``); the provider name is the client's own OTel identifier
``BaseChatClient.OTEL_PROVIDER_NAME`` (``_clients.py:271``). ``FunctionInvocationContext``
carries **no** agent, so the run middleware publishes what it captured into a
:class:`contextvars.ContextVar` for the duration of ``call_next()``. Tool calls run inside
the run's task tree and inherit that context, so a process running two agents on different
models concurrently still attributes each tool call correctly. Without the run middleware
installed, function capsules fall back to the ``model=`` passed at construction (or none) —
never to a guess.

Privacy: inputs and outputs are digested, never stored — the ledger carries
``agent_input_digest`` / ``agent_output_digest`` only. Raw floats in tool payloads fail
closed at the digest layer (``FloatInDigestError`` → warning, no capsule, run unaffected).

All sealing logic lives in the framework-free :class:`AgentFrameworkCore` (fully testable
without agent-framework installed); :class:`CapsuleFunctionMiddleware` and
:class:`CapsuleRunMiddleware` are the thin shells that bind it to the two seams.
"""
from __future__ import annotations

import contextvars
import warnings
from typing import Any

from ._base import CapsuleEmitterBase

__all__ = [
    "AgentFrameworkCore",
    "CapsuleFunctionMiddleware",
    "CapsuleRunMiddleware",
    "capsule_middleware",
]

RUNTIME = "msft-agent-framework"

#: Published by :class:`CapsuleRunMiddleware` for the duration of one agent run so the
#: function seam — which has no handle on the agent — can attribute the right model.
_RUN_MODEL: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "capsule_emit_af_run_model", default=None
)

_TERMINATION_NOTE = (
    "another middleware raised MiddlewareTermination/MiddlewareFailure through this "
    "wrapper; the refusal took effect, but whether the wrapped body already executed "
    "is not observable from the middleware seam"
)


def _model_from_agent(agent: Any) -> dict[str, str] | None:
    """Pull ``{provider, model_id}`` off an Agent Framework agent, if it has a client.

    Duck-typed on purpose: this module stays importable without agent-framework.
    ``RawAgent`` stores the chat client at ``self.client`` (``_agents.py:885``) and reads
    the model id off ``client.model`` (``_agents.py:902``). The provider is the client's
    own OTel identifier, ``OTEL_PROVIDER_NAME`` (``_clients.py:271``), which defaults to
    ``"unknown"`` on the base class — a client that answers neither is skipped rather
    than guessed at.
    """
    client = getattr(agent, "client", None)
    if client is None:
        return None
    model_id = getattr(client, "model", None)
    provider = getattr(type(client), "OTEL_PROVIDER_NAME", None)
    if isinstance(provider, str) and provider == "unknown":
        provider = None
    if not model_id and not provider:
        return None
    return {
        "provider": str(provider or "unknown"),
        "model_id": str(model_id or provider),
    }


def _jsonable(value: Any, *, _depth: int = 0) -> Any:
    """Project an Agent Framework payload into something the JCS digest can canonicalize.

    Handles the three shapes this surface actually hands us:

    - ``Content`` / ``Message`` / any framework object exposing ``to_dict()``
      (``_types.py``; ``Content.to_dict()`` yields ``{"type": ..., ...}``),
    - pydantic models (``FunctionInvocationContext.arguments`` may be a ``BaseModel``,
      ``_middleware.py:323``) via ``model_dump(mode="json")``,
    - raw ``bytes``, which no canonical JSON encoding accepts, replaced by
      ``"<omitted:N bytes>"``.

    Anything already JSON-native is returned untouched and left to the digest layer to
    accept or reject. Recursion is depth-capped so a self-referential
    ``raw_representation`` cannot hang a sealing call.
    """
    if _depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # Deliberately passed through: the digest layer fails closed on raw floats
        # (FloatInDigestError) and that failure is the intended signal, not something
        # this projection should paper over.
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<omitted:{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): _jsonable(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, _depth=_depth + 1) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(mode="json"), _depth=_depth + 1)
        except Exception:  # noqa: BLE001 — a model that will not dump is not worth an exception
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict(), _depth=_depth + 1)
        except Exception:  # noqa: BLE001 — same
            pass
    return str(value)


def _messages_payload(messages: Any) -> Any:
    """Project ``AgentContext.messages`` into ``[{role, text}]``.

    ``Message.to_dict()`` carries the full content-block tree including
    ``raw_representation`` passthrough; the digest only needs a stable projection of what
    was actually said, so this takes ``role`` + ``text`` (``_types.py`` ``Message.text``
    concatenates the text contents). A message that answers neither is projected whole.
    """
    if not isinstance(messages, (list, tuple)):
        return _jsonable(messages)
    out: list[Any] = []
    for message in messages:
        role = getattr(message, "role", None)
        text = getattr(message, "text", None)
        if role is None and text is None:
            out.append(_jsonable(message))
            continue
        out.append({"role": str(role), "text": "" if text is None else str(text)})
    return out


class AgentFrameworkCore(CapsuleEmitterBase):
    """Framework-free core of the Agent Framework middleware.

    Every method takes duck-typed context objects, so the whole behavior is exercised in
    tests without agent-framework installed. :class:`CapsuleFunctionMiddleware` and
    :class:`CapsuleRunMiddleware` route real contexts here.

    Sealing map (all through ``emit_capsule()``; capsule envelope invariant):

    - before the wrapped body   → ``effect.status="planned"``
    - clean return              → ``effect.status="confirmed"``, chained
    - exception                 → ``verdict="errored"``, ``effect.status="failed"``, chained
    - termination / failure     → ``verdict="blocked"``, effect stays ``"planned"``, chained

    Args:
        seal_runs: Seal the run-level pair from the agent seam (default True). Set False
            to record tool calls only.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer, ledger,
            anchor, anchor_url, anchor_wait, model, max_results).
    """

    def __init__(self, *, seal_runs: bool = True, **base_kw: Any) -> None:
        super().__init__(**base_kw)
        self.seal_runs = seal_runs

    # -- helpers -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising (never break the agent)."""
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: Agent Framework middleware failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    @staticmethod
    def _capsule_id(result: Any) -> str | None:
        return None if result is None else getattr(result, "capsule_id", None)

    @staticmethod
    def _marker(**extra: Any) -> dict[str, Any]:
        marker: dict[str, Any] = {"observation_mode": "in_path_wrapper"}
        marker.update({k: v for k, v in extra.items() if v is not None})
        return marker

    # -- function seam -----------------------------------------------------

    @staticmethod
    def _function_name(context: Any) -> str:
        function = getattr(context, "function", None)
        name = getattr(function, "name", None)
        return str(name) if name else "function"

    def _function_model(self) -> dict[str, str] | None:
        """Model for a tool-call capsule: the running agent's, else the configured one."""
        return _RUN_MODEL.get()

    def function_planned(self, context: Any) -> str | None:
        """Seal the commitment record for one tool call; return its capsule id."""
        name = self._function_name(context)
        result = self._seal(
            action=name,
            tool_input=_jsonable(getattr(context, "arguments", None)),
            effect={"type": name, "status": "planned"},
            action_type="fyi",
            runtime=RUNTIME,
            model=self._function_model(),
            extra_compute=self._marker(agent_framework_seam="function"),
        )
        return self._capsule_id(result)

    def function_confirmed(self, context: Any, planned_id: str | None) -> None:
        """Seal the outcome record for a tool call that returned cleanly."""
        name = self._function_name(context)
        self._seal(
            action=name,
            tool_output=_jsonable(getattr(context, "result", None)),
            effect={"type": name, "status": "confirmed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime=RUNTIME,
            model=self._function_model(),
            extra_compute=self._marker(agent_framework_seam="function"),
        )

    def function_failed(self, context: Any, planned_id: str | None, exc: BaseException) -> None:
        """Seal the outcome record for a tool call that raised."""
        name = self._function_name(context)
        self._seal(
            action=name,
            tool_output={"error": f"{type(exc).__name__}: {exc}"},
            verdict="errored",
            effect={"type": name, "status": "failed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime=RUNTIME,
            model=self._function_model(),
            extra_compute=self._marker(agent_framework_seam="function"),
        )

    def function_blocked(self, context: Any, planned_id: str | None, exc: BaseException) -> None:
        """Seal a refusal that another middleware imposed on this tool call.

        The refusal is SOMEBODY ELSE'S — this middleware never raises
        ``MiddlewareTermination``/``MiddlewareFailure``. The markers say so, so no reader
        attributes the deny to us, and ``agent_framework_effect_unobservable`` records
        that the effect status is a floor, not a measurement.
        """
        name = self._function_name(context)
        self._seal(
            action=name,
            tool_output={"blocked_by": type(exc).__name__, "detail": str(exc)},
            verdict="blocked",
            effect={"type": name, "status": "planned"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime=RUNTIME,
            model=self._function_model(),
            extra_compute=self._marker(
                agent_framework_seam="function",
                agent_framework_blocked_by=type(exc).__name__,
                agent_framework_effect_unobservable=True,
                agent_framework_result_present=getattr(context, "result", None) is not None,
                agent_framework_block_note=_TERMINATION_NOTE,
            ),
        )

    # -- agent (run) seam --------------------------------------------------

    @staticmethod
    def _run_action(context: Any) -> str:
        agent = getattr(context, "agent", None)
        name = getattr(agent, "name", None)
        return f"{name}.run" if name else "agent.run"

    def run_planned(self, context: Any) -> tuple[str | None, dict[str, str] | None]:
        """Seal the commitment record for one agent run; return ``(id, model)``."""
        action = self._run_action(context)
        model = _model_from_agent(getattr(context, "agent", None))
        result = self._seal(
            action=action,
            tool_input=_messages_payload(getattr(context, "messages", None)),
            effect={"type": action, "status": "planned"},
            action_type="fyi",
            runtime=RUNTIME,
            model=model,
            extra_compute=self._marker(
                agent_framework_seam="agent",
                agent_framework_stream=bool(getattr(context, "stream", False)),
            ),
        )
        return self._capsule_id(result), model

    def run_confirmed(self, context: Any, planned_id: str | None, model: dict[str, str] | None) -> None:
        """Seal the outcome record for a run that completed.

        On a streaming run ``context.result`` is a ``ResponseStream``, not a finished
        ``AgentResponse`` — the updates have not been consumed yet at the moment the
        pipeline unwinds. The projection records what is actually observable (the
        object's own ``to_dict``/``str``) and the capsule is stamped
        ``agent_framework_stream=True`` so a reader does not read it as a finished
        transcript.
        """
        action = self._run_action(context)
        response = getattr(context, "result", None)
        text = getattr(response, "text", None)
        payload: Any = {"text": str(text)} if isinstance(text, str) else _jsonable(response)
        self._seal(
            action=action,
            tool_output=payload,
            effect={"type": action, "status": "confirmed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime=RUNTIME,
            model=model,
            extra_compute=self._marker(
                agent_framework_seam="agent",
                agent_framework_stream=bool(getattr(context, "stream", False)),
            ),
        )

    def run_failed(
        self, context: Any, planned_id: str | None, model: dict[str, str] | None, exc: BaseException
    ) -> None:
        """Seal the outcome record for a run that raised."""
        action = self._run_action(context)
        self._seal(
            action=action,
            tool_output={"error": f"{type(exc).__name__}: {exc}"},
            verdict="errored",
            effect={"type": action, "status": "failed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime=RUNTIME,
            model=model,
            extra_compute=self._marker(agent_framework_seam="agent"),
        )

    def run_blocked(
        self, context: Any, planned_id: str | None, model: dict[str, str] | None, exc: BaseException
    ) -> None:
        """Seal a refusal another middleware imposed on this run."""
        action = self._run_action(context)
        self._seal(
            action=action,
            tool_output={"blocked_by": type(exc).__name__, "detail": str(exc)},
            verdict="blocked",
            effect={"type": action, "status": "planned"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime=RUNTIME,
            model=model,
            extra_compute=self._marker(
                agent_framework_seam="agent",
                agent_framework_blocked_by=type(exc).__name__,
                agent_framework_effect_unobservable=True,
                agent_framework_result_present=getattr(context, "result", None) is not None,
                agent_framework_block_note=_TERMINATION_NOTE,
            ),
        )


try:
    from agent_framework import AgentMiddleware as _AgentBase
    from agent_framework import FunctionMiddleware as _FunctionBase

    _HAVE_AGENT_FRAMEWORK = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _AgentBase = object  # type: ignore[assignment,misc]
    _FunctionBase = object  # type: ignore[assignment,misc]
    _HAVE_AGENT_FRAMEWORK = False


def _require_sdk(what: str) -> None:
    if not _HAVE_AGENT_FRAMEWORK:
        raise ImportError(
            f"{what} needs the Microsoft Agent Framework. "
            'Install with: pip install "capsule-emit[msft-agent-framework]"'
        )


class CapsuleFunctionMiddleware(_FunctionBase):  # type: ignore[valid-type,misc]
    """``FunctionMiddleware`` shell: one planned + one outcome capsule per tool call.

    Register through the public constructor kwarg — no fork, no monkeypatch::

        agent = Agent(client, tools=[...], middleware=[CapsuleFunctionMiddleware(core)])

    or per run: ``await agent.run(prompt, middleware=[CapsuleFunctionMiddleware(core)])``.

    Takes either an existing :class:`AgentFrameworkCore` (so the run middleware and this
    one share a ledger and a ``results`` history) or the core's keyword arguments.
    """

    def __init__(self, core: AgentFrameworkCore | None = None, /, **core_kw: Any) -> None:
        _require_sdk(type(self).__name__)
        if core is not None and core_kw:
            raise TypeError("pass either an AgentFrameworkCore or its keyword arguments, not both")
        self.core = core if core is not None else AgentFrameworkCore(**core_kw)

    async def process(self, context: Any, call_next: Any) -> None:
        """Seal around one tool call, then hand every outcome back unchanged."""
        planned_id = self.core.function_planned(context)
        try:
            await call_next()
        except BaseException as exc:
            if _is_control_flow(exc):
                self.core.function_blocked(context, planned_id, exc)
            else:
                self.core.function_failed(context, planned_id, exc)
            raise
        self.core.function_confirmed(context, planned_id)

    @property
    def last(self) -> Any:
        """The most recent EmitResult, or None."""
        return self.core.last

    @property
    def results(self) -> list[Any]:
        """All EmitResults sealed this session."""
        return self.core.results


class CapsuleRunMiddleware(_AgentBase):  # type: ignore[valid-type,misc]
    """``AgentMiddleware`` shell: one planned + one outcome capsule per agent run.

    Also publishes the run's ``{provider, model_id}`` into a :class:`contextvars.ContextVar`
    for the duration of the run, which is how :class:`CapsuleFunctionMiddleware` learns the
    model (``FunctionInvocationContext`` carries no agent). The token is reset in a
    ``finally``, so a raising run cannot leak the model into the next one.
    """

    def __init__(self, core: AgentFrameworkCore | None = None, /, **core_kw: Any) -> None:
        _require_sdk(type(self).__name__)
        if core is not None and core_kw:
            raise TypeError("pass either an AgentFrameworkCore or its keyword arguments, not both")
        self.core = core if core is not None else AgentFrameworkCore(**core_kw)

    async def process(self, context: Any, call_next: Any) -> None:
        """Seal around one agent run, then hand every outcome back unchanged."""
        if not self.core.seal_runs:
            model = _model_from_agent(getattr(context, "agent", None))
            token = _RUN_MODEL.set(model)
            try:
                await call_next()
            finally:
                _RUN_MODEL.reset(token)
            return

        planned_id, model = self.core.run_planned(context)
        token = _RUN_MODEL.set(model)
        try:
            await call_next()
        except BaseException as exc:
            if _is_control_flow(exc):
                self.core.run_blocked(context, planned_id, model, exc)
            else:
                self.core.run_failed(context, planned_id, model, exc)
            raise
        finally:
            _RUN_MODEL.reset(token)
        self.core.run_confirmed(context, planned_id, model)

    @property
    def last(self) -> Any:
        """The most recent EmitResult, or None."""
        return self.core.last

    @property
    def results(self) -> list[Any]:
        """All EmitResults sealed this session."""
        return self.core.results


def _is_control_flow(exc: BaseException) -> bool:
    """Is this a deliberate middleware refusal rather than a failure?

    Matched by class name rather than ``isinstance`` so the framework-free core stays
    testable without agent-framework installed, and so a future sibling signal in the
    same family is not silently misfiled as an error.
    """
    return any(
        cls.__name__ in ("MiddlewareTermination", "MiddlewareFailure")
        for cls in type(exc).__mro__
    )


def capsule_middleware(
    core: AgentFrameworkCore | None = None, /, **core_kw: Any
) -> list[Any]:
    """Both middlewares over one shared core, ordered for ``Agent(middleware=...)``.

        agent = Agent(client, tools=[...],
                      middleware=capsule_middleware(operator="acme-co", developer="a@v1"))

    Returns ``[CapsuleRunMiddleware, CapsuleFunctionMiddleware]``. The order in the list
    is what puts the capsule layer outermost at each seam, so a refusal raised by any
    middleware registered after it is on the record. ``categorize_middleware``
    (``_middleware.py:1708``) routes the two objects to their own seams by ``isinstance``,
    so a single list is all the caller has to pass.
    """
    _require_sdk("capsule_middleware")
    shared = core if core is not None else AgentFrameworkCore(**core_kw)
    return [CapsuleRunMiddleware(shared), CapsuleFunctionMiddleware(shared)]
