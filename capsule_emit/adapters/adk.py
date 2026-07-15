# SPDX-License-Identifier: Apache-2.0
"""Google ADK (Agent Development Kit) shell over CapsuleEmitterBase.

Two integration paths, because ADK apps are built two ways:

1. **Tool callbacks** — pass the emitter's bound callbacks to an agent::

       from capsule_emit.adapters.adk import ADKCapsuleEmitter

       emitter = ADKCapsuleEmitter(operator="acme-co", developer="po-agent@v1",
                                   model={"provider": "google", "model_id": "gemini-2.0-flash"})
       agent = LlmAgent(
           name="writer", model="gemini-2.0-flash", tools=[...],
           after_tool_callback=emitter.after_tool_callback,
           before_tool_callback=emitter.before_tool_callback,  # optional (pass-through)
       )

   ``after_tool_callback`` emits one ``executed`` capsule per completed tool call.

2. **Event-stream tap** — for apps that consume the ``Runner`` event stream and do
   not register tool callbacks at all (a common ADK pattern)::

       async for event in runner.run_async(...):
           emitter.tap_event(event)          # emits a capsule per completed tool call
           ...                                # your own handling continues

   or, to drain an entire stream::

       await emitter.tap_stream(runner.run_async(...))

A callback path that silently misses event-stream apps would under-report; this
adapter covers both shapes.

**Effects for consequential tools.** Auto-wiring seals every tool call, but by
default asserts no world-effect (read-only calls should not manufacture one). To
attach an effect to a consequential tool *without* dropping the callback wiring,
declare it once at construction — the callback and the event tap both look it up
by tool name::

    ADKCapsuleEmitter(..., effects={"write_order": {"type": "write_order",
                                                     "status": "dispatched"}})

**Refusals stay a policy act.** ``blocked`` / ``denied`` verdicts are NOT inferred
here — recording a refusal is a policy decision, and this library stays
enforcement-neutral. When *your* gate blocks a tool, call :meth:`emit_blocked` /
:meth:`emit_denied`, or wrap a predicate with :meth:`guard` to get a ready
``before_tool_callback`` that seals the block and short-circuits the tool.

**Errors are opt-in, not inferred.** ADK's ``after_tool_callback`` fires only after
a tool *returns*, so a tool that raises produces no capsule on Path 1; call
:meth:`emit_errored` from your own ``except`` block to seal the failed attempt. On
Path 2 an error-shaped ``function_response`` is sealed like any other output.

**Observation mode is stamped on every capsule** (per the profile's
`observation_mode` compute-attestation member): the callback path and the
manual seals (`guard`/`emit_blocked`/`emit_denied`/`emit_errored`) stamp
``in_path`` — the emitter sits in the action's call path; the event tap
stamps ``event_stream`` — the emitter observes the runtime's narration,
where id-less pairing is best-effort. Provenance, not a quality score:
the capsule states how it observed; the consumer decides the weight.

**Emit-error policy (matches the MCP adapter).** On the auto-instrumented paths
(``after_tool_callback`` / ``tap_event``) a failed emit is warned (RuntimeWarning)
and logged, never propagated — the record layer must not crash the agent's tool
path. A failed id-bearing emit is left un-marked, so the other path may still seal
it. Direct calls (:meth:`emit_blocked` / :meth:`emit_denied` / :meth:`emit_errored`)
raise normally; inside :meth:`guard` the block short-circuit is returned even if
sealing the refusal fails (the gate outcome must not depend on the record layer).

``tool_context`` is NOT serialized wholesale: only a minimal producer-context
allow-list (``agent_name``, ``function_call_id``, ``invocation_id`` — correlation
handles, not end-user PII) is threaded into ``compute_attestation``. The ADK
``session`` (which can carry ``user_id`` / ``session_id``) is deliberately never
pulled into the content-addressed capsule. These correlation ids are committed in
clear (the tool payload is digest-only); treat this hygiene as adapter policy, not
a spec guarantee.

Caveats: wiring both ``after_tool_callback`` and the event tap is now safe — a call
carrying a ``function_call_id`` is sealed at most once (deduped by id), so a
belt-and-braces setup does not double-count. Dedup can only work when an id is
present; under true concurrency with no ``function_call_id``, input->output pairing
is best-effort (the capsule still verifies, but a verifying capsule is not
necessarily a correctly-paired one), and a both-paths setup could double-count such
id-less calls — prefer one path there. An id-bearing response whose call was never
seen (dropped stream, evicted pending entry) seals with unknown input rather than
borrowing an id-less call's args. Unpaired calls (a tool that never returns, a
dropped stream) are bounded: pending state and the dedup/results history are all
capped, evicting oldest with a ``logging`` warning rather than growing without end.
The dedup-id history is itself bounded (``max_pending``), so after that many newer
ids a *very* late duplicate could re-seal — raise the cap for extremely long-lived
runners if that matters to your ledger.
"""
from __future__ import annotations

