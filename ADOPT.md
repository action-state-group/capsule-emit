# ADOPT.md — 30-minute self-serve adopter path

> **tl;dr:** `pip install capsule-emit` → one `emit()` call → verify from the bytes.
> No account. No key management. No server to run.

---

## Who this is for

**Identity vendors.** You know *who* the workload is — the agent's principal, its
identity document, its SPIFFE SVID. A capsule records *what it did*: the action it
took, the inputs and outputs (committed by digest, not exposed), the verdict it
received, and the effect it dispatched. The two layers compose without conflict.
You own the identity surface; the capsule owns the "did" record — and a third party
can independently accept the capsule without ever asking you to vouch for either layer.

**Runtime and sandbox vendors.** Your telemetry, your flight recorder, your sandbox
log — those are yours, and they're your word for it. When something happens across
an organizational boundary, your counterparty has no reason to trust your logs.
Anchoring a capsule registers a digest-only fingerprint on a public, append-only
transparency log. The capsule stays on your machine; the proof travels. An auditor
verifies from the bytes — not from your export.

**Audit and GRC tools.** You don't need to re-attest anything. The capsule already
carries the content-addressed record; the `agent-action-capsule` verifier replays
the digest checks deterministically from the raw bytes. No key escrow, no credential
sharing, no system access. Your audit tool consumes the verify surface and emits a
pass/fail finding against the capsule itself — not against the operator's self-report.

---

## Prerequisites

- Python 3.11+ (package supports ≥3.9, but 3.11+ recommended)
- An internet connection (to register the digest against the public anchor)

```bash
pip install capsule-emit   # version 0.3.2 (current)
```

---

## Step 1 — Emit your first capsule (2 minutes)

```python
from capsule_emit import emit

cap = emit(
    action="hello-world",
    operator="your-org",                    # the accountable tenant
    developer="my-agent@v1",               # agent identity + version
    agent_input={"task": "greet"},
    agent_output={"message": "hello"},
    model={"provider": "your-provider", "model_id": "your-model"},
    verdict="executed",
    effect={"type": "write_order", "status": "confirmed"},
)
print("Capsule ID:", cap.capsule_id)
print("Anchored: ", cap.anchored)
```

What this does:

1. **Content-addresses the action.** `agent_input` and `agent_output` are digested
   (SHA-256 over the RFC 8785 canonical form) and committed into the capsule.
   The raw values never leave your process — only their fingerprints do.
2. **Seals it.** The capsule is assembled as a deterministic JSON structure and
   content-addressed: the `capsule_id` is the SHA-256 of the canonical form.
   Any field tampered after sealing produces a different `capsule_id`.
3. **Anchors it, async.** The `capsule_id` (the digest, nothing else) is submitted
   in a background thread to the public SCITT transparency log at
   `anchor.agentactioncapsule.org`. Anchoring is non-blocking and does not delay
   the return. `cap.anchored = True` means the submission was dispatched; use the
   anchor receipt verification in Step 3 to confirm inclusion.
4. **Appends to a local ledger.** Every `emit()` appends the capsule to
   `ledger.jsonl` in the current directory. View it:
   ```bash
   capsule-emit ledger view ./ledger.jsonl
   ```

`cap.capsule_id` is a 64-character lowercase hex string — the SHA-256 content
address of this capsule. Keep it: it's how you refer to this record everywhere.

> **Float note.** Raw Python floats in `agent_input` / `agent_output` raise a
> `FloatInDigestError` because floating-point values cannot be reproducibly
> digested. Encode monetary or quantity values as exact decimal strings
> (e.g. `"1240.19"`) before sealing.

---

## Step 2 — Verify the record (1 minute)

The verifier ships in the specification package and is independent of `capsule-emit`
on purpose — any producer can make a capsule, any party can verify one.

```bash
pip install agent-action-capsule
agent-action-capsule verify --store ./ledger.jsonl
```

Or verify a single capsule directly from its JSON:

```python
import json, pathlib
cap_dict = cap.capsule          # EmitResult.capsule holds the raw dict
pathlib.Path("capsule.json").write_text(json.dumps(cap_dict))
```

```bash
agent-action-capsule verify capsule.json
```

What verification proves:

- The `capsule_id` matches the recomputed content address of the capsule fields.
  One byte tampered → different digest → verification fails.
- The `agent_input_digest` and `agent_output_digest` match the values in
  `compute_attestation` (supply the raw inputs to `--verify-inputs`).
- Class-1 verification is **offline and keyless** — reproducible from the bytes alone.

---

## Step 3 — Confirm anchor registration and verify the receipt (3 minutes)

After `emit()` dispatches the background anchor POST, confirm your capsule is in
the public log and verify the cryptographic inclusion proof offline:

```bash
CAPSULE_ID=<your-capsule_id>   # the 64-char hex from cap.capsule_id

# 1. Fetch receipt (POST is idempotent — same capsule_id always returns same receipt)
curl -s -X POST https://anchor.agentactioncapsule.org/v1/digest \
  -H 'Content-Type: application/json' \
  -d "{\"capsule_id\": \"${CAPSULE_ID}\"}" > anchor_resp.json

# 2. Save receipt file and display proof summary
python3 -c "
import json, base64
d = json.load(open('anchor_resp.json'))
open('receipt.cose', 'wb').write(base64.b64decode(d['receipt_b64']))
open('entry_hash.txt', 'w').write(d['entry_hash'])
print('entry_hash :', d['entry_hash'])
print('leaf_index :', d['leaf_index'], '/ tree_size:', d['tree_size'])
"

# 3. Fetch the anchor log public key (PEM)
python3 -c "
import urllib.request, json, base64
d = json.loads(urllib.request.urlopen(
    'https://anchor.agentactioncapsule.org/anchor/authority-pubkey').read())
raw = bytes.fromhex(d['pubkey_hex'])
der = bytes.fromhex('302a300506032b6570032100') + raw
b64 = base64.encodebytes(der).decode().strip()
open('anchor_pub.pem','w').write(
    '-----BEGIN PUBLIC KEY-----\n' + b64 + '\n-----END PUBLIC KEY-----')
print('anchor key_id:', d['key_id'])
"

# 4. Verify the receipt offline (zero-trust — no call back to the log operator)
pip install scitt-cose
scitt-cose \
  --receipt receipt.cose \
  --receipt-log-pubkey anchor_pub.pem \
  --leaf-entry-hex "$(cat entry_hash.txt)"
```

`Receipt ok: True` means the anchor's Ed25519 key signed a Merkle root that
provably contains your capsule digest — without trusting the anchor service
to tell you anything.

> **Anchor health check:**
> ```bash
> curl https://anchor.agentactioncapsule.org/health
> # -> {"ok":true, "tree_size": <N>, ...}
> ```

> **Verify permalink (landing with verify-surface deploy).** Once
> `https://verify.agentactioncapsule.org` is live, you can open
> `https://verify.agentactioncapsule.org/v/<capsule_id>` in a browser for a
> rendered view of your capsule alongside its anchor proof. Until then,
> the receipt verification above is the canonical proof path.

---

## Step 4 — Run conformance vectors in CI

The `agent-action-capsule` repository ships frozen conformance test vectors. Run
them in your CI pipeline to confirm that the verifier and your environment agree
with the spec's frozen bytes:

```yaml
# .github/workflows/capsule-conformance.yml
name: Capsule conformance
on: [push, pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          repository: action-state-group/agent-action-capsule
          path: aac
      - run: pip install -e aac/python
      - run: |
          for dir in aac/test-vectors/pos-*/; do
            agent-action-capsule verify "$dir/input.json"
          done
```

Every directory under `test-vectors/pos-*/` is one positive case with a frozen
`input.json` capsule and an `expected.json` verifier result. Negative cases
(under `neg-*/`) test tampered or invalid capsules and are expected to fail
verification — run them separately if you want to test your own tooling's
rejection path.

---

## Adapter integrations

`capsule-emit` ships thin adapters for common agent frameworks. All adapters wrap
the same `emit()` base — only the hook point changes.

### MCP (Model Context Protocol)

Use `MCPCapsuleEmitter` to wrap any MCP tool function. Works with both sync and
`async def` tools. Decorator order matters: `@server.tool()` outermost (so the
framework sees the wrapped signature), `@emitter.tool()` directly on the function.

```python
from mcp.server.fastmcp import FastMCP
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

server  = FastMCP("my-agent")
emitter = MCPCapsuleEmitter(
    operator="your-org",
    developer="my-agent@v1",
    model={"provider": "your-provider", "model_id": "your-model"},
)

@server.tool()                              # outermost — framework introspects this
@emitter.tool(effect_type="write_order")   # innermost — seals every call
def submit_order(vendor: str, amount: str, po_number: str) -> dict:
    # your tool logic
    return {"status": "dispatched", "order_id": "PO-1234"}

server.run()
```

Each call to `submit_order` produces one sealed, anchored capsule with
`runtime="mcp"` in `compute_attestation`. If the tool has a FastMCP `Context`
parameter, its `request_id` and `client_id` are automatically captured.

### Google ADK (Agent Development Kit)

`ADKCapsuleEmitter` works via tool callbacks or an event-stream tap.

**Callback path** — one capsule per completed tool call:

