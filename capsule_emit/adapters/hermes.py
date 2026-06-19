# SPDX-License-Identifier: BSD-3-Clause
"""Thin Hermes shell over CapsuleEmitterBase (~15 lines of adapter logic).

    from capsule_emit.adapters.hermes import HermesCapsuleEmitter

    emitter = HermesCapsuleEmitter(operator="acme-co", developer="my-agent@v1")

    # Call around a Hermes tool execution:
    result = execute_tool(tool_name, inputs)
    cap = emitter.after_tool(tool_name, inputs, result)
"""
from __future__ import annotations

from typing import Any

from ._base import CapsuleEmitterBase
from capsule_emit.core import EmitResult

__all__ = ["HermesCapsuleEmitter"]


class HermesCapsuleEmitter(CapsuleEmitterBase):
    """Hermes adapter — emit capsules around Hermes tool calls."""

    def after_tool(
        self,
        tool_name: str,
        tool_input: Any = None,
        tool_output: Any = None,
        *,
        verdict: str = "executed",
        effect_status: str = "dispatched",
    ) -> EmitResult:
        """Call after a Hermes tool execution to emit a capsule."""
        return self.emit_capsule(
            tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            verdict=verdict,
            effect={"type": tool_name, "status": effect_status},
        )