import logging
import warnings
from collections import OrderedDict, deque
from typing import Any, Callable

from ._base import CapsuleEmitterBase

__all__ = ["ADKCapsuleEmitter"]

_log = logging.getLogger(__name__)

# Attributes safe to record from a ToolContext. Deliberately excludes `session`
# and anything that can carry end-user identifiers into a tamper-evident record.
_SAFE_CONTEXT_ATTRS = ("agent_name", "function_call_id", "invocation_id")

# Bounds for long-lived runners: pending calls awaiting a response, deduped-id
# history, and retained EmitResults. Oldest is evicted past these caps.
_MAX_PENDING = 4096
_MAX_RESULTS = 4096


class _BoundedSet:
    """A set with FIFO eviction — membership without unbounded growth."""

    def __init__(self, maxlen: int) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self._max = maxlen

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def add(self, key: str) -> None:
        self._d[key] = None
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)


def _tool_name(tool: Any) -> str:
    """Best-effort tool name across ADK versions (BaseTool.name; else repr).

    Falls back to the literal ``"tool"`` only when nothing name-like is present;
    such calls share one id-less FIFO queue, so supply named tools for clean pairing.
    """
    return getattr(tool, "name", None) or getattr(tool, "__name__", None) or "tool"


def _compute_from_context(tool_context: Any) -> dict[str, Any]:
    """Pull ONLY a minimal producer-context allow-list (correlation handles) from a ToolContext.

    Never serializes the whole context; never touches `session`/user identifiers.
    """
    if tool_context is None:
        return {}
    out: dict[str, Any] = {}
    for attr in _SAFE_CONTEXT_ATTRS:
        val = getattr(tool_context, attr, None)
        if val is not None:
            out[f"adk_{attr}"] = str(val)
    return out


