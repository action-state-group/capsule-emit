# SPDX-License-Identifier: Apache-2.0
"""Dapr Agents adapter — per-action capsules at the agent's decision points.

This adapter records capsules at two distinct seam points in a Dapr Agents
workflow — the *live* decision layer, not the post-hoc execution record that
the capsule-emit-dapr Go adapter produces from signed workflow history.

    from capsule_emit.adapters.dapr_agents import DaprAgentsCapsuleEmitter

    emitter = DaprAgentsCapsuleEmitter(
        operator="acme-co",
        developer="invoice-agent@v1",
        agent_name="invoice-checker",
        app_id="invoice-app",
    )

    @emitter.tool("check_invoice")
    def check_invoice(invoice_id: str, amount: float) -> dict:
        ...                          # tool runs; capsule emitted after (fyi)

    # After ctx.wait_for_external_event() resolves:
    emitter.record_hitl(
        "approve_payment",
        approver_id="alice@example.com",
        decision="accept",              # or "reject" — as it happened
        tool_request={"invoice_id": "INV-001", "amount": "1240.00"},
        outcome={"approved_at": "2026-07-28T10:00:00Z"},
        workflow_instance_id="wf-abc123",
    )

─── Layer distinction ────────────────────────────────────────────────────────
capsule-emit-dapr (Go) = post-hoc execution records extracted from signed
  Dapr Workflow history after the run completes.
THIS adapter = per-action records captured LIVE at each decision point as the
  workflow executes — tool calls and HITL approval gates.

─── Integration surfaces ─────────────────────────────────────────────────────
1. @emitter.tool() — decorator on any tool callable.  Each invocation emits
   one capsule with action_type="fyi": the adapter observes what the agent
   called; the LLM's upstream decision is not visible at this layer.

2. emitter.record_hitl() — call after ctx.wait_for_external_event() resolves
   to record the HITL outcome.  Emits action_type="decide" with a REAL
   disposition block (actual approver id, actual accept/reject outcome).
   NEVER fabricate a disposition — only call this after the human has acted.

─── Namespaced payload extension ────────────────────────────────────────────
Every capsule from this adapter carries a "dapr_agents" block in
compute_attestation containing: agent_name, workflow_instance_id, tool_name,
app_id.  Values are exact decimal strings per §5.1; the block is committed to
capsule_id; receivers that do not recognise it MUST ignore it (Class-1).

─── LIMITATIONS (open questions for Dapr Agents maintainers) ────────────────
The following assumptions were made against the v1.0 documented API.  Each is
a question for Dapr Agents maintainers before treating this adapter as verified
against a live sidecar:

L1. Before/after tool callback surface: No stable before/after hook was found
    in the v1.0 docs at the agent level.  This adapter wraps at function
    definition time.  If Dapr Agents exposes an equivalent callback (e.g.
    agent middleware or lifecycle hooks), consider switching to that surface.

L2. Workflow instance ID inside a tool: The workflow instance ID is a Dapr
    Workflow concept.  It is NOT documented as available inside a synchronous
    tool function at v1.0.  Currently passed at emitter construction or
    per-call.  If the Python SDK exposes a context carrier inside tool
    execution (e.g. via contextvars), the emitter could auto-capture it.

L3. HITL approver identity: ctx.wait_for_external_event() returns the raw
    event payload.  The authenticated identity of the approver is NOT part of
    the standardised payload schema.  Callers must supply approver_id from
    their own auth layer (e.g. from the HTTP handler that raised the event).

L4. Workflow replay / activity idempotency: Dapr Workflow may replay activity
    functions on failure.  A wrapped tool firing on replay emits a duplicate
    capsule with the same inputs.  If idempotency is required, deduplicate
    downstream using (agent_name, workflow_instance_id, tool_name).

L5. App ID auto-discovery: The Dapr sidecar app-id is not surfaced inside a
    tool call by the Python SDK at v1.0.  Must be supplied at construction.

L6. HITL event schema: The payload of wait_for_external_event() is
    user-defined.  Callers are responsible for extracting approver_id and
    decision from it before passing them to record_hitl().

L7. Async activity support: Dapr Workflow activities are typically sync
    functions.  This adapter supports both sync and async callables.  Verify
    that async tool functions are supported in your Dapr Agents version.

─── Emit-error policy ───────────────────────────────────────────────────────
On @emitter.tool(): a failed emit is warned (RuntimeWarning) and logged, never
propagated — the record layer must not crash the tool call.
On record_hitl(): emit errors propagate normally; a failed HITL record is not
silently dropped because HITL decisions are always consequential.
"""
from __future__ import annotations

