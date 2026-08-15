# Dapr Agents adapter

`DaprAgentsCapsuleEmitter` records capsules at the agent's live decision points
— not post-hoc from history.  It owns two seam points in a Dapr Agents workflow:
one for every tool call the agent makes, and one for every HITL approval gate.

```python
from capsule_emit.adapters.dapr_agents import DaprAgentsCapsuleEmitter

emitter = DaprAgentsCapsuleEmitter(
    operator="acme-co",
    developer="invoice-agent@v1",
    agent_name="invoice-checker",
    app_id="invoice-app",
    workflow_instance_id="wf-abc123",   # set per workflow run
)
```

## Layer distinction

| Adapter | When | Trigger |
|---|---|---|
| capsule-emit-dapr (Go) | After the run | Extracted from signed Dapr Workflow history |
| **This adapter** | During the run | Hooked at each tool call and HITL gate |

The Go adapter produces post-hoc execution records from the signed history.
This adapter records live at each decision point — what the agent called and
what the human decided — as it happens.

## Where to put the call

### Surface 1 — tool calls (`@emitter.tool()`)

Wrap each tool function with `@emitter.tool()`.  One capsule with
`action_type="fyi"` is emitted per invocation — the adapter observes what the
agent called; the LLM's upstream decision is not visible at this seam.

```python
@emitter.tool("check_invoice")
def check_invoice(invoice_id: str, amount: str) -> dict:
    ...                    # your tool logic unchanged
```

Works with both `def` and `async def` functions.  Emit errors are warned and
logged, never propagated — the tool always returns normally.

**Why `action_type="fyi"`?**  The adapter sees the *tool boundary*, not the
LLM that decided to call it.  Recording the call as "fyi" is honest; the
upstream decision capsule (if any) lives in the model layer, not here.

### Surface 2 — HITL approval gates (`emitter.record_hitl()`)

After `ctx.wait_for_external_event()` resolves — and only then — record the
outcome:

```python
# Inside your Dapr Workflow definition:
approval_event = ctx.wait_for_external_event("approval_event")
ctx.yield_()

# Extract approver and decision from the event payload:
approver_id = approval_event.get("approved_by")   # from YOUR auth layer
decision = approval_event.get("decision")          # "accept" or "reject"

emitter.record_hitl(
    "approve_payment",
    approver_id=approver_id,
    decision=decision,
    tool_request={"invoice_id": "INV-001", "amount": "1240.00"},
    outcome=approval_event,
    prior_capsule_id=prior_check_capsule_id,   # chain to the preceding fyi
)
```

This emits `action_type="decide"` with a **real** disposition block —
`human_disposed=True`, `approver="human"`, the actual `decision` value.  The
capsule is chained to the preceding tool-call capsule when `prior_capsule_id`
is supplied.

**NEVER call `record_hitl()` with fabricated data.**  Only call it once the
human has actually acted.  Passing `decision="accept"` before the event
resolves would seal a false record in the tamper-evident log.

## Add it yourself

```python
from capsule_emit.adapters.dapr_agents import DaprAgentsCapsuleEmitter  # 1

emitter = DaprAgentsCapsuleEmitter(                                       # 2
    operator="acme-co",
    developer="my-agent@v1",
    agent_name="my-agent",
    app_id="my-dapr-app",
)

@emitter.tool("call_external_api")                                        # 3 (per tool)
def call_external_api(endpoint: str, payload: str) -> dict:
    ...
```

For HITL, add one `emitter.record_hitl(...)` call per approval gate after the
external event resolves.

## The `dapr_agents` extension

Every capsule carries a `dapr_agents` block in `compute_attestation`:

```json
{
  "dapr_agents": {
    "agent_name": "invoice-checker",
    "tool_name": "check_invoice",
    "workflow_instance_id": "wf-abc123",
    "app_id": "invoice-app"
  }
}
```

On HITL capsules, `approver_id` is also included.  All values are strings
per §5.1.  The block is committed to `capsule_id`; receivers that do not
recognise it MUST ignore it (Class-1 extensibility).