class ADKCapsuleEmitter(CapsuleEmitterBase):
    """Google ADK adapter — one capsule per completed tool call.

    Works via tool callbacks (:meth:`after_tool_callback` / :meth:`before_tool_callback`)
    or via the event stream (:meth:`tap_event` / :meth:`tap_stream`). Model is taken
    from the ``model=`` passed at construction (ADK does not surface the model in the
    tool-callback signature); override per-call is still available on the base.

    Args (in addition to the base):
        effects: Optional ``{tool_name: effect_dict}`` map. When a sealed tool call's
            name is present, that effect rides the capsule — the declarative way to mark
            consequential tools without giving up the auto-wiring. Absent name -> no
            effect (read-only default).
        max_pending: Cap on in-flight calls awaiting a response and on the deduped-id
            history; oldest is evicted (with a warning) past this. Also feeds the base
            ``max_results`` default so a long-lived tap does not retain results forever.
    """

    def __init__(
        self,
        *,
        effects: dict[str, dict[str, Any]] | None = None,
        max_pending: int = _MAX_PENDING,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("max_results", _MAX_RESULTS)
        CapsuleEmitterBase.__init__(self, **kwargs)
        self._effects: dict[str, dict[str, Any]] = dict(effects or {})
        self._max_pending = max_pending
        # function_call_id -> (tool_name, args): the correct, unambiguous pairing.
        # OrderedDict so an unpaired call (no response ever arrives) can be evicted
        # oldest-first once the cap is hit, instead of leaking for the runner's life.
        self._pending: OrderedDict[str, tuple[str, Any]] = OrderedDict()
        # tool_name -> FIFO queue of args, ONLY for calls that carry no id. Without
        # a per-name queue, two concurrent id-less calls to the same tool would
        # collide on a name key and mispair input->output in a tamper-evident record.
        self._pending_by_name: dict[str, deque] = {}
        # ids already sealed — makes wiring both paths idempotent (no double-count).
        self._seen = _BoundedSet(max_pending)

    def _effect_for(self, name: str) -> dict[str, Any] | None:
        """The declared effect for a tool name (a fresh copy), or None."""
        eff = self._effects.get(name)
        return dict(eff) if eff else None

    def _safe_emit(self, name: str, **emit_kwargs: Any) -> bool:
        """Emit a capsule; emit errors are warned and logged, never propagated.

        The record layer must never crash the agent's tool path (same policy as
        the MCP adapter). Returns True when the capsule sealed — callers gate
        dedup marking on this, so a call whose emit failed on one path can
        still be sealed by the other.
        """
        try:
            self.emit_capsule(name, **emit_kwargs)
            return True
        except Exception as exc:
            msg = f"capsule-emit: failed to seal capsule for '{name}': {exc}"
            warnings.warn(msg, RuntimeWarning, stacklevel=3)
            _log.warning(msg, exc_info=exc)
            return False

    # ------------------------------------------------------------------
    # Path 1 — tool callbacks
    # ------------------------------------------------------------------

    def before_tool_callback(
        self, tool: Any, args: dict[str, Any], tool_context: Any = None
    ) -> None:
        """ADK before_tool_callback. Pass-through: returns None so the tool runs.

        Present so the emitter can be wired symmetrically; recording a *block* is a
        policy act — use :meth:`guard` (or call :meth:`emit_blocked` from your own
        gate), not this no-op.
        """
        return None

    def after_tool_callback(
        self,
        tool: Any,
        args: dict[str, Any],
        tool_context: Any = None,
        tool_response: Any = None,
    ) -> None:
        """ADK after_tool_callback. Emits one ``executed`` capsule per tool call.

        Returns None (does not override the tool response). If the call's
        ``function_call_id`` was already sealed (e.g. by the event tap), this is a
        no-op — so wiring both paths does not double-count.
        """
        call_id = getattr(tool_context, "function_call_id", None)
        if call_id and call_id in self._seen:
            return None
        name = _tool_name(tool)
        sealed = self._safe_emit(
            name,
            tool_input=args,
            tool_output=tool_response,
            effect=self._effect_for(name),
            runtime="adk",
            extra_compute={"observation_mode": "in_path",
                           **_compute_from_context(tool_context)},
        )
        if call_id and sealed:
            self._seen.add(call_id)
        return None

    # ------------------------------------------------------------------
    # Path 2 — event-stream tap
    # ------------------------------------------------------------------

    def tap_event(self, event: Any) -> None:
        """Emit capsules for any completed tool call carried by one ADK ``Event``.

        Pairs function-call parts (args) with function-response parts (result) by
        ``id`` across events, so it works for concurrent ``ParallelAgent`` tool use
        where calls and responses interleave with no fixed order. A response whose
        ``id`` was already sealed is skipped (dedup); a call whose response never
        arrives is bounded and eventually evicted (see :attr:`_max_pending`); an
        id-bearing *orphan* response (call never seen) seals with unknown input —
        it never borrows from the id-less queue.

        When a function-call part carries no ``id`` (version/runtime dependent),
        pairing falls back to a per-tool-name FIFO queue rather than a single name
        key — so concurrent id-less calls to the same tool each keep their own input
        instead of one silently overwriting the other. FIFO is best-effort under
        true concurrency — an id-less concurrent capsule is integrity-valid but its
        input->output pairing is not guaranteed (it verifies, yet a verifying
        capsule is not necessarily correctly paired); supply stable
        ``function_call_id``s for exact pairing.
        """
        for name, call_id, args in _function_calls(event):
            if call_id:
                self._pending[call_id] = (name, args)
            else:
                queue = self._pending_by_name.setdefault(name, deque(maxlen=self._max_pending))
                if len(queue) == queue.maxlen:
                    _log.warning(
                        "adk: evicting oldest unpaired id-less call for tool %r "
                        "(pending cap %d reached; no capsule emitted for it)",
                        name,
                        self._max_pending,
                    )
                queue.append(args)
        self._evict_pending()
        for name, call_id, response in _function_responses(event):
            if call_id and call_id in self._seen:
                self._pending.pop(call_id, None)  # already sealed via another path
                continue
            if call_id:
                # id-bearing responses pair ONLY by id. An orphan (its call event
                # was dropped or evicted) seals with unknown input rather than
                # raiding the id-less FIFO — an unknown id must never claim an
                # id-less call's args, or both capsules seal mispaired.
                pname, pargs = self._pending.pop(call_id, (name, None))
            else:
                queue = self._pending_by_name.get(name)
                pargs = queue.popleft() if queue else None
                pname = name
                if queue is not None and not queue:
                    del self._pending_by_name[name]  # don't accumulate empty queues
            sealed = self._safe_emit(
                pname,
                tool_input=pargs,
                tool_output=response,
                effect=self._effect_for(pname),
                runtime="adk",
                extra_compute={"observation_mode": "event_stream",
                               **({"adk_function_call_id": call_id} if call_id else {})},
            )
            if call_id and sealed:
                self._seen.add(call_id)

    def _evict_pending(self) -> None:
        """Drop oldest unpaired id-bearing calls once the pending cap is exceeded."""
        while len(self._pending) > self._max_pending:
            old_id, _ = self._pending.popitem(last=False)
            _log.warning(
                "adk: evicting unpaired tool call %s (pending cap %d reached; "
                "no function_response arrived — no capsule emitted for it)",
                old_id,
                self._max_pending,
            )

    def tap_stream(self, events: Any) -> Any:
        """Drain a sync OR async ADK event iterable, tapping each event.

        Returns an awaitable when handed an async iterator, else runs inline.
        """
        if hasattr(events, "__aiter__"):
            async def _drain() -> None:
                async for ev in events:
                    self.tap_event(ev)
            return _drain()
        for ev in events:
            self.tap_event(ev)
        return None

    # ------------------------------------------------------------------
    # Refusals & errors — call these from your own policy gate / except block
    # ------------------------------------------------------------------

    def guard(
        self, predicate: Callable[[str, dict[str, Any]], bool], *, reason: str = "policy"
    ) -> Callable[..., Any]:
        """Build a ``before_tool_callback`` from your policy ``predicate(name, args)``.

        Returns None to let the tool run when the predicate is truthy; otherwise records
        a ``blocked`` capsule (auditor-grade evidence the gate declined) and returns ADK's
        short-circuit dict so the tool does not run. The policy is yours — this attests
        the outcome; the library evaluates no policy of its own::

            agent = LlmAgent(..., before_tool_callback=emitter.guard(policy.allows))

        The short-circuit is returned even if sealing the refusal fails (warned and
        logged, never propagated) — the gate outcome must not depend on the record
        layer's health.
        """
        def _before(tool: Any, args: dict[str, Any], tool_context: Any = None) -> Any:
            if predicate(_tool_name(tool), args):
                return None
            try:
                self.emit_blocked(tool, args, tool_context, reason=reason)
            except Exception as exc:
                msg = f"capsule-emit: failed to seal blocked capsule for '{_tool_name(tool)}': {exc}"
                warnings.warn(msg, RuntimeWarning, stacklevel=2)
                _log.warning(msg, exc_info=exc)
            return {"error": f"blocked by {reason}"}

        return _before

    def emit_blocked(
        self, tool: Any, args: dict[str, Any], tool_context: Any = None, *, reason: str | None = None
    ):
        """Record a ``blocked`` capsule: a gate stopped this tool before it ran."""
        return self.emit_capsule(
            _tool_name(tool),
            tool_input=args,
            tool_output=({"reason": reason} if reason else None),
            verdict="blocked",
            runtime="adk",
            extra_compute={"observation_mode": "in_path",
                           **_compute_from_context(tool_context)},
        )

    def emit_denied(
        self, tool: Any, args: dict[str, Any], tool_context: Any = None, *, reason: str | None = None
    ):
        """Record a ``denied`` capsule: authorization refused this tool."""
        return self.emit_capsule(
            _tool_name(tool),
            tool_input=args,
            tool_output=({"reason": reason} if reason else None),
            verdict="denied",
            runtime="adk",
            extra_compute={"observation_mode": "in_path",
                           **_compute_from_context(tool_context)},
        )

    def emit_errored(
        self, tool: Any, args: dict[str, Any], error: Any, tool_context: Any = None
    ):
        """Record a tool call that ran and raised — Path 1 can't see this otherwise.

        Verdict stays ``executed`` (the attempt happened); the exception is recorded
        as the output. No effect is asserted even for a tool with a declared effect: a
        raise means the world-effect did not demonstrably dispatch, so claiming one
        would be exactly the may/did confusion the capsule exists to prevent.

        On success the call's ``function_call_id`` (when present on *tool_context*)
        is marked sealed — so if ADK also surfaces the failure as an error-shaped
        ``function_response`` on the event stream, the tap does not double-seal it.
        """
        result = self.emit_capsule(
            _tool_name(tool),
            tool_input=args,
            tool_output={"error": str(error)},
            runtime="adk",
            extra_compute={"observation_mode": "in_path",
                           **_compute_from_context(tool_context)},
        )
        call_id = getattr(tool_context, "function_call_id", None)
        if call_id:
            self._seen.add(call_id)
        return result


# ----------------------------------------------------------------------
# Event parsing — tolerant of ADK version differences
# ----------------------------------------------------------------------

def _function_calls(event: Any):
    """Yield (name, id, args) for each function-call part on an event."""
    getter = getattr(event, "get_function_calls", None)
    if callable(getter):
        for fc in getter() or []:
            yield getattr(fc, "name", "tool"), getattr(fc, "id", None), getattr(fc, "args", None)
        return
    for part in _parts(event):
        fc = getattr(part, "function_call", None)
        if fc is not None:
            yield getattr(fc, "name", "tool"), getattr(fc, "id", None), getattr(fc, "args", None)


def _function_responses(event: Any):
    """Yield (name, id, response) for each function-response part on an event."""
    getter = getattr(event, "get_function_responses", None)
    if callable(getter):
        for fr in getter() or []:
            yield getattr(fr, "name", "tool"), getattr(fr, "id", None), getattr(fr, "response", None)
        return
    for part in _parts(event):
        fr = getattr(part, "function_response", None)
        if fr is not None:
            yield getattr(fr, "name", "tool"), getattr(fr, "id", None), getattr(fr, "response", None)


def _parts(event: Any):
    content = getattr(event, "content", None)
    return getattr(content, "parts", None) or []
