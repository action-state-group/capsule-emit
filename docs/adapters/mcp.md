# MCP adapter — `MCPCapsuleEmitter`

Your MCP server's logs tell *you* what your tools did. A capsule turns each
tool call into a record built for **someone who doesn't already trust you** —
your customer, their CISO, an auditor, the other side of a deal — sealed at the
tool's definition so no call site can forget it, and carrying a digest of the
tool manifest as the model saw it.

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

1. **One record per tool call, sealed at the tool.** `@emitter.tool("write_order")`
   on the definition — `def` or `async def`, the decorator directly on the
   function — seals a record for every call that *returns*, from anywhere,
   with the arguments and the return value digested automatically: verdict `executed`,
   effect `dispatched` — the tool ran; nothing in that record claims the
   outcome landed. Confirming an effect is a second, explicit
   `seal(..., confirms=..., effect={"status": "confirmed"})` from wherever you
   observe it, and a refusal is an explicit `emit_capsule(..., verdict="blocked")` —
   the adapter never infers either. `emitter.capture_toolset(tools)` seals a
   digest of the manifest your server passes it into every later record
   (`ext.mcp.toolset_digest`), so a change to a tool's description shows up as
   a boundary between adjacent records — provided the server re-captures, and
   provided the relying party holds the manifest the client actually received
   to compare against. Against a server that rewrites descriptions and never
   re-captures, the digest says nothing; it is a boundary marker, not a
   detector.
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
   records in a one-line summary after the tally, and, from the next release, `--require-signature`
   fails them (`INVALID`, exit 1) for ledgers whose producer always signs ([#185](https://github.com/action-state-group/capsule-emit/issues/185)), and the one check outside it
   is the ledger's checkpoint against the transparency service — see
   [Network behavior](#network-behavior).

## The 10-minute proof

The adapter ships a runnable demo — no LLM, no MCP client, no network with
`--no-anchor`, plain Python calls through the decorator the way MCP dispatch
would make them — every record verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[mcp]"
python examples/mcp-capsule/demo.py --no-anchor
```

You'll watch it decorate a tool and seal a record per call, capture the tool
manifest so each record carries `ext.mcp.toolset_digest`, then swap a tool's
description after the fact and watch the digest change land as a boundary
between adjacent records (the tool-description-swap attack described in published MCP security
guidance). Every record passes the offline payload verifier, and the demo prints the
`capsule-emit verify --store` line that adds the signature check. In your own
server the wiring is one constructor and one decorator per consequential tool,
under [Reference](#reference) below.

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
**zero egress**, set `CAPSULE_WITNESS=off` (the demo does) — with it off, offline `verify`
proves internal consistency only, and the anti-re-seal property is the part you
turned off. The `operator` and `developer` you pass to the emitter seal into the hash-chained record permanently — use a
role/version tag, not personal data. Inputs and outputs are sealed as digests:
data minimization, not confidentiality — a digest of a low-entropy value can be
recovered by enumeration.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace.
  The decorator seals the tools you decorate; a tool you leave undecorated, or
  a raw HTTP request the server makes on the side, is never sealed. Closing
  that is a separate consistency check against an independent log.
- **Dispatched is not confirmed.** The decorator's record says the tool ran.
  Whether the world-effect landed is a second record you seal from where you
  can see it — see [Notes](#notes).
- **No model in the record unless you pass it.** MCP wraps the tool, not the
  LLM, so the adapter cannot see which model decided; `model=` is explicit.
- **A tool that raises leaves no record here.** The decorator re-raises the
  exception unchanged and seals nothing for that call; if you want failed
  consequential calls on the record, catch at the call site and
  `emit_capsule(..., verdict=...)` yourself.
- **It records; it never changes the call.** The decorator returns what the
  tool returned and re-raises what it raised. Deny belongs to your gate layer.
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
  (next release).
- It is **not** observability, tracing, or a dashboard, and it carries no score,
  ranking, or reputation — it's the record and the math over it. (If you found
  this via an "integrations" listing: this sits *next to* your traces as the
  evidence layer, it doesn't replace them.)

---

## Reference

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
def write_order(vendor: str, total: float) -> dict:   # a tool you do not own
    return {"ok": True}

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
  explicit `seal(..., confirms=..., effect={..., "status": "confirmed"})`. The
  *dispatched → confirmed* chain is the boundary/gate layer, not the adapter layer.
- For a refusal or a custom verdict, use Option B and pass `verdict=`/`effect=` explicitly.
- `emitter.capture_toolset(tools)` seals a digest of the tool manifest **as presented to the
  model** into every subsequent capsule (`ext.mcp.toolset_digest`) — so a server that swaps a
  tool's description after gaining trust shows up as a visible digest change in the chain,
  not silence. See [`ext.mcp` — tool-manifest digest](../extensions/mcp-toolset-digest.md).

**Since 0.5.0:** the top-level producer verb is `seal()` (the former `emit()` was
renamed). `MCPCapsuleEmitter`, `@emitter.tool(...)`, and `emit_capsule(...)` are
unchanged — only the standalone confirm-chain call is now `seal()`.