import functools
import inspect
import logging
import warnings
from typing import Any

from ..core import EmitResult
from ._base import CapsuleEmitterBase

_log = logging.getLogger(__name__)

__all__ = ["DaprAgentsCapsuleEmitter"]


def _bind_inputs(sig: inspect.Signature, args: tuple, kwargs: dict) -> Any:
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return kwargs if kwargs else (args[0] if len(args) == 1 else args or {})


class DaprAgentsCapsuleEmitter(CapsuleEmitterBase):
    """Dapr Agents adapter — capsules at tool-call and HITL decision points.

    Args:
        operator: Accountable tenant / org identifier.
        developer: Agent name + version string (e.g. "invoice-agent@v1").
        agent_name: The Dapr Agents agent name; stored in the dapr_agents
            extension of every capsule.  Can be overridden per-call.
        app_id: The Dapr sidecar app-id; stored in the extension.  Must be
            supplied by the caller (see L5 in LIMITATIONS above).
        workflow_instance_id: Default workflow instance id committed to the
            dapr_agents extension.  Can be overridden per-call.  Not
            auto-captured from the Python SDK at v1.0 (see L2 in LIMITATIONS).
        ledger: Path to the JSONL ledger file (default: "ledger.jsonl").
        anchor: Fire-and-forget anchor on every emit (default True).
        anchor_url: Override the anchor endpoint.
        model: Default {"provider": ..., "model_id": ...} for all capsules.
    """

    def __init__(
        self,
        *,
        operator: str,
        developer: str,
        agent_name: str | None = None,
        app_id: str | None = None,
        workflow_instance_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(operator=operator, developer=developer, **kwargs)
        self._agent_name = agent_name
        self._app_id = app_id
        self._workflow_instance_id = workflow_instance_id

    def _dapr_ext(
        self,
        tool_name: str,
        *,
        agent_name: str | None = None,
        workflow_instance_id: str | None = None,
        app_id: str | None = None,
        approver_id: str | None = None,
    ) -> dict[str, Any] | None:
        ext: dict[str, str] = {}
        name = agent_name or self._agent_name
        wf_id = workflow_instance_id or self._workflow_instance_id
        aid = app_id or self._app_id
        if name:
            ext["agent_name"] = str(name)
        ext["tool_name"] = str(tool_name)
        if wf_id:
            ext["workflow_instance_id"] = str(wf_id)
        if aid:
            ext["app_id"] = str(aid)
        if approver_id:
            ext["approver_id"] = str(approver_id)
        return {"dapr_agents": ext}

    def tool(
        self,
        action: str | None = None,
        *,
        effect_type: str | None = None,
        agent_name: str | None = None,
        workflow_instance_id: str | None = None,
        app_id: str | None = None,
        prior_capsule_id: str | None = None,
    ) -> Any:
        """Decorator: wraps a Dapr Agents tool callable; emits a capsule per call.

        Emits action_type="fyi" — the adapter observes what the agent invoked;
        the LLM's upstream decision is not visible at this seam.  The effect
        block is populated from the tool result with status="dispatched".

        Works with both sync and async def functions.  Emit errors are warned
        and logged, never propagated — the tool always returns normally.

        Args:
            action: Action name for the capsule.  Defaults to fn.__name__.
            effect_type: Effect type string.  Defaults to action.
            agent_name: Per-decoration override for the agent_name extension field.
            workflow_instance_id: Per-decoration override for the workflow id field.
            app_id: Per-decoration override for the app_id extension field.
            prior_capsule_id: Optional capsule_id to chain this tool's capsule
                to (e.g. a preceding HITL decide capsule) — chains the whole
                run, not just fyi-after-fyi.  Static per decoration; for a
                chain built across multiple live calls, decorate right before
                the call once the prior capsule_id is known.
        """

        def decorator(fn: Any) -> Any:
            _action = action or fn.__name__
            _etype = effect_type or _action
            sig = inspect.signature(fn)

            def _emit_after(args: tuple, kwargs: dict, output: Any) -> None:
                tool_input = _bind_inputs(sig, args, kwargs)
                extra = self._dapr_ext(
                    _action,
                    agent_name=agent_name,
                    workflow_instance_id=workflow_instance_id,
                    app_id=app_id,
                )
                try:
                    self.emit_capsule(
                        _action,
                        tool_input=tool_input,
                        tool_output=output,
                        verdict="executed",
                        effect={"type": _etype, "status": "dispatched"},
                        action_type="fyi",
                        runtime="dapr_agents",
                        extra_compute=extra,
                        prior_capsule_id=prior_capsule_id,
                    )
                except Exception as exc:
                    msg = (
                        f"capsule-emit: failed to seal capsule for "
                        f"'{_action}': {exc}"
                    )
                    warnings.warn(msg, RuntimeWarning, stacklevel=4)
                    _log.warning(msg, exc_info=exc)

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    output = await fn(*args, **kwargs)
                    _emit_after(args, kwargs, output)
                    return output

                return async_wrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                output = fn(*args, **kwargs)
                _emit_after(args, kwargs, output)
                return output

            return wrapper

        return decorator

    def record_hitl(
        self,
        action: str,
        *,
        approver_id: str,
        decision: str,
        tool_request: Any = None,
        outcome: Any = None,
        workflow_instance_id: str | None = None,
        agent_name: str | None = None,
        app_id: str | None = None,
        prior_capsule_id: str | None = None,
    ) -> EmitResult:
        """Record a HITL (Human-In-The-Loop) decision as a decide capsule.

        Call this AFTER ctx.wait_for_external_event() resolves — only once the
        human has actually approved or rejected.  Never call with fabricated
        data; the capsule commits the disposition to the tamper-evident log.

        Args:
            action: Action name for the capsule (e.g. "approve_payment").
            approver_id: The authenticated identity of the human approver
                (e.g. "alice@example.com").  Stored in the dapr_agents
                extension.  See L3 in LIMITATIONS — supply from your auth
                layer; do not invent a default.
            decision: The actual outcome as it happened: "accept" to record
                that the human approved, "reject" to record a rejection.
                Never pass a default — only call after the event resolves.
            tool_request: The payload the agent sent for human review
                (optional).  Digest-committed; never leaves the process.
            outcome: The outcome payload returned by the external event or
                by your approval service (optional).  Digest-committed.
            workflow_instance_id: Dapr Workflow instance id.  Overrides the
                emitter-level default for this capsule.
            agent_name: Agent name override for this capsule.
            app_id: App-id override for this capsule.
            prior_capsule_id: Optional capsule_id of the preceding tool-call
                fyi capsule to chain this decide capsule to it.

        Returns:
            EmitResult with .capsule_id and .anchored.

        Raises:
            ValueError: If decision is not "accept" or "reject".
        """
        if decision not in ("accept", "reject"):
            raise ValueError(
                f"decision must be 'accept' or 'reject', got {decision!r}. "
                "Pass the actual human outcome — never fabricate a disposition."
            )

        verdict = "executed" if decision == "accept" else "blocked"
        effect_status = "dispatched" if decision == "accept" else "planned"

        extra = self._dapr_ext(
            action,
            agent_name=agent_name,
            workflow_instance_id=workflow_instance_id,
            app_id=app_id,
            approver_id=approver_id,
        )

        return self.emit_capsule(
            action,
            tool_input=tool_request,
            tool_output=outcome,
            verdict=verdict,
            effect={"type": action, "status": effect_status},
            action_type="decide",
            human_disposed=True,
            approver="human",
            decision=decision,
            runtime="dapr_agents",
            extra_compute=extra,
            prior_capsule_id=prior_capsule_id,
        )
