# Dapr Agents adapter — `DaprAgentsCapsuleEmitter`

Your Dapr Workflow history tells *you* what your agent did, after the run. A
capsule turns each tool call and each human approval into a record built for
**someone who doesn't already trust you** — your customer, their CISO, an
auditor, the other side of a deal — sealed live at the decision point, including
the approval a human refused.

That's the difference between a log and a record. A log is for you. A record is
for the person who has to believe you. Logs answer "what happened?" for the
team that owns the log; they don't answer "can a stranger confirm this months
later?", because the party that ran the agent also holds and can rewrite the
log. A capsule is content-addressed and checkable against the capsule format
([`draft-mih-scitt-agent-action-capsule`](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/), an individual
IETF Internet-Draft, not a WG document) by anyone holding the file. Altering a
record's content changes its id; the external checkpoint, below, pins the ids
as of the last accepted checkpoint — for everyone, the key holder included.

## What you get, in three claims

1. **One record per tool call and one per human decision, sealed as they
   happen.** `@emitter.tool("check_invoice")` on the tool seals an `fyi` record
   per invocation with the arguments and the return value digested — the
   adapter observes the tool boundary, not the model that chose it, and says
   so. `emitter.record_hitl(...)`, called only after the approval resolves — after
   `ctx.wait_for_external_event()` in a raw workflow, or after the decision
   arrives through `raise_approval_event` with the 1.0.x hook system's
   `RequireApproval` — seals a `decide` record with a real
   disposition — `human_disposed`, the approver id your auth layer supplied,
   the actual decision — chained to the tool record it gates. Every record
   carries the `dapr_agents` extension block (agent, tool, workflow instance,
   app id) so it joins the workflow's own history. Calling `record_hitl` before
   the human acts would seal a false record; the page says so twice.
