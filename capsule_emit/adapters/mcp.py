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
    # ── Toolset digest (ext.mcp) ──────────────────────────────────────────
    # A capsule proves what the agent DID, not which tool descriptions the
    # model was shown at decision time — a server that swaps a tool's
    # description after gaining trust is otherwise invisible in the record.
    #
    # Call emitter.capture_toolset(tools) once per session (right after the
    # server registers its tools — ``await app.list_tools()`` for FastMCP)
    # and again whenever the toolset changes.  Every capsule emitted after
    # that carries ``ext.mcp.toolset_digest`` — the same value while the
    # toolset is stable; a mid-session swap is a visible digest change
    # between adjacent capsules in the chain. See
    # docs/extensions/mcp-toolset-digest.md for the exact digest context.
    #
    #   tools = await app.list_tools()
    #   emitter.capture_toolset(tools)
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
import logging
import os
import platform
import socket
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from agent_action_capsule.canonical import FloatInDigestError, UnsafeIntegerError, jcs, json_digest, normalize

from ..gate import GateBlockedError, run_gate
from ._base import CapsuleEmitterBase

_log = logging.getLogger(__name__)

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
# Toolset digest (ext.mcp) — the manifest AS PRESENTED TO THE MODEL.
#
# Digest context (see docs/extensions/mcp-toolset-digest.md for the full
# spec): each manifest entry projects to exactly {name, description,
# input_schema} — no titles, annotations, output schema, or metadata — then
# the projected list is sorted by name (order-independent: reordering the
# manifest does not move the digest, only content changes do) and digested
# with JSON-DIGEST (RFC 8785 JCS + SHA-256), the same canonicalization used
# for capsule_id and I/O digests elsewhere in this library.
# ---------------------------------------------------------------------------


def _tool_field(tool: Any, *names: str) -> Any:
    """Read the first present field from *tool* — a dict or an object (e.g.
    ``mcp.types.Tool``) — trying each name in *names* in order."""
    if isinstance(tool, dict):
        for name in names:
            if name in tool:
                return tool[name]
        return None
    for name in names:
        if hasattr(tool, name):
            return getattr(tool, name)
    return None


def _project_tool(tool: Any) -> dict[str, Any]:
    """Project one manifest entry to the digest context: name + description
    + input schema.  Nothing else enters the digest.
    """
    name = _tool_field(tool, "name")
    if not isinstance(name, str) or not name:
        raise ValueError("MCP tool manifest entry is missing a required 'name'")
    description = _tool_field(tool, "description") or ""
    schema = _tool_field(tool, "inputSchema", "input_schema") or {}
    return {"name": name, "description": description, "input_schema": schema}


