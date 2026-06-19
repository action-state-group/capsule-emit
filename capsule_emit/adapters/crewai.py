# SPDX-License-Identifier: Apache-2.0
"""Thin CrewAI shell over CapsuleEmitterBase (~15 lines of adapter logic).

    from capsule_emit.adapters.crewai import CrewAICapsuleEmitter

    emitter = CrewAICapsuleEmitter(operator="acme-co", developer="my-agent@v1")
    wrapped_tool = emitter.wrap(my_crewai_tool)

Works without installing crewai — ``wrap()`` is framework-free.
"""
from __future__ import annotations

import functools
from typing import Any, Callable

from ._base import CapsuleEmitterBase

__all__ = ["CrewAICapsuleEmitter"]


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
