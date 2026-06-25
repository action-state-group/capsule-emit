# SPDX-License-Identifier: Apache-2.0
"""MCP-first capsule adapter (capsule-emit primary adapter).

Wraps an MCP tool function so every call emits a sealed, anchored capsule.
No MCP SDK dependency required — the wrapper works with any callable.

    from capsule_emit.adapters.mcp import MCPCapsuleEmitter

    emitter = MCPCapsuleEmitter(
        operator="acme-co",
        developer="po-agent@v1",
        anchor=False,           # True (default) → fire-and-forget digest anchor
        action_type="act",      # default for all tools (MCP tools do things)
    )

    # ── Decorator order ──────────────────────────────────────────────────
    # @framework.tool() on top, @emitter.tool() directly on the function.
    # functools.wraps preserves the signature so the framework's schema
    # generator still sees the real param names and types.
    #
    #   @server.tool()           # outermost — introspects the wrapped fn
    #   @emitter.tool()          # innermost — wraps the real fn
    #   async def write_po(vendor: str, total: float) -> dict:
    #       ...
    #
    # ── Name inference ───────────────────────────────────────────────────
    # @emitter.tool() with no arguments infers the action name from
    # fn.__name__.  Explicit name: @emitter.tool("my_action").
    #
    # ── runtime="mcp" ────────────────────────────────────────────────────
    # Every capsule from this adapter carries runtime="mcp" in
    # compute_attestation automatically.  No extra config needed.
    #
    # ── model= (dev-supplied, NOT auto-captured) ─────────────────────────
    # The MCP adapter sees the tool boundary, not the LLM.  Pass model=
    # at construction (default for all tools) or per-decorator.  There is
    # NO automatic model capture here — what you supply is what gets sealed.
    #
    #   emitter = MCPCapsuleEmitter(..., model={"provider": "anthropic",
    #                                           "model_id": "claude-sonnet-4-6"})
    #   # or per tool:
    #   @emitter.tool(model={"provider": "openai", "model_id": "gpt-4o"})
    #   def my_tool(...): ...
    #
    # ── action_type ──────────────────────────────────────────────────────
    # The spec allows two values (§5.1):
    #   "decide" — consequential; records a gate decision or tool execution.
    #   "fyi"    — passive observation; the adapter tier records what happened.
    # Default (None) auto-derives from verdict: "executed"/"confirmed"/etc →
    # "decide"; other → "fyi".  For MCP tools with verdict="executed" the
    # auto-derived value is "decide" — correct for consequential tool calls.
    # Pass action_type="fyi" at construction or per-tool for observation-only
    # or read-only tools that should not be marked as gate decisions.
    #
    # ── MCP Context provenance ───────────────────────────────────────────
    # If a tool parameter is typed as mcp.server.fastmcp.Context the
    # adapter automatically extracts request_id, client_id, and clientInfo
    # (name/version) and stores them in compute_attestation.  The Context
    # param is excluded from the input digest (it is infrastructure, not
    # tool input).  Degrades gracefully when mcp is not installed or when
    # called outside a real MCP request.
    #
    # ── Opt-in host provenance ───────────────────────────────────────────
    # OFF by default.  Enable with MCPCapsuleEmitter(..., host_provenance=True)
    # to capture hostname and OS platform in compute_attestation.
    #
    #   PRIVACY NOTE: host_provenance=True reveals the machine identity
    #   of the agent host in every capsule.  Only enable if that is
    #   acceptable for your deployment.
    #
    #   Strong hardware attestation (TEE/DCAP/TPM) is NOT provided here —
    #   that belongs in the CCF or gate layer, not the emit-tier.
    #
    # ── Tool-error policy ────────────────────────────────────────────────
    # If the wrapped function raises, the exception propagates immediately
    # and NO capsule is emitted.  A failed call leaves no partial ledger row.
    # To record failures, catch at the call site and call emit_capsule()
    # directly with effect={"type": ..., "status": "dispatched"}.
    #
    # ── Effect status ────────────────────────────────────────────────────
    # Capsules from @emitter.tool() carry effect.status = "dispatched"
    # (the tool ran; outcome not yet confirmed by a second party).
    # Call emit_capsule() directly with effect.status = "confirmed" once
    # you have confirmation.
    #
    # ── Anchor at construction ───────────────────────────────────────────
    # Pass anchor=True (the default) or anchor=False to the constructor.
    # Never poke emitter._anchor directly.
"""
from __future__ import annotations

