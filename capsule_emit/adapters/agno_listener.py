# SPDX-License-Identifier: Apache-2.0
"""Agno tool-hook listener — planned/confirmed/failed capsules per tool call.

    from agno.agent import Agent
    from capsule_emit.adapters.agno_listener import AgnoCapsuleListener

    listener = AgnoCapsuleListener(operator="acme-co", developer="my-agent@v1")
    agent = Agent(model=..., tools=[...], tool_hooks=[listener.hook])

Requires ``pip install "capsule-emit[agno]"``.

Third sibling in the listener family (``crewai_listener`` / ``langchain_listener``),
with the same core/shell split and the same two-record chain:

- before the tool runs → **planned** capsule (the commitment record)
- after it returns     → **confirmed** capsule, ``confirms``-chained to the
  planned one (response digest auto-derived)
- on exception         → ``verdict="errored"``, ``effect.status="failed"``,
  chained — errors become evidence instead of being dropped

Shape difference from the CrewAI/LangChain listeners
----------------------------------------------------
Those frameworks emit *separate* start/end/error callbacks, so their listeners
carry a ``run_id``-keyed pending map to pair a start with its outcome. Agno's
``tool_hooks`` are **middleware**: a hook receives a continuation and wraps the
call, so both records are sealed inside one invocation and ``planned_id`` is a
local variable. There is no pending map here, and no pairing heuristic to get
wrong — the chain link is structural.

Verified against the released agno wheel (see ``docs/adapters/agno.md`` for the
pin). Two facts from that wheel drive this design:

1. **Hook arguments are duck-typed by parameter name.** Agno inspects
   ``signature(hook).parameters`` and passes only the names it finds, from
   ``{agent, team, run_context, name, function_name, function, func,
   function_call, args, arguments}``. Note that ``function``/``func``/
   ``function_call`` all bind to the *continuation*, not to the tool's own
   entrypoint — calling it is what runs the rest of the chain.

2. **A hook that raises fails the tool call.** Agno runs the hook chain inside
   the same ``try`` as the entrypoint, so an exception raised by a listener is
   reported as the tool's own failure and the tool never executes. Every
   sealing path here is therefore wrapped: a broken ledger or anchor endpoint
   warns and is skipped, and cannot turn a working tool into a failed one.
   This is a stronger requirement than LangChain's (whose callback manager
   absorbs handler exceptions via ``raise_error=False``).

Replay / re-execution
---------------------
Agno caches tool results (``Function.cache_results``). On a cache hit the hook
chain still runs but **the entrypoint does not** — verified against the wheel.
The hook boundary cannot observe that distinction, so this listener does not
claim it did: ``planned`` means the call was committed to, ``confirmed`` means a
result came back for it. When ``include_replay_marker`` is on (default) and an
identical ``(tool, arguments)`` call has already been confirmed by this
listener, the repeat's capsule carries ``agno_replay_of`` (the earlier confirmed
capsule id) and ``agno_replay_note`` in its compute attestation — a pointer for
a reader, not an assertion that the tool re-ran.

Privacy: inputs and outputs are digested, never stored — the ledger carries
``agent_input_digest`` / ``agent_output_digest`` only.

Floats in tool payloads are canonicalized before they reach the digest layer
(RFC 8785 §3.2.2.3 decimal strings, via
:func:`capsule_emit.numbers.canonicalize_for_digest` in
``adapters._base.emit_capsule``), so an ordinary ``value: float`` tool argument
seals normally and its two records chain. What remains unsealable is a payload
with no canonical form at all — NaN, ±Infinity, an object JSON cannot carry —
or a genuinely broken ledger. Those still warn and seal no planned capsule:
warn-and-skip is the only agent-safe choice, because a hook that raises is
reported by agno as the tool's own failure (fact 2 above).

Skipping the planned record must not leave a *silent* orphan, though. The
outcome record carries ``unchained_reason`` in its compute attestation, so a
reader can tell "this outcome has no committed plan, and here is why" from the
ledger alone instead of seeing an unremarkable confirmed record with
``chain: null`` (capsule-emit#128).

All sealing logic lives in the framework-free :class:`AgnoListenerCore` (fully
testable without agno installed); :class:`AgnoCapsuleListener` binds it to
agno's hook calling convention.
"""
from __future__ import annotations

import hashlib
import json
import warnings
from collections import OrderedDict
from typing import Any, Callable

from ._base import CapsuleEmitterBase

__all__ = ["AgnoCapsuleListener", "AgnoListenerCore"]

#: Stamped on an outcome capsule whose planned capsule could not be sealed, so
#: the missing chain link is self-describing in the ledger rather than silent.
UNCHAINED_REASON = (
    "planned capsule could not be sealed for this tool call; this outcome "
    "record has no parent and is not evidence of a committed plan"
)

_REPLAY_NOTE = (
    "identical call previously confirmed by this listener; agno may serve a "
    "cached result without re-running the tool"
)


