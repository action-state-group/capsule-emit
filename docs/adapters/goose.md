# Goose extension

[Goose](https://github.com/aaif-goose/goose) is Block's open-source AI coding
agent (a founding AAIF project alongside MCP and AGENTS.md).
**Goose extensions are MCP servers** — every Goose tool is an MCP tool — so the
hardened `MCPCapsuleEmitter` you already know is the foundation of this extension.
No new glue is needed.

There are two integration patterns.  They compose freely.

---

## Pattern A — seal at the tool (recommended)

Add `@emitter.tool()` to your Python MCP server and every call Goose makes to
that tool is automatically sealed into a verifiable capsule.

```python
from mcp.server.fastmcp import FastMCP
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

server  = FastMCP("po-agent")
emitter = MCPCapsuleEmitter(
    operator="acme-co",
    developer="goose-agent@v1",
    anchor=False,            # True → fire-and-forget digest anchor
)

@server.tool()              # MCP layer (Goose connects here)
@emitter.tool(effect_type="write_order")   # capsule-emit record layer (inner)
def submit_order(vendor: str, amount: float, po_number: str) -> dict:
    """Submit a purchase order — every Goose call is sealed."""
    return {"status": "dispatched", "po_number": po_number, "vendor": vendor}

if __name__ == "__main__":
    server.run()            # stdio — Goose spawns this as a child process
```

**Decorator order matters:** `@server.tool()` must be outermost (it introspects
the signature), `@emitter.tool()` directly on the function (it wraps the real
callable). `functools.wraps` preserves the signature so Goose sees the real
parameter names and types.

### Add this server to Goose

```yaml
# ~/.config/goose/config.yaml
extensions:
  po_agent:
    enabled: true
    type: stdio
    name: po_agent
    description: "Purchase-order tools with capsule audit trail"
    cmd: python3
    args: ["/path/to/your/server.py"]
    timeout: 30
    envs:
      CAPSULE_OPERATOR: "acme-co"
      CAPSULE_DEVELOPER: "goose-agent@v1"
```

### Or with uvx

```yaml
extensions:
  po_agent:
    type: stdio
    cmd: uvx
    args: ["--from", "capsule-emit[mcp]", "capsule-emit-server"]
```

---

## Pattern B — companion server (query / verify from Goose)

`capsule-emit` ships a standalone MCP companion server that gives Goose tools
to record arbitrary tool calls, verify capsules, and inspect the ledger.  Add it
to any Goose session to give the agent capsule-awareness.

```yaml
extensions:
  capsule_emit:
    enabled: true
    type: stdio
    name: capsule_emit
    description: "Record + verify Agent Action Capsules"
    cmd: python3
    args: ["-m", "capsule_emit.server"]
    timeout: 30
    envs:
      CAPSULE_LEDGER: "/tmp/goose-capsules.jsonl"
      CAPSULE_OPERATOR: "my-org"
      CAPSULE_DEVELOPER: "goose-agent@v1"
```

Tools exposed:

| Tool | What it does |
|------|--------------|
| `capsule_record(action, tool_input, tool_output, …)` | Seal any tool call manually |
| `capsule_verify(capsule_id, ledger)` | Verify a capsule by ID or prefix |
| `capsule_ledger(ledger, limit)` | Summarise the ledger (most-recent rows) |

Requires: `pip install "capsule-emit[mcp]"`

---

## Add it yourself

```python
from mcp.server.fastmcp import FastMCP                      # 1
from capsule_emit.adapters.mcp import MCPCapsuleEmitter    # 2

server  = FastMCP("my-agent")                              # 3
emitter = MCPCapsuleEmitter(
    operator="acme-co", developer="my-agent@v1",           # 4
)

@server.tool()                                             # 5
@emitter.tool()                                            # 6  ← inner decorator
def my_tool(x: str) -> str:
    return x.upper()
```

All existing tools get sealed with two extra lines (5 + 6).

## Or tell your coding agent

> Add the capsule-emit Goose extension to our MCP server. `pip install "capsule-emit[mcp]"`,
> import `MCPCapsuleEmitter`, create one
> `MCPCapsuleEmitter(operator="<our-org>", developer="<this-agent>@<version>", anchor=False)`,
> and add `@emitter.tool()` directly inside every `@server.tool()` decorator so
> every tool call Goose makes is sealed into a verifiable capsule.  Decorator order:
> `@server.tool()` outer, `@emitter.tool()` inner (directly on the function).  Don't
> change tool behavior, don't add `anchor=True` unless I ask.  Show me the diff first.

## Verify after a Goose session

```bash
agent-action-capsule verify --store ledger.jsonl
```

Or per-capsule:

```bash
agent-action-capsule verify --store ledger.jsonl --id <capsule_id_prefix>
```

## Run the demo

```bash
pip install "capsule-emit[dev]"
python examples/goose-capsule/demo.py --no-anchor
```

Output (abridged):

```
[step 4] Ledger: 3 capsule(s) sealed
  23ace1c61d4dce12… get_price [executed] runtime=mcp
  c2508f13214c38a0… submit_order [executed] runtime=mcp
  02a3673e32b9be9e… submit_order [executed] runtime=mcp

[step 5] Verify all capsules (offline — no network needed)
  23ace1c61d4dce12… ok=True  ✓
  c2508f13214c38a0… ok=True  ✓
  02a3673e32b9be9e… ok=True  ✓

[step 6] Tamper test: flip one byte in output digest → verify fails
  verify result:     ok=False  findings: ['recomputed … != carried …']
  Tamper detected — ok=False as expected. ✓
```

## Connect real Goose (step-by-step)

These steps require an LLM API key for the Goose session itself; the capsule
sealing happens entirely in your MCP server and does not need the key.

```bash
# 1. Install
pip install "capsule-emit[mcp]"
curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh | bash

# 2. Configure the extension (edit ~/.config/goose/config.yaml as shown above)

# 3. Run a session
ANTHROPIC_API_KEY=<key> goose run \
  -t "call submit_order with vendor=Frobozz, amount=1240.19, po_number=PO-7777"

# 4. Verify the capsule offline
agent-action-capsule verify --store ledger.jsonl
```

Goose v1.39.0 (aarch64-apple-darwin) installs as a prebuilt binary — no Rust
toolchain required.

## Notes

- **Every capsule carries `runtime="mcp"`** in `compute_attestation` automatically.
  No extra config.
- **No model auto-capture.** The MCP adapter sees the tool boundary, not the LLM.
  Pass `model={"provider": "anthropic", "model_id": "claude-opus-4-8"}` at
  construction (or per-tool) if you need the model sealed.
- **`action_type` for read-only tools.** Pass `@emitter.tool(action_type="fyi")` on
  lookup / read-only tools so they're marked as observations rather than gate
  decisions.  Consequential tools (`verdict="executed"`) default to `"decide"`.
- **Effect chain.** `@emitter.tool()` emits `effect.status="dispatched"` (the tool
  ran; outcome not yet confirmed by a second party).  Call `emit_capsule()` directly
  with `effect.status="confirmed"` once you have confirmation.
- **The capsule does not live inside the MCP message.** The protocol is unchanged.
  capsule-emit is a record layer composing into the server — Goose never sees it.
