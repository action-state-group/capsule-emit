# SPDX-License-Identifier: Apache-2.0
"""Thin CrewAI shell over CapsuleEmitterBase (~15 lines of adapter logic).

    from capsule_emit.adapters.crewai import CrewAICapsuleEmitter

    emitter = CrewAICapsuleEmitter(operator="acme-co", developer="my-agent@v1")
    wrapped_tool = emitter.wrap(my_crewai_tool)

Works without installing crewai — ``wrap()`` is framework-free.

This module also holds :class:`CrewAIListenerCore`, the framework-free half of
the event-bus listener. The thin ``crewai``-importing shell that binds it to
``BaseEventListener`` lives in :mod:`capsule_emit.adapters.crewai_listener`.
"""
from __future__ import annotations

import functools
import json
import warnings
from collections import deque
from typing import Any, Callable

from ._base import CapsuleEmitterBase

__all__ = ["CrewAICapsuleEmitter", "CrewAIListenerCore"]


class CrewAICapsuleEmitter(CapsuleEmitterBase):
    """CrewAI adapter — wrap a tool callable; emit a capsule per call."""

    def wrap(self, tool: Any, action: str | None = None) -> Any:
        """Wrap a CrewAI tool or any callable; emit a capsule on each call."""
        _action = action or getattr(tool, "name", None) or getattr(tool, "__name__", "tool")

        if callable(tool):
            @functools.wraps(tool)
            def _wrapper(*args: Any, **kwargs: Any) -> Any:
                output = tool(*args, **kwargs)
                inp = kwargs if kwargs else (args[0] if len(args) == 1 else args)
                self.emit_capsule(_action, tool_input=inp, tool_output=output)
                return output
            return _wrapper

        # CrewAI BaseTool subclass: patch ._run
        original_run = getattr(tool, "_run", None)
        if original_run is None:
            return tool

        @functools.wraps(original_run)
        def _patched_run(*args: Any, **kwargs: Any) -> Any:
            output = original_run(*args, **kwargs)
            inp = kwargs if kwargs else (args[0] if len(args) == 1 else args)
            self.emit_capsule(_action, tool_input=inp, tool_output=output)
            return output

        tool._run = _patched_run
        return tool