import functools
import inspect
import os
import platform
import socket
from typing import Any, Callable

from ._base import CapsuleEmitterBase

__all__ = ["MCPCapsuleEmitter"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bind_inputs(sig: inspect.Signature, args: tuple, kwargs: dict) -> Any:
    """Return the complete named-argument dict for a call with *args/**kwargs.

    Uses ``sig.bind()`` so positional, mixed, and keyword-only calls all
    produce the same fully-named dict.  Falls back gracefully for variadic
    or exotic signatures.
    """
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return kwargs if kwargs else (args[0] if len(args) == 1 else args or {})


def _find_context_param(fn: Callable) -> str | None:
    """Return the name of the MCP Context parameter in *fn*'s signature, or None.

    Uses ``typing.get_type_hints()`` to resolve string annotations produced by
    ``from __future__ import annotations`` (PEP 563).  Falls back to an
    unresolved-string scan for forward-reference edge cases.  Avoids importing
    the mcp package at module level.
    """
    import typing

    # Primary: resolve annotations via the function's module globals
    try:
        hints = typing.get_type_hints(fn)
        for name, ann in hints.items():
            ann_name = getattr(ann, "__name__", "") or ""
            if ann_name == "Context":
                module = getattr(ann, "__module__", "") or ""
                if "mcp" in module or module == "":
                    return name
    except Exception:
        pass

    # Fallback: unresolved string annotations (forward refs, missing imports)
    try:
        for name, param in inspect.signature(fn).parameters.items():
            ann = param.annotation
            if isinstance(ann, str) and ann.split(".")[-1] == "Context":
                return name
    except Exception:
        pass

    return None


def _extract_mcp_context(ctx: Any) -> dict[str, Any]:
    """Safely extract provenance fields from a FastMCP Context instance.

    All attribute accesses are wrapped in try/except — the context raises
    ``ValueError`` when accessed outside a real MCP request (e.g. in tests
    that call the tool directly).  Returns an empty dict on any failure.
    """
    out: dict[str, Any] = {}
    try:
        request_id = ctx.request_id
        if request_id is not None:
            out["mcp_request_id"] = str(request_id)
    except Exception:
        pass
    try:
        client_id = ctx.client_id
        if client_id is not None:
            out["mcp_client_id"] = str(client_id)
    except Exception:
        pass
    try:
        session = ctx.session
        if session is not None:
            cp = getattr(session, "client_params", None)
            if cp is not None:
                ci = getattr(cp, "clientInfo", None)
                if ci is not None:
                    name = getattr(ci, "name", None)
                    version = getattr(ci, "version", None)
                    if name is not None:
                        out["mcp_client_name"] = str(name)
                    if version is not None:
                        out["mcp_client_version"] = str(version)
    except Exception:
        pass
    return out


def _host_block() -> dict[str, str]:
    """Return a minimal host provenance block: hostname + OS platform."""
    return {
        "host_name": socket.gethostname(),
        "host_platform": f"{platform.system()} {platform.release()}".strip(),
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MCPCapsuleEmitter(CapsuleEmitterBase):
    """MCP-first adapter: wrap tool callables; emit a capsule per call.

    Designed for MCP tool endpoints but works with any Python callable.
    Supports both sync and ``async def`` tool functions.

    Every capsule carries ``runtime="mcp"`` in ``compute_attestation``
    automatically.

    Args:
        operator: Accountable tenant / org identifier.
        developer: Agent name + version string.
        ledger: Path to the JSONL ledger file (default: ``ledger.jsonl``).
        anchor: Fire-and-forget anchor on every emit (default: True).
            Pass ``anchor=False`` for offline/sandbox use.  Never poke
            ``emitter._anchor`` directly.
        anchor_url: Override the anchor endpoint.
        model: Default ``{"provider": ..., "model_id": ...}`` applied to
            every capsule.  The MCP adapter does NOT auto-capture the model
            — what you supply is what gets sealed.  Can be overridden
            per-tool with ``@emitter.tool(model=...)``.
        action_type: Default ``action_type`` for all tools.  The spec
            (§5.1) allows ``"decide"`` (consequential action/gate decision)
            and ``"fyi"`` (passive observation).  ``None`` (default)
            auto-derives from the verdict: ``"executed"``/``"confirmed"``
            etc → ``"decide"``; other → ``"fyi"``.  Pass ``"fyi"`` for
            read-only or observation-only servers.  Override per-tool with
            ``@emitter.tool(action_type="fyi")``.
        host_provenance: When ``True``, adds ``host_name`` and
            ``host_platform`` to every capsule's ``compute_attestation``.
            Default ``False`` (no host info in capsules).

            **Privacy note:** enabling this reveals the machine identity of
            the agent host in every capsule.  Strong TEE/DCAP hardware
            attestation is NOT provided here — that belongs in the gate layer.
    """

    def __init__(
        self,
        *,
        operator: str,
        developer: str,
        ledger: str | os.PathLike = "ledger.jsonl",
        anchor: bool = True,
        anchor_url: str | None = None,
        model: dict[str, str] | None = None,
        action_type: str | None = None,
        host_provenance: bool = False,
    ) -> None:
        super().__init__(
            operator=operator,
            developer=developer,
            ledger=ledger,
            anchor=anchor,
            anchor_url=anchor_url,
            model=model,
        )
        self._default_action_type = action_type
        self._host_provenance = host_provenance

    def tool(
        self,
        action: str | None = None,
        *,
        effect_type: str | None = None,
        verdict: str = "executed",
        action_type: str | None = None,
        model: dict[str, str] | None = None,
    ) -> Callable:
        """Decorator: wraps a tool function and emits a capsule on each call.

        Works with both sync and ``async def`` functions.  For async
        functions the wrapper is also ``async``; the capsule is emitted
        *after* the coroutine resolves so the output digest is correct.

        Every capsule automatically carries ``runtime="mcp"`` in
        ``compute_attestation``.  If the tool has a FastMCP ``Context``
        parameter, its request ID and client info are also captured.

        Args:
            action: Action name for the capsule.  Defaults to the function
                name (``fn.__name__``) — no explicit name is needed for
                most tools.
            effect_type: Effect type string (defaults to *action*).
            verdict: Disposition verdict_class (default ``"executed"``).
            action_type: Override per-tool action type.  Defaults to the
                constructor ``action_type`` (``"act"``).  Use
                ``action_type="decide"`` for approval / confirmation tools.
            model: Per-tool model override.  ``None`` (default) falls back
                to the constructor ``model=``.  The adapter does NOT
                auto-capture the model; supply it explicitly.
        """

        def decorator(fn: Callable) -> Callable:
            _action = action or fn.__name__
            _effect_type = effect_type or _action
            _atype = action_type if action_type is not None else self._default_action_type
            sig = inspect.signature(fn)
            ctx_param = _find_context_param(fn)

            def _build_emit_args(args: tuple, kwargs: dict, output: Any) -> dict:
                full_input = _bind_inputs(sig, args, kwargs)
                tool_input = (
                    {k: v for k, v in full_input.items() if k != ctx_param}
                    if ctx_param else full_input
                )
                extra: dict[str, Any] = {}
                if ctx_param:
                    ctx_val = full_input.get(ctx_param)
                    if ctx_val is not None:
                        extra.update(_extract_mcp_context(ctx_val))
                if self._host_provenance:
                    extra.update(_host_block())
                return {
                    "tool_input": tool_input,
                    "tool_output": output,
                    "verdict": verdict,
                    "effect": {"type": _effect_type, "status": "dispatched"},
                    "model": model,
                    "runtime": "mcp",
                    "action_type": _atype,
                    "extra_compute": extra or None,
                }

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    output = await fn(*args, **kwargs)
                    self.emit_capsule(_action, **_build_emit_args(args, kwargs, output))
                    return output

                return async_wrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                output = fn(*args, **kwargs)
                self.emit_capsule(_action, **_build_emit_args(args, kwargs, output))
                return output

            return wrapper

        return decorator
