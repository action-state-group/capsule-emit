# SPDX-License-Identifier: Apache-2.0
"""MCP-first capsule adapter (capsule-emit primary adapter).

Wraps an MCP tool function so every call emits a sealed, anchored capsule.
No MCP SDK dependency required — the wrapper works with any callable.

    from capsule_emit.adapters.mcp import MCPCapsuleEmitter

    emitter = MCPCapsuleEmitter(operator="acme-co", developer="my-agent@v1")

    # Decorate a tool function — capsule emitted on every call.
    @emitter.tool("write_po")
    def write_po(vendor: str, total: float) -> dict:
        ...

    # Or wrap ad-hoc after a call:
    result = write_po(vendor="Frobozz", total=1240.19)
    cap = emitter.emit_capsule("write_po", tool_input={...}, tool_output=result)
"""
from __future__ import annotations

import functools
from typing import Any, Callable

from ._base import CapsuleEmitterBase

__all__ = ["MCPCapsuleEmitter"]


class MCPCapsuleEmitter(CapsuleEmitterBase):
    """MCP-first adapter: wrap tool callables; emit a capsule per call.

    Designed for MCP tool endpoints but works with any Python callable.
    """

    def tool(
        self,
        action: str | None = None,
        *,
        effect_type: str | None = None,
        verdict: str = "executed",
    ) -> Callable:
        """Decorator: wraps a tool function and emits a capsule on each call.

        Args:
            action: Action name for the capsule (defaults to the function name).
            effect_type: Effect type string (defaults to *action*).
            verdict: Disposition verdict_class (default ``"executed"``).
        """

        def decorator(fn: Callable) -> Callable:
            _action = action or fn.__name__
            _effect_type = effect_type or _action

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                output = fn(*args, **kwargs)
                tool_input = kwargs if kwargs else (args[0] if len(args) == 1 else args)
                self.emit_capsule(
                    _action,
                    tool_input=tool_input,
                    tool_output=output,
                    verdict=verdict,
                    effect={"type": _effect_type, "status": "dispatched"},
                )
                return output

            return wrapper

        return decorator
