# SPDX-License-Identifier: Apache-2.0
"""CrewAI event-bus listener — register once, every tool call seals a capsule.

    from capsule_emit.adapters.crewai_listener import CapsuleEventListener

    listener = CapsuleEventListener(operator="acme-co", developer="ops-crew@v1")
    # instantiate before crew.kickoff(); registration happens in __init__
    crew.kickoff()

Requires ``pip install "capsule-emit[crewai]"``. This is the idiomatic
integration path — CrewAI's own docs teach ``BaseEventListener`` as *the*
third-party mechanism (it's how the listed observability tools integrate).
The older :class:`~capsule_emit.adapters.crewai.CrewAICapsuleEmitter` wrap()
adapter stays for surgical per-tool use.

All sealing logic lives in the framework-free
:class:`~capsule_emit.adapters.crewai.CrewAIListenerCore`; this module is only
the binding to CrewAI's bus:

- ``ToolUsageStartedEvent``  → planned capsule
- ``ToolUsageFinishedEvent`` → confirmed capsule (chained to the planned one)
- ``ToolUsageErrorEvent``    → errored/failed capsule (chained)
- ``CrewKickoffStarted/Completed/FailedEvent`` → fyi capsules (config-gated)
- LLM call events → off by default (``include_llm=True`` to enable)

Replay safety: per CrewAI's documented guidance that side-effecting listeners
early-return during replay, handlers check the bus's ``is_replaying()`` (when
the installed CrewAI version exposes it) before sealing anything.

Error isolation: CrewAI's bus runs handlers through ``is_call_handler_safe``
(exceptions are printed, never raised into the crew run); the core additionally
never raises — a broken anchor endpoint cannot affect a crew.
"""
from __future__ import annotations

from typing import Any

from .crewai import CrewAIListenerCore

__all__ = ["CapsuleEventListener"]

try:  # modern layout (crewai >= 0.100-ish)
    from crewai.events import (
        BaseEventListener,
        CrewKickoffCompletedEvent,
        CrewKickoffFailedEvent,
        CrewKickoffStartedEvent,
        LLMCallCompletedEvent,
        LLMCallStartedEvent,
        ToolUsageErrorEvent,
        ToolUsageFinishedEvent,
        ToolUsageStartedEvent,
    )
except ImportError:  # pragma: no cover - exercised only on older crewai
    try:  # older layout
        from crewai.utilities.events import (  # type: ignore[no-redef]
            BaseEventListener,
            CrewKickoffCompletedEvent,
            CrewKickoffFailedEvent,
            CrewKickoffStartedEvent,
            LLMCallCompletedEvent,
            LLMCallStartedEvent,
            ToolUsageErrorEvent,
            ToolUsageFinishedEvent,
            ToolUsageStartedEvent,
        )
    except ImportError as exc:
        raise ImportError(
            "CapsuleEventListener needs crewai. "
            'Install with: pip install "capsule-emit[crewai]"'
        ) from exc

try:
    # Module-level in current crewai (contextvar-backed); per their guidance,
    # side-effecting listeners early-return while it is True.
    from crewai.events.event_bus import is_replaying as _is_replaying
except ImportError:  # pragma: no cover - older crewai without replay support
    _is_replaying = None


class CapsuleEventListener(BaseEventListener):
    """CrewAI ``BaseEventListener`` that seals one capsule per crew event.

    Accepts the same configuration as
    :class:`~capsule_emit.adapters.crewai.CrewAIListenerCore` (operator,
    developer, ledger, anchor..., include_lifecycle, include_llm) and exposes
    the core as :attr:`core` (``listener.core.last`` / ``.results``).

    ``BaseEventListener.__init__`` registers against the global
    ``crewai_event_bus`` — instantiate once, before ``crew.kickoff()``.
    """

    def __init__(self, **core_kw: Any) -> None:
        self.core = CrewAIListenerCore(**core_kw)
        super().__init__()  # triggers setup_listeners(crewai_event_bus)

    def setup_listeners(self, crewai_event_bus: Any) -> None:  # noqa: D102 - their hook
        core = self.core
        # Wire the replay guard when this CrewAI exposes it: prefer the
        # module-level is_replaying(); fall back to a bus method if present.
        replay_probe = _is_replaying or getattr(crewai_event_bus, "is_replaying", None)
        if core._replay_check is None and callable(replay_probe):
            core._replay_check = replay_probe

        @crewai_event_bus.on(ToolUsageStartedEvent)
        def _on_tool_started(source: Any, event: Any) -> None:
            core.on_tool_started(event)

        @crewai_event_bus.on(ToolUsageFinishedEvent)
        def _on_tool_finished(source: Any, event: Any) -> None:
            core.on_tool_finished(event)

        @crewai_event_bus.on(ToolUsageErrorEvent)
        def _on_tool_error(source: Any, event: Any) -> None:
            core.on_tool_error(event)

        @crewai_event_bus.on(CrewKickoffStartedEvent)
        def _on_crew_started(source: Any, event: Any) -> None:
            core.on_crew_kickoff(event, "started")

        @crewai_event_bus.on(CrewKickoffCompletedEvent)
        def _on_crew_completed(source: Any, event: Any) -> None:
            core.on_crew_kickoff(event, "completed")

        @crewai_event_bus.on(CrewKickoffFailedEvent)
        def _on_crew_failed(source: Any, event: Any) -> None:
            core.on_crew_kickoff(event, "failed")

        @crewai_event_bus.on(LLMCallStartedEvent)
        def _on_llm_started(source: Any, event: Any) -> None:
            core.on_llm_call(event, "started")

        @crewai_event_bus.on(LLMCallCompletedEvent)
        def _on_llm_completed(source: Any, event: Any) -> None:
            core.on_llm_call(event, "completed")
