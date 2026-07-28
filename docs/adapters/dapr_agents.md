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
source (`capsule_emit/adapters/dapr_agents.py`) for the full numbered list:

- **L1 Callback surface** — No before/after tool hook was found at v1.0; this
  adapter wraps at function definition time.
- **L2 Workflow ID inside tools** — Not available from the SDK inside a tool
  call at v1.0; must be supplied at construction or per-call.
- **L3 HITL approver identity** — `wait_for_external_event()` returns a raw
  payload; the authenticated approver identity must come from your auth layer.
- **L4 Replay idempotency** — Dapr Workflow may replay activities; wrapped
  tools fire again on replay, emitting duplicate capsules.
- **L5 App ID auto-discovery** — Must be supplied at construction.

## Notes

- `tool_input` / `tool_output` are digest-committed automatically (content
  never leaves the process).
- `model=` is **not** auto-captured; pass it at construction if you want the
  model sealed into every capsule.
- For HITL rejections: `verdict="blocked"`, `effect.status="planned"` (the
  action was gated; it did not dispatch).
- For a subsequent "actually executed" capsule after an accepted HITL gate,
  emit a second capsule from the execution site with `confirms=<hitl_capsule_id>`.
