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

2. emitter.record_approval_response() — the native hook flow.  A
   Hooks(before_tool_call=...) callback returns RequireApproval; the runtime
   publishes ApprovalRequiredEvent and suspends; the human's answer is delivered
   by DurableAgent.raise_approval_event(instance_id, approval_request_id,
   approved, reason, approver_token).  Call record_approval_response() right
   next to that call, with the same arguments — that is the only point where a
   real decision exists.  Never from inside the hook.
   emitter.record_hitl() is the lower-level form for any other gate (e.g. a
   hand-rolled ctx.wait_for_external_event()).  Both emit action_type="decide"
   with a REAL disposition block (actual approver id, actual accept/reject
   outcome).  NEVER fabricate a disposition — only call after the human acted.

─── Namespaced payload extension ────────────────────────────────────────────
Every capsule from this adapter carries a "dapr_agents" block in
compute_attestation containing: agent_name, workflow_instance_id, tool_name,
app_id.  Values are exact decimal strings per §5.1; the block is committed to
capsule_id; receivers that do not recognise it MUST ignore it (Class-1).

─── LIMITATIONS (open questions for Dapr Agents maintainers) ────────────────
The following assumptions were made against the v1.0 documented API.  Each is
a question for Dapr Agents maintainers before treating this adapter as verified
against a live sidecar:

L1. Before/after tool callback surface: dapr-agents >= 1.0.5 ships
    dapr_agents.hooks (Hooks / ToolHookContext / RequireApproval / Deny ...),
    registered via DurableAgent(hooks=Hooks(...)).  This adapter still wraps
    tools at definition time for the fyi record (only Python-defined tools;
    MCP/OpenAPI-sourced tools are seen by the hook seam, not by the decorator).
    The HITL decide record IS wired to the native flow — see
    record_approval_response() above.  Dapr's own docs called after_tool_call
    "reserved API surface... not yet dispatched" at 1.0.5; the 1.0.6 pass that
    verified the approval flow did not re-check that hook.

L2. Workflow instance ID inside a tool: The workflow instance ID is a Dapr
    Workflow concept.  It is NOT documented as available inside a synchronous
    tool function at v1.0.  Currently passed at emitter construction or
    per-call.  If the Python SDK exposes a context carrier inside tool
    execution (e.g. via contextvars), the emitter could auto-capture it.

L3. HITL approver identity: neither the hook context (ToolHookContext:
    step_name, step_kind, source, payload, tool_call_id) nor the native
    ApprovalResponseEvent carries a verified approver — raise_approval_event()
    takes an optional raw approver_token that dapr-agents passes through
    unvalidated, and approver_subject is populated only by a caller-supplied
    plugin (always None as built by dapr-agents itself).  Callers must resolve
    the token in their own auth layer and pass the result as approver_id.
    The workflow instance id likewise lives on the workflow context
    (ctx.instance_id) / the raise_approval_event() call, not on the hook
    context.

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
        anchor: Legacy, non-default fire-and-forget anchor channel (default
            False; pass True or set CAPSULE_ANCHOR=legacy-on to opt in).
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
        approval_request_id: str | None = None,
        tool_call_id: str | None = None,
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
        if approval_request_id:
            ext["approval_request_id"] = str(approval_request_id)
        if tool_call_id:
            ext["tool_call_id"] = str(tool_call_id)
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
        approval_request_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> EmitResult:
        """Record a HITL (Human-In-The-Loop) decision as a decide capsule.

        This is the low-level form (any approval mechanism, including a hand-rolled
        ``ctx.wait_for_external_event`` gate). For Dapr Agents' native hook flow —
        ``Hooks(before_tool_call=...)`` returning ``RequireApproval``, answered by
        ``DurableAgent.raise_approval_event(...)`` — use :meth:`record_approval_response`,
        which takes that call's own arguments.

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
            approval_request_id: Dapr Agents' ``approval_request_id`` for the
                gate this decision answers (optional; sealed in the extension).
            tool_call_id: The LLM-assigned ``tool_call_id`` of the gated call
                (optional; sealed in the extension).

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
            approval_request_id=approval_request_id,
            tool_call_id=tool_call_id,
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

    def record_approval_response(
        self,
        action: str,
        *,
        instance_id: str,
        approval_request_id: str,
        approved: bool,
        approver_id: str,
        reason: str | None = None,
        tool_request: Any = None,
        tool_call_id: str | None = None,
        agent_name: str | None = None,
        app_id: str | None = None,
        prior_capsule_id: str | None = None,
    ) -> EmitResult:
        """Record a decision delivered through Dapr Agents' native approval flow.

        The idiomatic HITL path in dapr-agents >= 1.0.5 (verified on 1.0.6) is: a ``before_tool_call``
        hook returns ``RequireApproval``; the runtime publishes an
        ``ApprovalRequiredEvent`` (``approval_request_id``, ``instance_id``,
        ``step_name``, ``tool_call_id``, ``tool_arguments``) and suspends; the human
        answers and your approval service calls
        ``DurableAgent.raise_approval_event(instance_id, approval_request_id,
        approved, reason, approver_token)``. **That call is the seam** — the only
        point where a real decision exists — so call this method right next to it,
        with the same arguments, *after* the human has acted. Never from inside the
        hook: at ``before_tool_call`` time no decision has been made.

        The hook context (``ToolHookContext``) carries no workflow id and no
        approver identity; both come from elsewhere. ``instance_id`` is the same
        value you pass to ``raise_approval_event``. ``approver_id`` must be an
        identity **your** auth layer has verified — dapr-agents passes
        ``approver_token`` through unvalidated and its ``approver_subject`` is only
        populated by a caller-supplied plugin — so resolve the token to a subject
        before calling; this method never derives one.

        Args:
            action: Action name for the capsule (e.g. the gated tool's ``step_name``).
            instance_id: The Dapr Workflow instance the decision resumes.
            approval_request_id: The gate's id from ``ApprovalRequiredEvent``.
            approved: The human's actual decision (``True`` → ``accept``,
                ``False`` → ``reject``).
            approver_id: The verified approver identity (see above).
            reason: The human's stated reason, if any (digest-committed as outcome).
            tool_request: The gated call's arguments (``ApprovalRequiredEvent.tool_arguments``),
                if you want them committed.
            tool_call_id: The gated call's ``tool_call_id``.
            agent_name / app_id / prior_capsule_id: as :meth:`record_hitl`.

        Note on effect status (inherited from :meth:`record_hitl`): approval
        seals effect status ``dispatched``, rejection seals ``planned`` with
        verdict ``blocked``. ``capsule_emit.approval.list_pending`` treats a
        ``blocked``/``planned`` record as awaiting resolution, so a definitive
        human "no" still shows as pending there until a resolving record
        follows; the disposition block itself is unambiguous.
        """
        outcome: dict[str, Any] = {"approved": bool(approved)}
        if reason is not None:
            outcome["reason"] = str(reason)
        return self.record_hitl(
            action,
            approver_id=approver_id,
            decision="accept" if approved else "reject",
            tool_request=tool_request,
            outcome=outcome,
            workflow_instance_id=instance_id,
            agent_name=agent_name,
            app_id=app_id,
            prior_capsule_id=prior_capsule_id,
            approval_request_id=approval_request_id,
            tool_call_id=tool_call_id,
        )
