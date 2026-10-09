# SPDX-License-Identifier: Apache-2.0
"""Strands Agents hook listener — planned/confirmed/failed capsules per tool call.

    from strands import Agent
    from capsule_emit.adapters.strands_listener import StrandsCapsuleListener

    listener = StrandsCapsuleListener(operator="acme-co", developer="my-agent@v1")
    agent = Agent(model=..., tools=[...], hooks=[listener])

Requires ``pip install "capsule-emit[strands]"``.

Fourth sibling in the listener family (``crewai_listener`` / ``langchain_listener``
/ ``agno_listener``), with the same core/shell split and the same two-record chain:

- before the tool runs → **planned** capsule (the commitment record)
- after it returns     → **confirmed** capsule, ``confirms``-chained to the
  planned one (response digest auto-derived)
- tool raised / errored → ``verdict="errored"``, ``effect.status="failed"``, chained
- cancelled by another hook → ``verdict="blocked"``, effect left ``"planned"``,
  chained — a refusal that took effect is evidence, not silence

Registration is via Strands' own public extension point and needs **no fork**:
``Agent(hooks=[...])`` is a documented constructor kwarg (``strands/agent/agent.py:223``
in the 1.54.0 wheel), consumed into ``self.hooks = HookRegistry()`` (``:469``) and
dispatched with ``isinstance(hook, HookProvider)`` (``:536``). ``HookProvider`` is a
``@runtime_checkable`` Protocol (``strands/hooks/registry.py:114``), so that isinstance
check is *structural* — it tests for a ``register_hooks`` attribute and nothing else.
That is why this module imports strands lazily, inside :meth:`register_hooks`: the
listener is importable, and its whole sealing core is testable, without strands
installed.

What the events give us
-----------------------
Verified against the released ``strands-agents==1.54.0`` wheel (the extra pins it):

- ``BeforeToolCallEvent`` — ``strands/hooks/events.py:208``. Fields
  ``selected_tool``, ``tool_use``, ``invocation_state``, ``cancel_tool``.
- ``AfterToolCallEvent`` — ``strands/hooks/events.py:248``. Fields
  ``selected_tool``, ``tool_use``, ``invocation_state``, ``result``,
  ``exception``, ``cancel_message``, ``duration``, ``retry``.

These are typed dataclasses with the tool payload already destructured, so there is
much less shape-guessing here than the CrewAI event bus required. ``tool_use`` is a
``ToolUse`` TypedDict (``strands/types/tools.py:65``) carrying ``name``, ``toolUseId``
and ``input``; ``result`` is a ``ToolResult`` TypedDict (``:102``) carrying ``status``,
``toolUseId`` and a list of content blocks (``:82``).

Pairing uses ``tool_use["toolUseId"]`` — exact, no FIFO heuristic. That matters
because the default executor is ``ConcurrentToolExecutor``
(``strands/tools/executors/concurrent.py:19``), which runs a model turn's tool calls
as concurrent asyncio tasks, so before/after events for different tools interleave.

Observation only, deliberately
------------------------------
``BeforeToolCallEvent.cancel_tool`` is writable (``events.py:231``) and the executor
honours it in path: setting it short-circuits execution and substitutes an error
tool result (``strands/tools/executors/_executor.py:185-208``). **This listener never
writes it.** Deny belongs to the gate layer, not to the evidence layer; see the
``observation_mode`` proposal in capsule-emit #48. This listener is ``event_stream``
even though the hook surface it rides is ``in_path``-capable — recorded here so the
capability is not lost, and so nobody has to re-discover it to make the deny case.

Never-raises, and why it is load-bearing here
---------------------------------------------
``HookRegistry.invoke_callbacks_async`` catches only ``InterruptException``; every
other exception from a callback propagates (``registry.py`` docstring, and the loop
at ``:330-345``). The before-hook is invoked *outside* the executor's ``try``
(``_executor.py:177`` vs ``try:`` at ``:209``), so a raising before-callback aborts the
whole tool stream. The after-hook is invoked *inside* it (``:289``), so a raising
after-callback is caught by ``except Exception as e`` (``:318``) — which then builds an
error result and invokes the after-hook **again** (``:327``), where it raises a second
time, now uncaught. Either way a careless listener turns a working tool call into a
failed agent turn, and the after case double-fires. Every sealing path here is
therefore individually guarded: a broken ledger or anchor endpoint warns and is
skipped, and can never affect the agent.

Retries (their nearest thing to replay)
---------------------------------------
Strands has no result cache at the hook boundary, but it does have a retry loop:
``AfterToolCallEvent.retry`` is writable by any hook, and when set the executor
discards the result and re-enters the loop (``_executor.py:176`` / ``:299`` / ``:330``),
firing ``BeforeToolCallEvent`` again for the **same** ``toolUseId``.

This listener records that by counting before-events per ``toolUseId`` and stamping
``strands_attempt`` (1-based) plus ``strands_retry_of`` (the prior attempt's planned
capsule id) into the compute attestation from attempt 2 on. It deliberately does *not*
report ``AfterToolCallEvent.retry`` as seen at our callback's turn: after-events use
reverse callback ordering (``events.py``, ``should_reverse_callbacks``) and any hook may
flip the flag after us, so that read is a partial observation. A second before-event
for the same id is not — it is the executor having actually re-run the call.

Privacy: inputs and outputs are digested, never stored — the ledger carries
``agent_input_digest`` / ``agent_output_digest`` only. Raw floats in tool payloads fail
closed at the digest layer (``FloatInDigestError`` → warning, no capsule, run
unaffected).

All sealing logic lives in the framework-free :class:`StrandsListenerCore` (fully
testable without strands installed); :class:`StrandsCapsuleListener` is the
``HookProvider`` shell that binds it to ``HookRegistry.add_callback``.
"""
from __future__ import annotations