```python
from google.adk.agents import LlmAgent
from capsule_emit.adapters.adk import ADKCapsuleEmitter

emitter = ADKCapsuleEmitter(
    operator="your-org",
    developer="my-adk-agent@v1",
    model={"provider": "google", "model_id": "gemini-2.0-flash"},
    effects={
        "write_order": {"type": "write_order", "status": "dispatched"},
    },
)

agent = LlmAgent(
    name="writer",
    model="gemini-2.0-flash",
    tools=[...],
    after_tool_callback=emitter.after_tool_callback,
)
```

`effects` is the declarative way to mark consequential tools: any call to
`write_order` gets that effect on its capsule. Tools not listed in `effects`
produce capsules with no effect asserted (read-only default).

**Event-stream tap** — for ADK apps that consume the `Runner` event stream:

```python
async for event in runner.run_async(...):
    emitter.tap_event(event)    # seals one capsule per completed tool call
    # your own event handling continues unchanged
```

### Dapr Agents

`DaprAgentsCapsuleEmitter` records capsules at **live decision points** inside a Dapr Agents
workflow — distinct from the `capsule-emit-dapr` Go adapter, which extracts post-hoc execution
records from signed Dapr Workflow history.

**Two seam points:**

**1 — Tool calls** (`@emitter.tool()`): wraps any Dapr Agents tool callable. One capsule with
`action_type="fyi"` per invocation.

**2 — HITL approval gates** (`emitter.record_hitl()`): called after `ctx.wait_for_external_event()`
resolves. Records the real human decision as `action_type="decide"` with a non-fabricated
disposition block.

```python
from capsule_emit.adapters.dapr_agents import DaprAgentsCapsuleEmitter

emitter = DaprAgentsCapsuleEmitter(
    operator="acme-co",
    developer="invoice-agent@v1",
    agent_name="invoice-checker",     # identifies this agent in the capsule
    app_id="invoice-app",             # Dapr sidecar app-id
    workflow_instance_id="wf-abc123", # set per workflow run
)

@emitter.tool("check_invoice")
def check_invoice(invoice_id: str, amount: str) -> dict:
    ...  # your tool logic; one fyi capsule sealed per call

# After the HITL event resolves — supply REAL approver and decision:
decide = emitter.record_hitl(
    "approve_payment",
    approver_id="alice@example.com",  # from your auth layer (never fabricated)
    decision="accept",                # "accept" or "reject" as it happened
    tool_request={"invoice_id": "INV-001", "amount": "1240.00"},
    outcome=approval_event_payload,
    prior_capsule_id=fyi_capsule_id,  # chains the decide to the fyi
)
```

`decision` must be `"accept"` or `"reject"` — anything else raises `ValueError` at the call site.
The `dapr_agents` block in `compute_attestation` carries `agent_name`, `tool_name`,
`workflow_instance_id`, `app_id`, and `approver_id` as exact strings (§5.1 compliant, Class-1
ignored by receivers that don't recognise the extension).

See [`docs/adapters/dapr_agents.md`](docs/adapters/dapr_agents.md) for the full API reference,
known limitations (L1–L7), and a side-by-side comparison with the Go adapter.


---

## Open registration policy notice

> The public anchor (`anchor.agentactioncapsule.org`) runs an **open registration
> policy** — no authentication required, no issuer binding enforced. It is a
> neutral public-good service for the standard.
>
> Production deployments may run
> [`capsule-anchor`](https://github.com/action-state-group/capsule-anchor) with a
> stricter registration policy (key binding, allow-list, rate limits). The open
> policy means any valid Signed Statement is accepted; it does **not** imply
> endorsement of the content.
>
> To point at a different anchor endpoint:
> ```bash
> export AAC_ANCHOR_URL=https://your-anchor.example.com/v1/digest
> ```
> or pass `anchor_url=...` to `emit()`.

---

## What's next

- **Chain records.** Link a confirmation to its parent action:
  `emit(..., confirms=parent_capsule_id)` — the *approved → executed → confirmed*
  sequence, or a cross-agent chain by id alone. See
  [`docs/chaining.md`](docs/chaining.md).
- **Declare constraints.** A `flows/<action>/manifest.md` declares the rules your
  action runs under. `capsule-emit` reads it (declaration); a compatible gateway
  enforces the same file (enforcement) with no change to your `emit()` calls.
  See [`docs/going-deeper.md`](docs/going-deeper.md).
- **Self-host the anchor.** Run
  [`capsule-anchor`](https://github.com/action-state-group/capsule-anchor) in your
  own VPC — nothing leaves your walls, the digest stays in your jurisdiction.

---

*Need help?* Open an issue at
[github.com/action-state-group/capsule-emit](https://github.com/action-state-group/capsule-emit/issues)
or post to the IETF SCITT working group mailing list (`scitt@ietf.org`).