## Limitations

The following are open questions for Dapr Agents maintainers.  See the adapter
source (`capsule_emit/adapters/dapr_agents.py`) for the full numbered list.
Re-verified 2026-07-30 against `dapr-agents==1.0.5` (see drift note below):

- **L1 Callback surface — PARTIALLY RESOLVED as of dapr-agents ≥1.0.x.**
  `dapr_agents.hooks` now ships a native `before_tool_call`/`after_tool_call`
  hook system (`Hooks`, `ToolHookContext`, `HookDecision` — `Proceed` /
  `Deny` / `Mutate` / `Skip` / `RequireApproval`), registered via
  `DurableAgent(hooks=Hooks(...))`.  This is a real, exercisable before-call
  seam that did not exist when this adapter was built (confirmed absent in
  `dapr-agents==1.0.0`).  This adapter's decorator-wrap approach
  (`@emitter.tool()`) remains valid as the simpler integration and is what
  this demo exercises; wiring `@emitter.tool()`'s emission into a
  `before_tool_call`/`after_tool_call` hook callback instead of a Python
  decorator is a reasonable follow-up but is a design change, not a fix —
  left for a dedicated task. Note: `after_tool_call` is documented by Dapr
  as "reserved API surface... not yet dispatched by the agent runtime" in
  1.0.5, so only `before_tool_call` is currently usable for a real hook.
- **L2 Workflow ID inside tools — STILL TRUE.** `ToolHookContext` /
  `HookContext` (the new hook system) carry `step_name`, `step_kind`,
  `source`, `payload`, `tool_call_id` — no `instance_id` or workflow field.
  Must still be supplied at construction or per-call.
- **L3 HITL approver identity — STILL TRUE, confirmed in the native flow
  too.** Dapr Agents' own new `ApprovalResponseEvent` (sent to
  `DurableAgent.raise_approval_event()`) carries `approved: bool` and
  `reason`, but no approver-identity field — the framework's own native
  approval schema has the same gap this adapter already worked around.
- **L4 Replay idempotency** — unchanged; Dapr Workflow may replay activities,
  wrapped tools fire again on replay, emitting duplicate capsules.
- **L5 App ID auto-discovery — STILL TRUE**, confirmed absent from the new
  hook context fields as well. Must be supplied at construction.

### Drift note (2026-07-30 rerun)

- **Version-naming correction:** there is no `dapr-agents` release numbered
  1.18 — latest on PyPI is **1.0.5** (checked against GitHub releases too).
  `dapr-agents==1.0.5` transitively pins the Dapr *core* SDK (`dapr` package)
  at **1.18.3** — that is almost certainly the source of any "Dapr Agents
  1.18" label; the two version numbers belong to different packages.
- **New in 1.0.x (not present in 1.0.0):** the `Hooks`/`RequireApproval`/
  `Deny`/`ApprovalRequiredEvent`/`ApprovalResponseEvent` native approval
  system described in L1/L3 above.
- **Adapter fix landed this pass:** `@emitter.tool()` gained an optional
  `prior_capsule_id` kwarg so a tool-call `fyi` capsule can chain onto a
  preceding `decide` capsule (previously only `record_hitl()` supported
  chaining) — needed for a real fyi → decide(blocked) → fyi chain where the
  agent escalates after a denial. See `test_tool_chains_to_prior_decide_capsule`.
- No changes were needed to capsule construction, digest computation, or the
  `dapr_agents` extension shape — none of it imports the `dapr_agents`
  package directly, so there was no hard breakage to fix, only the
  documentation/limitations drift above.

## Notes

- `tool_input` / `tool_output` are digest-committed automatically (content
  never leaves the process).
- `model=` is **not** auto-captured; pass it at construction if you want the
  model sealed into every capsule.
- For HITL rejections: `verdict="blocked"`, `effect.status="planned"` (the
  action was gated; it did not dispatch).
- For a subsequent "actually executed" capsule after an accepted HITL gate,
  emit a second capsule from the execution site with `confirms=<hitl_capsule_id>`.