class CrewAIListenerCore(CapsuleEmitterBase):
    """Framework-free core of the CrewAI event-bus listener.

    Holds all sealing logic for crew events as plain methods over duck-typed
    event objects, so the behavior is fully testable without ``crewai``
    installed. :class:`capsule_emit.adapters.crewai_listener.CapsuleEventListener`
    is the thin ``BaseEventListener`` shell that routes real bus events here.

    Sealing map (one capsule per event, all through ``capsule_emit.emit()``):

    - tool started  → ``effect.status="planned"`` (the commitment record)
    - tool finished → ``effect.status="confirmed"``, ``confirms`` the planned
      capsule (the did→confirmed chain; response digest auto-derived)
    - tool error    → ``verdict="errored"``, ``effect.status="failed"``,
      chained to the planned capsule
    - crew kickoff started/completed/failed → fyi capsules
      (``include_lifecycle``, default on — one per crew run)
    - LLM call events → OFF by default (``include_llm``); volume, not evidence

    Replay safety: when ``replay_check`` returns True (wired to the bus's
    ``is_replaying()`` by the shell, per CrewAI's documented guidance that
    side-effecting listeners early-return during replay), no capsule is
    emitted. Content-digest idempotence is the backstop.

    A handler never raises: emission failures ``warnings.warn`` and return.
    CrewAI's bus already isolates handler exceptions (``is_call_handler_safe``);
    this is belt and suspenders so a broken anchor endpoint cannot even print
    a traceback into a crew run.

    Args:
        include_lifecycle: Seal crew kickoff started/completed/failed as fyi
            capsules (default True; one capsule per crew run per event).
        include_llm: Seal LLM call events (default False).
        replay_check: Zero-arg callable; True means "replaying, do not seal".
        max_pending: Bound on remembered planned-capsule ids per
            (tool, args) key (default 64) so a listener that sees starts
            without finishes cannot grow without bound.
        **base_kw: :class:`CapsuleEmitterBase` config (operator, developer,
            ledger, anchor, anchor_url, anchor_wait, model, max_results).
    """

    def __init__(
        self,
        *,
        include_lifecycle: bool = True,
        include_llm: bool = False,
        replay_check: Callable[[], bool] | None = None,
        max_pending: int = 64,
        **base_kw: Any,
    ) -> None:
        super().__init__(**base_kw)
        self._include_lifecycle = include_lifecycle
        self._include_llm = include_llm
        self._replay_check = replay_check
        self._max_pending = max_pending
        # (tool_name, args_key) -> FIFO of planned capsule_ids awaiting outcome
        self._pending: dict[tuple[str, str], deque[str]] = {}

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _args_key(tool_args: Any) -> str:
        """Correlation key for started→finished pairing. Tolerant by design —
        this keys an in-memory dict, it is NOT the sealed digest (which is
        JCS via ``emit()`` and fails closed on floats)."""
        try:
            return json.dumps(tool_args, sort_keys=True, default=str)
        except Exception:
            return repr(tool_args)

    def _replaying(self) -> bool:
        if self._replay_check is None:
            return False
        try:
            return bool(self._replay_check())
        except Exception:
            return False  # a broken replay probe must not silence evidence

    def _seal(self, **emit_kw: Any) -> Any | None:
        """emit_capsule that warns instead of raising (never break the crew)."""
        try:
            return self.emit_capsule(**emit_kw)
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the boundary
            warnings.warn(
                f"capsule-emit: CrewAI listener failed to seal a capsule: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

    # -- tool events -------------------------------------------------------

    def on_tool_started(self, event: Any) -> None:
        """ToolUsageStartedEvent → planned capsule (the commitment record)."""
        if self._replaying():
            return
        tool_name = getattr(event, "tool_name", None) or "tool"
        tool_args = getattr(event, "tool_args", None)
        result = self._seal(
            action=tool_name,
            tool_input=tool_args,
            effect={"type": tool_name, "status": "planned"},
            action_type="fyi",
            runtime="crewai",
        )
        if result is None:
            return
        key = (tool_name, self._args_key(tool_args))
        stack = self._pending.setdefault(key, deque(maxlen=self._max_pending))
        stack.append(result.capsule_id)

    def _pop_planned(self, tool_name: str, tool_args: Any) -> str | None:
        key = (tool_name, self._args_key(tool_args))
        stack = self._pending.get(key)
        if not stack:
            return None
        planned_id = stack.popleft()  # FIFO: first started, first resolved
        if not stack:
            del self._pending[key]
        return planned_id

    def on_tool_finished(self, event: Any) -> None:
        """ToolUsageFinishedEvent → confirmed capsule chained to the planned one."""
        if self._replaying():
            return
        tool_name = getattr(event, "tool_name", None) or "tool"
        tool_args = getattr(event, "tool_args", None)
        output = getattr(event, "output", None)
        self._seal(
            action=tool_name,
            tool_input=tool_args,
            tool_output=output,
            effect={"type": tool_name, "status": "confirmed"},
            prior_capsule_id=self._pop_planned(tool_name, tool_args),
            action_type="fyi",
            runtime="crewai",
        )

    def on_tool_error(self, event: Any) -> None:
        """ToolUsageErrorEvent → errored/failed capsule chained to the planned one."""
        if self._replaying():
            return
        tool_name = getattr(event, "tool_name", None) or "tool"
        tool_args = getattr(event, "tool_args", None)
        error = getattr(event, "error", None)
        self._seal(
            action=tool_name,
            tool_input=tool_args,
            tool_output=None if error is None else str(error),
            verdict="errored",
            effect={"type": tool_name, "status": "failed"},
            prior_capsule_id=self._pop_planned(tool_name, tool_args),
            action_type="fyi",
            runtime="crewai",
        )

    # -- crew lifecycle ----------------------------------------------------

    def on_crew_kickoff(self, event: Any, phase: str) -> None:
        """Crew kickoff started/completed/failed → one fyi capsule.

        ``phase`` ∈ {"started", "completed", "failed"}.
        """
        if not self._include_lifecycle or self._replaying():
            return
        crew_name = getattr(event, "crew_name", None) or "crew"
        payload: dict[str, Any] = {"crew_name": crew_name}
        for field in ("inputs", "output", "error"):
            value = getattr(event, field, None)
            if value is not None:
                payload[field] = str(value)
        self._seal(
            action=f"crew_kickoff_{phase}",
            tool_input=payload,
            verdict="errored" if phase == "failed" else "executed",
            action_type="fyi",
            runtime="crewai",
        )

    # -- LLM events (off by default) ---------------------------------------

    def on_llm_call(self, event: Any, phase: str) -> None:
        """LLM call started/completed → fyi capsule, only when ``include_llm``."""
        if not self._include_llm or self._replaying():
            return
        self._seal(
            action=f"llm_call_{phase}",
            tool_input={"model": str(getattr(event, "model", None))},
            action_type="fyi",
            runtime="crewai",
        )