import warnings
from collections import OrderedDict
from typing import Any

from ._base import CapsuleEmitterBase

__all__ = ["StrandsCapsuleListener", "StrandsListenerCore"]

_RETRY_NOTE = (
    "the Strands tool executor re-entered its retry loop for this toolUseId; "
    "this is a re-run of the call, not a cached result"
)

# Content-block keys carrying opaque binary payloads (strands/types/tools.py:82).
_BINARY_BLOCK_KEYS = ("image", "document", "video")


def _model_from_agent(agent: Any) -> dict[str, str] | None:
    """Pull ``{provider, model_id}`` off a Strands ``Agent``, if it has a model.

    Duck-typed on purpose: this module stays importable without strands. Every
    first-party provider stores its id under ``model_id`` in the dict returned by
    ``Model.get_config()`` (``strands/models/model.py:213``; e.g. bedrock
    ``models/bedrock.py:219``, openai ``:67``, anthropic ``:89``), and the provider
    name is the model class's module leaf. A model that answers neither is skipped
    rather than guessed at.
    """
    model = getattr(agent, "model", None)
    if model is None:
        return None
    model_id = None
    try:
        config = model.get_config()
        if isinstance(config, dict):
            model_id = config.get("model_id") or config.get("model")
    except Exception:  # noqa: BLE001 — model introspection is never worth an exception
        model_id = None
    if not model_id:
        model_id = getattr(model, "model_id", None)
    provider = type(model).__module__.rsplit(".", 1)[-1] or None
    if not model_id and not provider:
        return None
    return {
        "provider": str(provider or "unknown"),
        "model_id": str(model_id or provider),
    }


def _result_payload(result: Any) -> Any:
    """Project a ``ToolResult`` into something the JCS digest can canonicalize.

    A ``ToolResult`` (``strands/types/tools.py:102``) is ``{toolUseId, status,
    content}`` where each content block is a ``ToolResultContent``
    (``:82``) holding exactly one of ``text``, ``json``, ``image``, ``document``
    or (in the bidi surface) ``video``. Text and json blocks are evidence and pass
    through; image/document/video blocks are raw ``bytes`` that no canonical JSON
    encoding accepts, so they are replaced by ``{"<kind>": "<omitted:N bytes>"}``.

    Without this projection an image-returning tool would fail the digest and
    silently produce no outcome capsule at all — fail-closed in the wrong place.
    Anything that is not the documented shape is returned untouched and left to
    the digest layer to accept or reject.
    """
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result
    projected: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            projected.append(block)
            continue
        for key in _BINARY_BLOCK_KEYS:
            if key in block:
                payload = block[key]
                size = len(payload) if isinstance(payload, (bytes, bytearray)) else None
                projected.append(
                    {key: f"<omitted:{size} bytes>" if size is not None else "<omitted>"}
                )
                break
        else:
            projected.append(block)
    out = dict(result)
    out["content"] = projected
    return out


