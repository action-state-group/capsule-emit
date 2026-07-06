# MCP capsule quickstart

> Any MCP tool call → a sealed, anchored, independently verifiable record.
> One decorator. Five minutes. Zero config.

```
wrap a tool call
       ↓
   seal it (input + output digests, timestamp, operator — committed to SHA-256 capsule_id)
       ↓
  anchor it (fire-and-forget POST to the public transparency log)
       ↓
   verify it (any party, offline or with inclusion proof)
```

The MCP protocol is unchanged. `capsule-emit` is the **record layer** you compose
into your server — your tool's behavior, signature, and return value stay exactly
the same.

---

## 1. Install

```bash
pip install capsule-emit              # decorator works with any callable — no MCP SDK needed
pip install "capsule-emit[mcp]"       # also installs the mcp package (FastMCP integration)
```

## 2. Run the demo

```bash
# Fully offline — no network, no keys:
python examples/mcp-capsule/demo.py --no-anchor

# Anchored to the public transparency log (default):
python examples/mcp-capsule/demo.py
```

Expected output:

```
=== capsule-emit + MCP demo ===
wrap any MCP tool → verifiable record trail, in one decorator

  PO-2026-0047: effect.status='dispatched'  capsule_id=3a7f1c…
  PO-2026-0048: effect.status='dispatched'  capsule_id=8d2b4e…
  PO-2026-0049: effect.status='dispatched'  capsule_id=c1f9a3…

Latest capsule:
  action_id       : submit_order/2026-07-05T…
  action_type     : decide  ← 'decide'=consequential action; 'fyi'=observation-only
  runtime         : mcp     ← auto-set by adapter
  effect.status   : dispatched
  capsule_id      : c1f9a3…
  anchored        : True

  Input/output committed by digest (raw values stay LOCAL):
    agent_input_digest  : e4a3…
    agent_output_digest : 7b2d…

  ✓ verify(capsule).ok — tamper any byte and this fails

CLI verify (offline — from the ledger bytes, no network needed):
  $ agent-action-capsule verify --store /tmp/…/mcp_capsule_ledger.jsonl
  VALID  PO-2026-0047 …
  VALID  PO-2026-0048 …
  VALID  PO-2026-0049 …

✓ Done. Copy this pattern into your MCP server.
```

## 3. Add it to your server

Two lines to set up; one decorator per consequential tool:

```python
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

emitter = MCPCapsuleEmitter(
    operator="your-org",        # stable identifier for your tenant
    developer="your-agent@v1",  # agent name + version
    # anchor=True by default → fire-and-forget POST to the public log
    # Set AAC_ANCHOR_URL to point at a private anchor (see below)
    # Pass anchor=False to skip anchoring entirely (offline / CI)
)
```

### Option A — decorate the tool definition (recommended)

Use this when **you own the tool definition** and want every invocation sealed:

```python
from mcp.server.fastmcp import FastMCP

app = FastMCP("my-agent")

@app.tool()          # MCP protocol layer — outermost, introspects the wrapped fn
@emitter.tool()      # record layer — innermost, seals every call automatically
async def send_payment(amount: float, recipient: str) -> dict:
    # your tool implementation — unchanged
    return {"tx_id": "TX-001", "status": "dispatched"}
```

`functools.wraps` preserves the signature so FastMCP's schema generator still sees
`amount` and `recipient`, not `*args, **kwargs`.

**Decorator order is required:** `@app.tool()` on top, `@emitter.tool()` directly on
the function. Reversing the order breaks FastMCP's schema introspection.

### Option B — wrap a single call site

Use this when **you don't own the tool** (imported / third-party), or you only want
to seal specific calls:

```python
result = transfer(amount=100, recipient="alice@example.com")
emitter.emit_capsule(
    "transfer",
    tool_input={"amount": 100, "recipient": "alice@example.com"},
    tool_output=result,
)
```

Same capsule either way. Option A seals every call automatically; Option B gives
you per-call control.

### Name inference

`@emitter.tool()` with no arguments infers the action name from `fn.__name__`.
You can override it: `@emitter.tool("my_action")`.

### Async tools

Both sync and `async def` functions work. The wrapper is automatically async for
async tools so the MCP SDK sees the right type:

```python
@app.tool()
@emitter.tool()
async def fetch_report(report_id: str) -> dict:
    ...
```