def _args_fingerprint(tool_name: str, arguments: Any) -> str | None:
    """Stable fingerprint of a tool call, for the replay marker only.

    Local and never sealed — the capsule carries the salted digests built by
    the core. Returns None if the arguments cannot be fingerprinted, which
    simply means this call gets no replay marker.
    """
    try:
        body = json.dumps(arguments, sort_keys=True, default=repr)
    except Exception:  # noqa: BLE001 — a fingerprint is never worth an exception
        try:
            body = repr(arguments)
        except Exception:  # noqa: BLE001
            return None
    return hashlib.sha256(f"{tool_name}\x00{body}".encode()).hexdigest()


def _model_from_agent(agent: Any) -> dict[str, str] | None:
    """Pull ``{provider, model_id}`` off an agno Agent/Team, if it has a model.

    Duck-typed on purpose: this module stays importable without agno, and the
    same shape is reachable on both ``Agent`` and ``Team``.
    """
    model = getattr(agent, "model", None)
    if model is None:
        return None
    model_id = getattr(model, "id", None)
    provider = getattr(model, "provider", None) or getattr(model, "name", None)
    if not model_id and not provider:
        return None
    return {
        "provider": str(provider or "unknown"),
        "model_id": str(model_id or provider),
    }


class AgnoListenerCore(CapsuleEmitterBase):
    """Framework-free core of the Agno tool-hook listener.

    :meth:`wrap_call` is the whole listener: hand it a tool name, a
    continuation, and the arguments, and it seals the planned record, runs the
    continuation, and seals the outcome. It takes a plain callable, so the full
    behavior is exercised in tests without agno installed.

    Sealing map (all through ``emit_capsule()``; capsule envelope invariant):

    - before the call → ``effect.status="planned"``
    - clean return    → ``effect.status="confirmed"``, chained to the planned id
    - exception       → ``verdict="errored"``, ``effect.status="failed"``, chained

    Args:
        include_replay_marker: Stamp ``agno_replay_of`` on a confirmed capsule
            whose ``(tool, arguments)`` this listener has already confirmed
            (default True). See the module docstring on agno's tool cache.
        max_seen: Bound on remembered call fingerprints (default 256), so a long
            run cannot grow the replay table without bound.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer,
            ledger, anchor, anchor_url, anchor_wait, model, max_results).
    """

    def __init__(
        self,
        *,
        include_replay_marker: bool = True,
        max_seen: int = 256,
        **base_kw: Any,
    ) -> None:
        super().__init__(**base_kw)
        self._include_replay_marker = include_replay_marker
        self._max_seen = max_seen
        # call fingerprint -> confirmed capsule id; insertion-ordered for bound-eviction
        self._seen: OrderedDict[str, str] = OrderedDict()

    # -- helpers -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising (never break the agent run)."""
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: Agno listener failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    def _remember(self, fingerprint: str | None, capsule_id: str | None) -> None:
        if fingerprint is None or capsule_id is None:
            return
        while len(self._seen) >= self._max_seen:
            self._seen.popitem(last=False)  # evict oldest
        self._seen[fingerprint] = capsule_id

    def _replay_compute(self, fingerprint: str | None) -> dict[str, Any] | None:
        """Compute-attestation add-ons marking a repeat of an earlier call."""
        if not self._include_replay_marker or fingerprint is None:
            return None
        prior = self._seen.get(fingerprint)
        if prior is None:
            return None
        return {"agno_replay_of": prior, "agno_replay_note": _REPLAY_NOTE}

    def _outcome_compute(
        self, fingerprint: str | None = None, planned_dropped: bool = False
    ) -> dict[str, Any] | None:
        """Merge the replay marker and the unchained-orphan marker, if any."""
        extra = dict(self._replay_compute(fingerprint) or {})
        if planned_dropped:
            extra["unchained_reason"] = UNCHAINED_REASON
        return extra or None

    # -- the three records -------------------------------------------------

    def seal_planned(
        self, tool_name: str, arguments: Any, model: dict[str, str] | None = None
    ) -> str | None:
        """Commitment record, sealed before the tool runs. Returns its id."""
        result = self._seal(
            action=tool_name,
            tool_input=arguments,
            effect={"type": tool_name, "status": "planned"},
            action_type="fyi",
            runtime="agno",
            model=model,
        )
        return None if result is None else result.capsule_id

    def seal_confirmed(
        self,
        tool_name: str,
        output: Any,
        planned_id: str | None,
        model: dict[str, str] | None = None,
        fingerprint: str | None = None,
        planned_dropped: bool = False,
    ) -> None:
        """Outcome record for a clean return, chained to the planned capsule.

        ``planned_dropped`` marks the record as a known orphan (the planned
        seal was attempted and failed) rather than letting ``chain: null``
        pass for an ordinary record — see :data:`UNCHAINED_REASON`.
        """
        result = self._seal(
            action=tool_name,
            tool_output=output,
            effect={"type": tool_name, "status": "confirmed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime="agno",
            model=model,
            extra_compute=self._outcome_compute(fingerprint, planned_dropped),
        )
        self._remember(fingerprint, None if result is None else result.capsule_id)

    def seal_failed(
        self,
        tool_name: str,
        error: BaseException | None,
        planned_id: str | None,
        model: dict[str, str] | None = None,
        planned_dropped: bool = False,
    ) -> None:
        """Outcome record for a raising tool — the error is evidence, not silence."""
        self._seal(
            action=tool_name,
            tool_output=None if error is None else str(error),
            verdict="errored",
            effect={"type": tool_name, "status": "failed"},
            prior_capsule_id=planned_id,
            action_type="fyi",
            runtime="agno",
            model=model,
            extra_compute=self._outcome_compute(planned_dropped=planned_dropped),
        )

    # -- the middleware ----------------------------------------------------

    def wrap_call(
        self,
        tool_name: str,
        call_next: Callable[..., Any],
        arguments: Any = None,
        model: dict[str, str] | None = None,
    ) -> Any:
        """Seal planned → run ``call_next(**arguments)`` → seal the outcome.

        The tool's own exception propagates unchanged (its failure belongs to
        the agent); the listener contributes no exception of its own. Every
        listener step is individually guarded, so a broken ledger cannot turn a
        working tool call into a failed one.
        """
        arguments = {} if arguments is None else arguments
        fingerprint = _args_fingerprint(tool_name, arguments)
        planned_id = self.seal_planned(tool_name, arguments, model)
        dropped = planned_id is None
        try:
            result = call_next(**arguments)
        except Exception as exc:
            self.seal_failed(tool_name, exc, planned_id, model, dropped)
            raise
        self.seal_confirmed(tool_name, result, planned_id, model, fingerprint, dropped)
        return result

    async def wrap_call_async(
        self,
        tool_name: str,
        call_next: Callable[..., Any],
        arguments: Any = None,
        model: dict[str, str] | None = None,
    ) -> Any:
        """Async twin of :meth:`wrap_call` (agno awaits the continuation)."""
        arguments = {} if arguments is None else arguments
        fingerprint = _args_fingerprint(tool_name, arguments)
        planned_id = self.seal_planned(tool_name, arguments, model)
        dropped = planned_id is None
        try:
            result = await call_next(**arguments)
        except Exception as exc:
            self.seal_failed(tool_name, exc, planned_id, model, dropped)
            raise
        self.seal_confirmed(tool_name, result, planned_id, model, fingerprint, dropped)
        return result


class AgnoCapsuleListener:
    """Binds :class:`AgnoListenerCore` to agno's tool-hook calling convention.

    Register the hook on an agent (or team, or a single ``@tool``)::

        listener = AgnoCapsuleListener(operator="acme-co", developer="my-agent@v1")
        agent = Agent(model=..., tools=[...], tool_hooks=[listener.hook])

    Use :attr:`async_hook` for ``arun``/``aexecute`` paths — agno skips async
    hooks on sync calls (and logs a warning), so register the one that matches
    the path you drive, or both hooks on their respective agents.

    Accepts the same configuration as :class:`AgnoListenerCore` and exposes the
    core as :attr:`core` (``listener.core.last`` / ``.results``).

    Unlike the CrewAI and LangChain listeners this shell needs no agno import:
    agno's hook protocol is structural (it inspects parameter names on a plain
    callable), so there is no base class to subclass and nothing to guard with
    an ImportError. The ``[agno]`` extra pins the version the hook contract was
    verified against.
    """

    def __init__(self, **core_kw: Any) -> None:
        self.core = AgnoListenerCore(**core_kw)
        core = self.core

        # Built once, not per property access: these callables are handed to
        # agno and end up in `Agent.tool_hooks`, where identity is what lets a
        # caller compare, deduplicate, or remove a registered hook. A property
        # that minted a fresh closure each time would put a different object in
        # the list than the one the caller thought they registered.
        def capsule_tool_hook(
            function_name: str,
            function_call: Callable[..., Any],
            arguments: dict[str, Any] | None = None,
            agent: Any = None,
        ) -> Any:
            return core.wrap_call(
                function_name, function_call, arguments, _model_from_agent(agent)
            )

        async def capsule_tool_hook_async(
            function_name: str,
            function_call: Callable[..., Any],
            arguments: dict[str, Any] | None = None,
            agent: Any = None,
        ) -> Any:
            return await core.wrap_call_async(
                function_name, function_call, arguments, _model_from_agent(agent)
            )

        self._hook = capsule_tool_hook
        self._async_hook = capsule_tool_hook_async

    @property
    def hook(self) -> Callable[..., Any]:
        """A sync ``tool_hooks`` callable — the same object on every access.

        The parameter names are the contract: agno fills ``function_name``,
        ``function_call`` (the continuation), ``arguments``, and ``agent`` by
        inspecting this signature. Renaming them silently changes what agno
        passes, so they are fixed rather than left to ``**kwargs`` — agno
        passes nothing it was not asked for by name.
        """
        return self._hook

    @property
    def async_hook(self) -> Callable[..., Any]:
        """An async ``tool_hooks`` callable for ``arun``/``aexecute`` paths."""
        return self._async_hook

    # convenience passthroughs, mirroring the sibling listeners
    @property
    def last(self) -> Any:
        """The most recent EmitResult, or None."""
        return self.core.last

    @property
    def results(self) -> list[Any]:
        """All EmitResults sealed this session."""
        return self.core.results