class StrandsListenerCore(CapsuleEmitterBase):
    """Framework-free core of the Strands hook listener.

    :meth:`on_before_tool_call` and :meth:`on_after_tool_call` take duck-typed
    event objects, so the whole behavior is exercised in tests without strands
    installed. :class:`StrandsCapsuleListener` is the ``HookProvider`` that routes
    real events here.

    Sealing map (all through ``emit_capsule()``; capsule envelope invariant):

    - before tool call         → ``effect.status="planned"``
    - clean result             → ``effect.status="confirmed"``, chained
    - error result / exception → ``verdict="errored"``, ``effect.status="failed"``, chained
    - cancelled by a hook      → ``verdict="blocked"``, effect stays ``"planned"``, chained

    Args:
        include_attempt_marker: Stamp ``strands_attempt`` / ``strands_retry_of``
            on capsules from a retried tool call (default True). See the module
            docstring on the executor's retry loop.
        max_pending: Bound on remembered planned-capsule ids and attempt counters
            (default 256), so a long run that sees before-events without matching
            after-events cannot grow either table without bound.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer, ledger,
            anchor, anchor_url, anchor_wait, model, max_results).
    """

    def __init__(
        self,
        *,
        include_attempt_marker: bool = True,
        max_pending: int = 256,
        **base_kw: Any,
    ) -> None:
        super().__init__(**base_kw)
        self._include_attempt_marker = include_attempt_marker
        self._max_pending = max_pending
        # toolUseId -> planned capsule id awaiting its outcome
        self._pending: OrderedDict[str, str] = OrderedDict()
        # toolUseId -> (attempt count, planned id of the previous attempt)
        self._attempts: OrderedDict[str, tuple[int, str | None]] = OrderedDict()

    # -- helpers -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising (never break the agent)."""
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: Strands listener failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    @staticmethod
    def _tool_fields(event: Any) -> tuple[str, str | None, Any]:
        """``(tool_name, tool_use_id, tool_input)`` from an event's ``tool_use``."""
        tool_use = getattr(event, "tool_use", None)
        if not isinstance(tool_use, dict):
            return "tool", None, None
        name = tool_use.get("name") or "tool"
        raw_id = tool_use.get("toolUseId")
        return str(name), None if raw_id is None else str(raw_id), tool_use.get("input")

    def _bound(self, table: OrderedDict[str, Any]) -> None:
        while len(table) >= self._max_pending:
            table.popitem(last=False)  # evict oldest

    def _note_attempt(self, tool_use_id: str | None) -> tuple[int, str | None]:
        """Record a before-event for ``tool_use_id``; return ``(attempt, prior_planned)``.

        Attempt is 1-based. ``prior_planned`` is the planned capsule id of the
        previous attempt, or None on the first one.
        """
        if tool_use_id is None:
            return 1, None
        attempt, prior_planned = self._attempts.get(tool_use_id, (0, None))
        return attempt + 1, prior_planned

    def _attempt_compute(self, attempt: int, prior_planned: str | None) -> dict[str, Any] | None:
        """Compute-attestation add-ons for a re-run of an earlier tool call."""
        if not self._include_attempt_marker or attempt < 2:
            return None
        marker: dict[str, Any] = {"strands_attempt": attempt, "strands_retry_note": _RETRY_NOTE}
        if prior_planned is not None:
            marker["strands_retry_of"] = prior_planned
        return marker

    # -- the events --------------------------------------------------------

    def on_before_tool_call(self, event: Any) -> None:
        """``BeforeToolCallEvent`` → planned capsule (the commitment record).

        The listener reads ``event`` and writes nothing back to it. In particular
        it never sets ``cancel_tool`` — see the module docstring.
        """
        tool_name, tool_use_id, tool_input = self._tool_fields(event)
        attempt, prior_planned = self._note_attempt(tool_use_id)
        result = self._seal(
            action=tool_name,
            tool_input=tool_input,
            effect={"type": tool_name, "status": "planned"},
            action_type="fyi",
            runtime="strands",
            model=_model_from_agent(getattr(event, "agent", None)),
            extra_compute=self._attempt_compute(attempt, prior_planned),
        )
        if tool_use_id is None:
            return
        planned_id = None if result is None else result.capsule_id
        self._bound(self._attempts)
        self._attempts[tool_use_id] = (attempt, planned_id or prior_planned)
        if planned_id is not None:
            self._bound(self._pending)
            self._pending[tool_use_id] = planned_id

    def on_after_tool_call(self, event: Any) -> None:
        """``AfterToolCallEvent`` → confirmed / failed / cancelled capsule, chained.

        Reads ``result``, ``exception`` and ``cancel_message`` and writes nothing
        back; ``retry`` is deliberately not read (see the module docstring).
        """
        tool_name, tool_use_id, _ = self._tool_fields(event)
        planned_id = self._pending.pop(tool_use_id, None) if tool_use_id is not None else None
        attempt, _prior = (
            self._attempts.get(tool_use_id, (1, None)) if tool_use_id is not None else (1, None)
        )

        result = getattr(event, "result", None)
        exception = getattr(event, "exception", None)
        cancel_message = getattr(event, "cancel_message", None)
        status = result.get("status") if isinstance(result, dict) else None

        if cancel_message is not None:
            # A before-hook cancelled the call in path (executor _executor.py:185).
            # The tool never ran, so the effect record stays "planned" — the spec's
            # planned carve, effect_mode "not_applicable" — and the verdict carries
            # the refusal. Same shape as capsule_emit.gate's blocked emit, so a
            # reader (and viewer.py's is_refusal) sees a refusal, not a failure.
            #
            # "cancelled" is deliberately NOT used as an effect status: the reserved
            # set is planned/dispatched/confirmed/failed/reverted (§5.2), and an
            # unknown status derives effect_mode "dispatched_unconfirmed", which
            # would claim this tool dispatched when it did not.
            #
            # The refusal is SOMEBODY ELSE'S. This listener never writes cancel_tool;
            # the marker below says so, so no reader attributes the deny to us.
            marker: dict[str, Any] = {
                "strands_cancelled_by_hook": True,
                "observation_mode": "event_stream",
            }
            attempt_marker = self._attempt_compute(attempt, planned_id)
            if attempt_marker:
                marker.update(attempt_marker)
            self._seal(
                action=tool_name,
                tool_output={"cancel_message": str(cancel_message)},
                verdict="blocked",
                effect={"type": tool_name, "status": "planned"},
                prior_capsule_id=planned_id,
                action_type="fyi",
                runtime="strands",
                model=_model_from_agent(getattr(event, "agent", None)),
                extra_compute=marker,
            )
            return

        if exception is not None or status == "error":
            self._seal(
                action=tool_name,
                tool_output=str(exception) if exception is not None else _result_payload(result),
                verdict="errored",
                effect={"type": tool_name, "status": "failed"},
                prior_capsule_id=planned_id,
                action_type="fyi",
                runtime="strands",
                model=_model_from_agent(getattr(event, "agent", None)),
                extra_compute=self._attempt_compute(attempt, planned_id),
            )
            return

        self._seal(
            action=tool_name,
            tool_output=_result_payload(result),
            effect={"type": tool_name, "status": "confirmed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime="strands",
            model=_model_from_agent(getattr(event, "agent", None)),
            extra_compute=self._attempt_compute(attempt, planned_id),
        )


class StrandsCapsuleListener:
    """A Strands ``HookProvider`` that seals one capsule per tool-call boundary.

    Register it through the public constructor kwarg — no fork, no monkeypatch::

        listener = StrandsCapsuleListener(operator="acme-co", developer="my-agent@v1")
        agent = Agent(model=..., tools=[...], hooks=[listener])

    or against a registry directly (``agent.hooks.add_hook(listener)``).

    Accepts the same configuration as :class:`StrandsListenerCore` and exposes the
    core as :attr:`core` (``listener.core.last`` / ``.results``).

    The callbacks registered are **synchronous**. Strands supports async callbacks
    on the tool path (``HookRegistry.invoke_callbacks_async`` awaits coroutine
    callbacks, ``registry.py:333``), but the *sync* dispatcher
    ``HookRegistry.invoke_callbacks`` raises ``RuntimeError`` if any registered
    callback is async (``:377``). Sealing is a local append, not a network wait —
    so sync callbacks cost nothing on the async path and keep this provider usable
    on every dispatcher, present and future.
    """

    def __init__(self, **core_kw: Any) -> None:
        self.core = StrandsListenerCore(**core_kw)

    def register_hooks(self, registry: Any, **_kwargs: Any) -> None:
        """Register the two tool-call callbacks (the ``HookProvider`` protocol).

        Strands is imported here rather than at module scope so this listener —
        and its whole sealing core — stays importable and testable without the
        SDK. ``**_kwargs`` is accepted because their protocol declares it for
        forward compatibility (``registry.py:132``).
        """
        try:
            from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
        except ImportError as exc:  # pragma: no cover - exercised only without strands
            raise ImportError(
                "StrandsCapsuleListener needs strands-agents. "
                'Install with: pip install "capsule-emit[strands]"'
            ) from exc

        registry.add_callback(BeforeToolCallEvent, self.core.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.core.on_after_tool_call)

    # convenience passthroughs, mirroring the sibling listeners
    @property
    def last(self) -> Any:
        """The most recent EmitResult, or None."""
        return self.core.last

    @property
    def results(self) -> list[Any]:
        """All EmitResults sealed this session."""
        return self.core.results