2. **Each record is addressed by the digest of its own canonical content, and
   signed.** The digest is self-consistency: anyone holding the file recomputes
   it, so a changed field changes the id and the link from the outcome to its
   record stops resolving. The producer signature proves the key named in the
   record signed that id — and nothing more until you pin the producer's key
   through a channel you already trust: the signature and key id sit outside
   the digest, so a ledger re-signed under a fresh key passes offline `verify`
   and still matches its checkpoint. What constrains everyone, the key holder
   included, is the external checkpoint, as of the last accepted one — see
   [Network behavior](#network-behavior).
3. **You re-check it offline.** `capsule-emit verify --store <ledger>.jsonl`
   recomputes every digest and outcome link and checks every producer signature it
   finds — no account, no service, no network. It runs the format's reference
   payload verifier plus the producer-envelope check; a record carrying no
   signature at all is not failed by default — `capsule-emit verify` counts such
   records in a one-line summary after the tally, and, from 0.8.3, `--require-signature`
   fails them (`INVALID`, exit 1) for ledgers whose producer always signs ([#185](https://github.com/action-state-group/capsule-emit/issues/185)), and the one check outside it
   is the ledger's checkpoint against the transparency service — see
   [Network behavior](#network-behavior).

## The 10-minute proof

No Dapr sidecar and no workflow runtime: the adapter imports nothing from
`dapr_agents`, so the proof is the wiring run as-is — a decorated tool call, then a stand-in for a resolved rejection event
recorded against it (no human acts in the proof — in your workflow this call
follows `ctx.wait_for_external_event()`, never precedes it), then the CLI
verifier:

```python
import os, pathlib, subprocess, sys, tempfile

os.environ.setdefault("CAPSULE_WITNESS", "off")  # zero egress for the proof
from capsule_emit.adapters.dapr_agents import DaprAgentsCapsuleEmitter

ledger = pathlib.Path(tempfile.mkdtemp()) / "ledger.jsonl"
emitter = DaprAgentsCapsuleEmitter(
    operator="acme-co", developer="invoice-agent@v1",
    agent_name="invoice-checker", app_id="invoice-app",
    workflow_instance_id="wf-demo", ledger=str(ledger), anchor=False,
)

@emitter.tool("check_invoice")
def check_invoice(invoice_id: str, amount: str) -> dict:
    return {"invoice_id": invoice_id, "ok": True}

check_invoice("INV-001", "1240.00")                  # fyi: the tool boundary
emitter.record_hitl(                                 # decide: a stand-in for the
    "approve_payment", approver_id="approver-role:ap-lead",  # resolved approval event
    decision="reject",
    tool_request={"invoice_id": "INV-001", "amount": "1240.00"},
    outcome={"decision": "reject"},
    prior_capsule_id=emitter.last.capsule_id,          # chained to the fyi
)

print(subprocess.run([sys.executable, "-m", "capsule_emit.cli", "verify",
                      "--store", str(ledger)], capture_output=True, text=True).stdout)
```

```
  VALID
  VALID

2/2 VALID
```

Two records — `fyi` with effect `dispatched`, `decide` with effect left
`planned` (gated, not dispatched) and chained to it — each `VALID` under the digest recompute and its producer signature. `pip install
"capsule-emit[dapr-agents]"` is the only dependency; the proof imports
capsule-emit alone, never `dapr_agents`. In your workflow the wiring is the decorator per tool and one
`record_hitl` per approval gate, under [Reference](#reference) below.

## Network behavior

By default the emitter runs an async **checkpoint/witness** stream: it
periodically posts a *checkpoint* — size, root hash, timestamp; **never capsule
content** — to a transparency service, and prints a notice before the first
attempt. That external commitment is what
makes a re-seal of the content detectable — by anyone, the key holder
included — as of the last accepted checkpoint; that comparison is the one check
outside offline `verify`. A
checkpoint goes out every 100 entries or 900 seconds by default, from a
background thread joined at interpreter exit: records sealed since the last
accepted checkpoint are covered only once the next one lands, dropping records
from the end of the ledger is invisible to offline `verify` until then, and a
process killed before exit never posts its pending checkpoint. The detection
holds given one honest witness — one that checks each checkpoint against the
last it accepted; multi-witness bundling, which the default does not do for you,
is what raises the bar against a dishonest witness. For a first local run with
**zero egress**, set `CAPSULE_WITNESS=off` (the proof above does) — with it
off, offline `verify` proves internal consistency only, and the anti-re-seal
property is the part you turned off. The `operator`, `developer`, `agent_name` and `app_id` you pass to the
emitter — and the `approver_id` you pass to `record_hitl`, stored raw — seal
into the hash-chained record permanently; use a role/version tag, not personal
data. Inputs and outputs are sealed as digests:
data minimization, not confidentiality — a digest of a low-entropy value can be
recovered by enumeration.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace.
  Only decorated tools seal, and a replayed Dapr activity seals again — see
  [Limitations](#limitations). Closing that is a separate consistency check
  against an independent log.
- **It sees the tool boundary, not the decision.** Tool records are `fyi`
  because the model's choice is not visible at this seam; the `decide` record
  exists only for a human decision your workflow actually received.
- **Approver identity and workflow id are yours to supply.** The hook
  context carries neither; Dapr's own approval event (1.0.6) carries at most an
  optional, unvalidated approver JWT — resolve it to an opaque subject id in
  your auth layer before passing `approver_id`, which seals raw. The workflow
  id is the `instance_id` you hand to `raise_approval_event` — pass the same
  value to `record_approval_response`. The adapter
  never guesses — see [Limitations](#limitations).
- **Only Python-defined tools carry the decorator.** Tools sourced from MCP or
  OpenAPI get no `fyi` record from this adapter; the `before_tool_call` hook is
  the seam that sees them — see [Limitations](#limitations).
- **It records; it never changes the call.** The decorator returns what the
  tool returned; `record_hitl` seals what the human decided. Deny belongs to
  your gate layer.
- **Tamper-evidence, not tamper-proof.** The digest catches an altered field;
  the signature catches an altered record only once you know which key to
  expect. Neither, by itself, stops the holder from re-sealing the entire
  chain offline — that's what the external witness is for, and why it
  defaults on.
- **Kinds of `verify` — don't conflate them.** Two checks live inside
  `capsule-emit verify` — the digest recompute and the producer signature — and
  both run offline. Checking the ledger's checkpoint against the transparency
  service is outside it, and that is what backs the anti-re-seal property above.
  Never quote a green `capsule-emit verify` as witness verification, and never
  read a valid signature as a name: it proves the key in the record signed it,
  not who holds the key — a ledger re-signed under a fresh key passes it, so does one with the signatures stripped, unless you pass `--require-signature`
  (0.8.3).
- It is **not** observability, tracing, or a dashboard, and it carries no score,
  ranking, or reputation — it's the record and the math over it. (If you found
  this via an "integrations" listing: this sits *next to* your logs as the
  evidence layer, it doesn't replace them.)

---

## Reference

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
    return {"invoice_id": invoice_id, "ok": True}   # your tool logic unchanged
```

Works with both `def` and `async def` functions.  Emit errors are warned and
logged, never propagated — the tool always returns normally.

**Why `action_type="fyi"`?**  The adapter sees the *tool boundary*, not the
LLM that decided to call it.  Recording the call as "fyi" is honest; the
upstream decision capsule (if any) lives in the model layer, not here.

### Surface 2 — HITL approval gates

Dapr Agents ≥ 1.0.x has a native approval flow: a `before_tool_call` hook
returns `RequireApproval`; the runtime publishes an `ApprovalRequiredEvent`
(`approval_request_id`, `instance_id`, `step_name`, `tool_call_id`,
`tool_arguments`) and suspends the workflow; a human decides; your approval
service — a Slack bot, a dashboard, a CLI — delivers the answer with
`DurableAgent.raise_approval_event(instance_id, approval_request_id, approved,
reason, approver_token)`. **That call is the seam.** It is the only point where
a real decision exists, so the record is written right next to it, with the
same arguments — never from inside the hook, where nothing has been decided:

```python
# In your approval service, after the human has acted:
def deliver_decision(agent, *, instance_id, approval_request_id, approved, reason, approver_token):
    approver_id = resolve_subject(approver_token)   # YOUR auth layer; dapr-agents does not validate the token
    emitter.record_approval_response(
        "delete_invoice",                           # the gated step_name
        instance_id=instance_id,
        approval_request_id=approval_request_id,
        approved=approved,
        reason=reason,
        approver_id=approver_id,
        tool_request={"invoice_id": "INV-001"},     # ApprovalRequiredEvent.tool_arguments, if you keep them
        tool_call_id="call_abc123",
    )
    agent.raise_approval_event(instance_id, approval_request_id, approved, reason, approver_token)
```

This seals `action_type="decide"` — `executed` on approval, `blocked` on
rejection — with `human_disposed=True`, the real `decision`, and
`workflow_instance_id`, `approval_request_id`, `tool_call_id`, `approver_id`
in the `dapr_agents` extension. The hook context (`ToolHookContext`: `step_name`,
`step_kind`, `source`, `payload`, `tool_call_id`) carries neither the workflow
id nor an approver, which is why both come from the `raise_approval_event`
call and your auth layer, not from the hook.

For any other gate — a hand-rolled `ctx.wait_for_external_event()` — the
lower-level `record_hitl()` takes the decision directly. After the event
resolves, and only then:

```python
# Inside your Dapr Workflow definition:
# approval_event = ctx.wait_for_external_event("approval_event")
# ctx.yield_()
approval_event = {"approved_by": "approver-role:ap-lead", "decision": "reject"}   # what the event resolved to
prior_check_capsule_id = None

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
  **The HITL record is wired to this native flow**: `record_approval_response`
  takes `raise_approval_event`'s own arguments — see
  [Surface 2](#surface-2--hitl-approval-gates).
- **L2 Workflow ID inside tools — STILL TRUE.** `ToolHookContext` /
  `HookContext` (the new hook system) carry `step_name`, `step_kind`,
  `source`, `payload`, `tool_call_id` — no `instance_id` or workflow field.
  Must still be supplied at construction or per-call.
- **L3 HITL approver identity — STILL TRUE, confirmed in the native flow
  too.** Dapr Agents' own new `ApprovalResponseEvent` (sent to
  `DurableAgent.raise_approval_event()`) carries `approved: bool` and `reason`, and no *resolved* approver identity —
  1.0.6 adds an optional `approver_token` (a raw JWT the framework passes through
  unvalidated); its `approver_subject` field is populated only by a
  caller-supplied plugin and is `None` as dapr-agents builds it, and
  `raise_approval_event` has no subject parameter at all. Resolving the token
  to a subject id is your auth layer's job; `record_approval_response`
  requires the result as `approver_id` and never derives one.
- **L4 Replay idempotency** — unchanged; Dapr Workflow may replay activities,
  wrapped tools fire again on replay, emitting duplicate capsules.
- **L5 App ID auto-discovery — STILL TRUE**, confirmed absent from the new
  hook context fields as well. Must be supplied at construction.

### Drift note (2026-07-30 rerun)

- **Version-naming correction:** there is no `dapr-agents` release numbered
  1.18 — latest on PyPI at that rerun was **1.0.5** (checked against GitHub releases
  too); the proof above ran on 1.0.6.
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