def _project_toolset(tools: Iterable[Any]) -> list[dict[str, Any]]:
    """Canonical projection of a tool manifest — sorted by name."""
    return sorted((_project_tool(t) for t in tools), key=lambda t: t["name"])


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
        anchor: Fire-and-forget anchor on every emit. ``None`` (default) resolves
            via ``CAPSULE_ANCHOR`` (defaulting to on when unset) — see
            ``capsule_emit.core._emit_capsule``'s ``anchor`` param.
            Pass ``anchor=False`` for offline/sandbox use.  Never poke
            ``emitter._anchor`` directly.
        anchor_url: Override the anchor endpoint.
        anchor_wait: When set, block up to this many seconds per tool call for
            the real anchor outcome, so ``.last.anchored`` / ``.anchor_status``
            reflect a genuine confirmed/failed result instead of the default
            non-blocking "submitted". ``None`` (default) never blocks.
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
        seal_reads: When ``False``, tools explicitly decorated with
            ``action_type="fyi"`` are skipped entirely — no capsule emitted,
            no anchor call.  This matches the gateway pattern of passing
            queries un-sealed, for decorator-based stacks that want parity.
            Default ``True`` (backward-compatible: every wrapped call seals).

            The skip fires only on *explicit* ``"fyi"`` labels.  A tool with
            no ``action_type`` (unknown) is still sealed — unknown defaults to
            gated (fail-safe), never silently dropped.
        emit_manifest_artifact: When ``True`` (default), the first
            ``capture_toolset()`` call and every call that changes the
            digest write the canonical manifest bytes to disk as openable
            evidence (see ``manifest_artifact_dir``).  ``False`` keeps the
            digest + typed reference visible in every capsule while
            withholding the bytes ("digest visible, bytes withheld").
        manifest_artifact_dir: Directory for manifest artifact files.
            Defaults to a sibling of the ledger file
            (``<ledger-stem>.mcp-manifests/``).
    """

    def __init__(
        self,
        *,
        operator: str,
        developer: str,
        ledger: str | os.PathLike = "ledger.jsonl",
        anchor: bool | None = None,
        anchor_url: str | None = None,
        anchor_wait: float | None = None,
        model: dict[str, str] | None = None,
        action_type: str | None = None,
        host_provenance: bool = False,
        seal_reads: bool = True,
        emit_manifest_artifact: bool = True,
        manifest_artifact_dir: str | os.PathLike | None = None,
    ) -> None:
        super().__init__(
            operator=operator,
            developer=developer,
            ledger=ledger,
            anchor=anchor,
            anchor_url=anchor_url,
            anchor_wait=anchor_wait,
            model=model,
        )
        self._default_action_type = action_type
        self._host_provenance = host_provenance
        self._seal_reads = seal_reads
        self._emit_manifest_artifact = emit_manifest_artifact
        self._manifest_artifact_dir = manifest_artifact_dir
        self._toolset_digest: str | None = None
        self._toolset_ref: dict[str, str] | None = None

    @property
    def toolset_digest(self) -> str | None:
        """The current tool-manifest digest, or ``None`` before the first
        ``capture_toolset()`` call."""
        return self._toolset_digest

    def capture_toolset(self, tools: Iterable[Any]) -> str:
        """Register the tool manifest as presented to the model.

        Computes a digest over the canonical projection of *tools* (name +
        description + input schema; see docs/extensions/mcp-toolset-digest.md
        for the exact digest context) and carries it as
        ``ext.mcp.toolset_digest`` in every capsule emitted from this point
        on — the same value while the toolset is stable.

        Call once per session, right after the server registers its tools
        (``tools = await app.list_tools()`` for FastMCP), and again whenever
        the toolset changes.  A mid-session change shows up as a digest
        change between adjacent capsules in the chain — no other capsule
        field moves.

        When ``emit_manifest_artifact=True`` (constructor default, see
        class docstring), a call that changes the digest (including the
        first call) also writes the canonical manifest bytes to disk as
        openable evidence.  ``emit_manifest_artifact=False`` keeps the
        digest + typed reference visible without ever writing bytes.

        Returns the toolset digest (hex).
        """
        projected = _project_toolset(tools)
        digest = json_digest(projected)
        changed = digest != self._toolset_digest
        self._toolset_digest = digest
        self._toolset_ref = {
            "type": "MCPToolManifest",
            "digest_alg": "SHA-256",
            "digest": digest,
        }
        if changed and self._emit_manifest_artifact:
            self._write_manifest_artifact(digest, projected)
        return digest

    def _manifest_dir(self) -> Path:
        if self._manifest_artifact_dir is not None:
            return Path(self._manifest_artifact_dir)
        ledger_path = Path(self._ledger)
        return ledger_path.parent / f"{ledger_path.stem}.mcp-manifests"

    def _write_manifest_artifact(self, digest: str, projected: list[dict[str, Any]]) -> Path:
        """Write the exact canonical (JCS) bytes that hash to *digest*.

        A verifier who obtains this file can recompute SHA-256 over its
        raw bytes and compare directly against ``ext.mcp.toolset_digest`` —
        no re-serialization step, no ambiguity about whitespace or key order.
        """
        directory = self._manifest_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.json"
        if not path.exists():
            path.write_bytes(jcs(normalize(projected)))
        return path

    def _ext_mcp_block(self) -> dict[str, Any] | None:
        if self._toolset_digest is None:
            return None
        return {
            "toolset_digest": self._toolset_digest,
            "digest_alg": "SHA-256",
            "manifest_ref": self._toolset_ref,
        }

    def tool(
        self,
        action: str | None = None,
        *,
        effect_type: str | None = None,
        verdict: str = "executed",
        action_type: str | None = None,
        model: dict[str, str] | None = None,
        constraints: list | None = None,
        on_block: Any = None,
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
            constraints: Optional list of :class:`~capsule_emit.gate.Constraint`
                objects.  When provided, the gate runs after the tool call.
                All-pass → emit with ``verdict="executed"`` and
                ``gate_checks`` in ``compute_attestation``.
                Any-fail + ``on_block`` → call ``on_block(action, gate_result)``
                and emit with ``verdict="blocked"``.
                Any-fail, no ``on_block`` → raise
                :class:`~capsule_emit.gate.GateBlockedError`.
                ``None`` (default): existing path, 100% unchanged.
            on_block: Optional escalation callback when a gated tool is
                blocked.  Signature: ``(action: str, gate_result: GateResult)
                -> None``.  Only consulted when ``constraints`` is provided.
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
                ext_mcp = self._ext_mcp_block()
                if ext_mcp is not None:
                    extra["ext.mcp"] = ext_mcp
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

            _skip_reads = not self._seal_reads and _atype == "fyi"

            def _safe_emit(action_name: str, emit_kwargs: dict) -> None:
                """Emit a capsule; transient emit errors are warned, never propagated.

                The record layer must never crash the tool call for transient
                failures.  However, producer errors (FloatInDigestError,
                UnsafeIntegerError) are structural: the capsule cannot be sealed
                regardless of retries, and silently dropping it would produce a
                ledger that is empty for an undiagnosable reason.  These propagate
                immediately so the caller gets an emission-time error naming the
                rejected field rather than a downstream crash on an empty list.
                """
                try:
                    self.emit_capsule(action_name, **emit_kwargs)
                except (FloatInDigestError, UnsafeIntegerError):
                    raise  # producer error: propagate at emission time, don't suppress
                except Exception as exc:
                    msg = f"capsule-emit: failed to seal capsule for '{action_name}': {exc}"
                    warnings.warn(msg, RuntimeWarning, stacklevel=4)
                    _log.warning(msg, exc_info=exc)

            def _emit_with_gate(
                args: tuple, kwargs: dict, output: Any
            ) -> None:
                """Run gate checks and emit the appropriate capsule verdict."""
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
                ext_mcp = self._ext_mcp_block()
                if ext_mcp is not None:
                    extra["ext.mcp"] = ext_mcp

                gate_result = run_gate(constraints, tool_input, output)
                gate_checks = gate_result.to_gate_checks()
                extra["gate_checks"] = gate_checks

                if gate_result.passed:
                    self.emit_capsule(
                        _action,
                        tool_input=tool_input,
                        tool_output=output,
                        verdict="executed",
                        effect={"type": _effect_type, "status": "dispatched"},
                        model=model,
                        runtime="mcp",
                        action_type=_atype,
                        extra_compute=extra,
                    )
                    return

                # Gate blocked.
                if on_block is not None:
                    on_block(_action, gate_result)
                    self.emit_capsule(
                        _action,
                        tool_input=tool_input,
                        tool_output=output,
                        verdict="blocked",
                        effect={"type": _effect_type, "status": "planned"},
                        model=model,
                        runtime="mcp",
                        action_type=_atype,
                        extra_compute=extra,
                    )
                    return

                raise GateBlockedError(_action, gate_result)

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    output = await fn(*args, **kwargs)
                    if not _skip_reads:
                        if constraints is not None:
                            _emit_with_gate(args, kwargs, output)
                        else:
                            _safe_emit(_action, _build_emit_args(args, kwargs, output))
                    return output

                return async_wrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                output = fn(*args, **kwargs)
                if not _skip_reads:
                    if constraints is not None:
                        _emit_with_gate(args, kwargs, output)
                    else:
                        _safe_emit(_action, _build_emit_args(args, kwargs, output))
                return output

            return wrapper

        return decorator
