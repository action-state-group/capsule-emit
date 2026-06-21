# MCP adapter

`MCPCapsuleEmitter` is the **primary** adapter. It works with MCP tool endpoints —
and with any plain Python callable, no MCP SDK required.

```python
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

emitter = MCPCapsuleEmitter(operator="acme-co", developer="po-agent@v1")
```

Both placements below capture the call **input** (args/kwargs) and **output**
(return value) automatically — they only differ in *where the wiring lives*.

## Where to put the call

### Option A — decorate the tool definition (recommended)

Put `@emitter.tool(...)` on the function where it's **defined**. Every call to that
tool — from anywhere — emits a capsule. This is the cleanest: the sealing lives
with the tool, so no call site can forget it.

```python
@emitter.tool("write_order")
def write_order(vendor: str, total: float) -> dict:
    ...
```

Use this when **you own the tool definition** and want *every* invocation sealed.

### Option B — wrap a single call site (ad-hoc)

Call `emitter.emit_capsule(...)` right after a call. The tool definition is
untouched; only *this* call site seals. Use this when you **don't own the tool**
(it's imported/third-party), or you only want to seal **some** calls (e.g. only the
ones that move money, not read-only lookups).

```python
result = write_order(vendor="Frobozz", total=1240.19)
cap = emitter.emit_capsule(
    "write_order",
    tool_input={"vendor": "Frobozz", "total": 1240.19},
    tool_output=result,
)
```

**The difference in one line:** Option A seals *the tool* (every call, automatic
I/O capture); Option B seals *one call* (you choose which, you pass the I/O). Same
capsule either way.

## Add it yourself

For most agents it's genuinely two lines — the import + constructor once, and one
decorator per consequential tool:

```python
from capsule_emit.adapters.mcp import MCPCapsuleEmitter          # 1
emitter = MCPCapsuleEmitter(operator="acme-co", developer="po-agent@v1")  # 2

@emitter.tool("write_order")                                         # 3 (per tool)
def write_order(vendor: str, total: float) -> dict:
    ...
```

Decorate only the tools that **do something consequential** (writes, payments,
external effects) — not every read.

## Or tell your coding agent

Paste this into Claude Code (or any coding agent) in your repo:

> Add `capsule-emit` to this MCP server. `pip install capsule-emit`, create one
> `MCPCapsuleEmitter(operator="<our-org>", developer="<this-agent>@<version>")` at
> module load, and decorate every **state-changing** tool with `@emitter.tool("<action_name>")`
> (leave read-only tools alone). Don't change tool signatures or behavior. Show me
> the diff before applying.

## Notes

- `tool_input`/`tool_output` are digest-committed automatically (see
  [anatomy](../anatomy.md)).
- `model=` is **not** auto-captured here — MCP wraps the *tool*, not the LLM, so the
  adapter can't see which model decided. Pass it explicitly via `emit_capsule(...,
  model={...})` (Option B) if you want it sealed.
- The decorator auto-adds `effect={"type": action, "status": "dispatched"}`. **There is no
  auto-confirmed capsule** — recording that an effect *actually happened* requires a second
  explicit `emit(..., confirms=..., effect={..., "status": "confirmed"})`. The
  *dispatched → confirmed* chain is the boundary/gate layer, not the adapter layer.
- For a refusal or a custom verdict, use Option B and pass `verdict=`/`effect=` explicitly.
