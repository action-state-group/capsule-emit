# SPDX-License-Identifier: Apache-2.0
"""MCP-first capsule adapter (capsule-emit primary adapter).

Wraps an MCP tool function so every call emits a sealed, anchored capsule.
No MCP SDK dependency required — the wrapper works with any callable.

    from capsule_emit.adapters.mcp import MCPCapsuleEmitter

    emitter = MCPCapsuleEmitter(
        operator="acme-co",
        developer="po-agent@v1",
        anchor=False,           # True (default) → fire-and-forget digest anchor
    )

    # ── Decorator order ──────────────────────────────────────────────────
    # @framework.tool() on top, @emitter.tool() directly on the function.
    # functools.wraps preserves the signature so the framework's schema
    # generator still sees the real param names and types.
    #
    #   @server.tool()        # outermost — introspects the wrapped fn
    #   @emitter.tool()       # innermost — wraps the real fn
    #   async def write_po(vendor: str, total: float) -> dict:
    #       ...
    #
    # ── Name inference ───────────────────────────────────────────────────
    # @emitter.tool() with no arguments infers the action name from
    # fn.__name__.  Explicit name: @emitter.tool("my_action").
    #
    # ── Tool-error policy ────────────────────────────────────────────────
    # If the wrapped function raises, the exception propagates immediately
    # and NO capsule is emitted.  A failed call leaves no partial ledger row.
    # To record failures, catch at the call site and call emit_capsule()
    # explicitly with effect={"type": ..., "status": "dispatched"}.
    #
    # ── Effect status ────────────────────────────────────────────────────
    # Capsules from @emitter.tool() carry effect.status = "dispatched"
    # (the tool ran; outcome not yet confirmed by a second party).
    # Call emit_capsule() directly with effect.status = "confirmed" once
    # you have confirmation.
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from ._base import CapsuleEmitterBase

__all__ = ["MCPCapsuleEmitter"]


def _bind_inputs(sig: inspect.Signature, args: tuple, kwargs: dict) -> Any:
    """Return the complete named-argument dict for a call with *args/**kwargs.

    Uses ``sig.bind()`` so positional, mixed, and keyword-only calls all
    produce the same fully-named dict.  Falls back to kwargs (or args as a
    last resort) when the signature doesn't match (variadic / exotic
    signatures).
    """
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return kwargs if kwargs else (args[0] if len(args) == 1 else args or {})


class MCPCapsuleEmitter(CapsuleEmitterBase):
    """MCP-first adapter: wrap tool callables; emit a capsule per call.

    Designed for MCP tool endpoints but works with any Python callable.
    Supports both sync and async (``async def``) tool functions.

    Pass ``anchor=False`` to the constructor for offline/sandbox use.
    Pass ``anchor=True`` (the default) to fire-and-forget a digest-only
    anchor submission on every emit.  Never poke ``emitter._anchor``
    directly.
    """

    def tool(
        self,
        action: str | None = None,
        *,
        effect_type: str | None = None,
        verdict: str = "executed",
    ) -> Callable:
        """Decorator: wraps a tool function and emits a capsule on each call.

        Works with both sync and ``async def`` functions.  For async
        functions the wrapper is also ``async``; the capsule is emitted
        *after* the coroutine resolves so the output digest is correct.

        Args:
            action: Action name for the capsule.  Defaults to the function
                name (``fn.__name__``) — no explicit name is needed for
                most tools.
            effect_type: Effect type string (defaults to *action*).
            verdict: Disposition verdict_class (default ``"executed"``).
        """

        def decorator(fn: Callable) -> Callable:
            _action = action or fn.__name__
            _effect_type = effect_type or _action
            sig = inspect.signature(fn)  # computed once at decoration time

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    output = await fn(*args, **kwargs)
                    tool_input = _bind_inputs(sig, args, kwargs)
                    self.emit_capsule(
                        _action,
                        tool_input=tool_input,
                        tool_output=output,
                        verdict=verdict,
                        effect={"type": _effect_type, "status": "dispatched"},
                    )
                    return output

                return async_wrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                output = fn(*args, **kwargs)
                tool_input = _bind_inputs(sig, args, kwargs)
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