## 4. What gets sealed

Every capsule commits the following — all by SHA-256 digest:

| Field | What | Where |
|---|---|---|
| `agent_input_digest` | SHA-256(canonical JSON of tool arguments) | `compute_attestation` |
| `agent_output_digest` | SHA-256(canonical JSON of return value) | `compute_attestation` |
| `operator` | your org identifier | capsule payload |
| `developer` | your agent name + version | capsule payload |
| `timestamp` | UTC seal time | capsule payload |
| `runtime` | `"mcp"` — set automatically | `compute_attestation` |
| `capsule_id` | SHA-256 of the entire payload | top-level |

**Raw values stay local.** Only digests leave the process. The capsule can be
shared, anchored, or audited without exposing tool inputs or outputs.

## 5. Anchor by default

`anchor=True` is the constructor default. On every `@emitter.tool()` call,
`capsule-emit` fires a background POST with only the `capsule_id` hex string to
the SCITT transparency log — your tool call is never blocked.

**Override the endpoint:**

```bash
export AAC_ANCHOR_URL=https://your-private-anchor.example.com
```

```python
emitter = MCPCapsuleEmitter(..., anchor_url="https://your-anchor.example.com")
```

**Disable anchoring** (offline / sandbox / CI):

```python
emitter = MCPCapsuleEmitter(..., anchor=False)
# or at the call site:
python examples/mcp-capsule/demo.py --no-anchor
```

**Resilience:** if the anchor POST fails, a warning is issued and the tool call
proceeds normally. The record layer never crashes the tool.

## 6. Verify a capsule

**Offline — from the ledger bytes, no network needed:**

```bash
agent-action-capsule verify --store ./ledger.jsonl
```

Exit 0 = all capsules verify. Tamper one byte of any capsule and this fails.

**After anchoring — verify inclusion in the public transparency log:**

```bash
agent-action-capsule verify --transparent statement.cose \
    --issuer-key issuer_pub.pem
```

`receipt_verified: True` in the output proves the capsule's digest is in the
append-only log — independently of the producer.

**In-process:**

```python
from agent_action_capsule import verify

result = verify(emitter.last.capsule)
assert result.ok
```

## 7. Read-only tools

Read-only tools (lookups, queries) don't need a capsule. Mark them explicitly so
the intent is recorded:

```python
@emitter.tool(action_type="fyi")   # observation-only
def get_balance(account: str) -> float:
    ...
```

Or skip sealing reads entirely with `seal_reads=False`:

```python
emitter = MCPCapsuleEmitter(..., seal_reads=False)
# fyi tools → no capsule; unknown/decide tools → still sealed (fail-safe)
```

## 8. Tell your coding agent

Paste this prompt into Claude Code (or any coding agent) in your MCP server repo:

> Add `capsule-emit` to this MCP server. `pip install capsule-emit`, create one
> `MCPCapsuleEmitter(operator="<our-org>", developer="<this-agent>@<version>")` at
> module load, and decorate every **state-changing** tool with `@emitter.tool()`
> (leave read-only tools alone). Don't change tool signatures or behavior. Show me
> the diff before applying.

## 9. About the record format

Each capsule conforms to
[`draft-mih-scitt-agent-action-capsule`](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/),
an individual submission to the IETF SCITT Working Group. The format is
independently verifiable — any party can check a capsule without trusting the
producer.

The verifier ([`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule))
is a separate package on purpose: **you don't need `capsule-emit` to verify**.
The capsule spec repo also ships frozen conformance vectors for interoperability
testing.

---

## Reference

| | |
|---|---|
| Constructor | `MCPCapsuleEmitter(operator, developer, ledger="ledger.jsonl", anchor=True, anchor_url=None, model=None, action_type=None, host_provenance=False, seal_reads=True)` |
| Decorator | `@emitter.tool(action=None, *, effect_type=None, verdict="executed", action_type=None, model=None)` |
| Direct emit | `emitter.emit_capsule(action, tool_input, tool_output, *, verdict, effect, ...)` |
| Env var | `AAC_ANCHOR_URL` — override the anchor endpoint |
| Last capsule | `emitter.last` — the most recent `EmitResult` |
| All capsules | `emitter.results` — list of all `EmitResult`s this session |

Full adapter reference: [`docs/adapters/mcp.md`](../../docs/adapters/mcp.md)
