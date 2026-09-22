# Goose extension — `MCPCapsuleEmitter` inside your Goose MCP server

Goose's session log tells *you* what your extension did. A capsule turns each
tool call Goose makes to your extension into a record built for **someone who
doesn't already trust you** — your customer, their CISO, an auditor, the other
side of a deal — including the call an approval gate in your server refused.

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

1. **One record per tool call, sealed inside the extension.** Goose extensions
   are MCP servers, so the wiring is `@server.tool()` outside and
   `@emitter.tool()` directly on the function: every call Goose makes that *returns* seals a record with the arguments and
   the return value digested, verdict `executed`,
   effect `dispatched` — the tool ran; nothing in that record claims the
   outcome landed — and `runtime="mcp"` stamped. An approval gate in your server that refuses an order is an explicit
   `emit_capsule(..., verdict="blocked")` with the disposition your server
   asserts in the record (a self-assertion, not something the math attests), and a later escalation chains to it.
   Confirming an effect is a second, explicit `seal(..., confirms=...)` from
   wherever you observe it.
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

The adapter ships a runnable demo — no Goose session, no LLM key, no network
with `--no-anchor`, the same decorated tools Goose would call, exercised
directly — every record verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[mcp]"
python examples/goose-capsule/demo.py --no-anchor
```

You'll watch it seal `get_price` and `submit_order` as `executed`, a large order a *simulated* approval gate rejected as `blocked` (the refusal
is one your server records — a denial Goose makes on its own side never
reaches the extension), and the escalation to
a manager that chains to that refusal. Four records, every one `VALID` under the offline composed check (digest recompute and producer signature),
then a tamper test: flip one byte in an output digest and verify fails. The demo
seals to a throwaway ledger and checks it for you. Handing the real extension to
Goose is a `config.yaml` stanza, under [Reference](#reference) below; the
sealing happens inside your server and never needs Goose's API key.

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
  Only tools you decorate seal; what Goose does with its other extensions, or
  in its own shell, is never sealed here. Closing that is a separate
  consistency check against an independent log.
- **Dispatched is not confirmed.** The decorator's record says the tool ran;
  a confirmed effect is a second record you seal from where you can see it.
- **No model in the record unless you pass it.** The extension sees the tool
  boundary, not the model Goose is running; `model=` is explicit.
- **A tool that raises leaves no record here.** The decorator re-raises the
  exception unchanged and seals nothing for that call; catch at the call site
  and `emit_capsule(..., verdict=...)` if you want the failure on the record.
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
  (0.8.3).
- It is **not** observability, tracing, or a dashboard, and it carries no score,
  ranking, or reputation — it's the record and the math over it. (If you found
  this via an "integrations" listing: this sits *next to* your traces as the
  evidence layer, it doesn't replace them.)

---

## Reference

[Goose](https://github.com/aaif-goose/goose) is an open-source AI coding
agent, originally built by Block and now an AAIF project (a founding one,
alongside MCP and AGENTS.md).
**Goose extensions are MCP servers** — every Goose tool is an MCP tool — so the
hardened `MCPCapsuleEmitter` you already know is the foundation of this extension.
No new glue is needed.

There are two integration patterns. They compose once
[capsule-emit#184](https://github.com/action-state-group/capsule-emit/issues/184)
ports the companion server to mcp 2.x; until then Pattern B needs its own
environment (uvx/pipx, pinned below).

---

## Pattern A — seal at the tool (recommended)

Add `@emitter.tool()` to your Python MCP server and every call Goose makes to
that tool is automatically sealed into a verifiable capsule.

```python
from mcp.server.mcpserver import MCPServer   # mcp < 2: from mcp.server.fastmcp import FastMCP
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

server  = MCPServer("po-agent")   # FastMCP("po-agent") on mcp < 2
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
```

(`operator` / `developer` are constructor arguments of `MCPCapsuleEmitter`; the
emitter reads no environment variables.)

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

Or with uvx, pinned the same way:

```yaml
extensions:
  capsule_emit:
    type: stdio
    cmd: uvx
    args: ["--from", "capsule-emit[mcp]", "--with", "mcp<2", "capsule-emit-server"]
```

Requires: `pip install "capsule-emit[mcp]" "mcp<2"` — the companion server still
imports the mcp 1.x `FastMCP`; the port to mcp 2.x's `MCPServer` is tracked in
[capsule-emit#184](https://github.com/action-state-group/capsule-emit/issues/184).

---

## Add it yourself

```python
from mcp.server.mcpserver import MCPServer   # 1  (mcp < 2: from mcp.server.fastmcp import FastMCP)
from capsule_emit.adapters.mcp import MCPCapsuleEmitter    # 2

server  = MCPServer("my-agent")                            # 3
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
capsule-emit verify --store ledger.jsonl
```

Pass an absolute `ledger=` to the emitter: the default `ledger.jsonl` lands in
whatever directory Goose spawned the extension from.

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
capsule-emit verify --store ledger.jsonl
```

Goose v1.39.0 (aarch64-apple-darwin) installs as a prebuilt binary — no Rust
toolchain required.

## Issues-first contributions: the Verification-stage evidence comment

Goose's contribution lifecycle (CONTRIBUTING.md, at time of writing) ends at a
**Verification** stage — a human confirms the implementation works — and its PR
rules require explaining "how the issue's verification plan was carried out."
When the implementer is an agent sealing its work with this adapter, that explanation can carry the records alongside the prose:

```bash
# during In progress: the agent's tool calls seal into ledger.jsonl as usual
# at Verification:
capsule-emit evidence --ledger ledger.jsonl --issue <ready-issue-url> --out comment.md
```

The output is a markdown comment for the PR/issue: per-step sealed records
(action, verdict, effect, capsule_id), the chain summary, offline verify
commands, and a viewer permalink. Fail-closed — every capsule is re-verified
locally at generation time; a tampered ledger refuses to render. Runnable
end-to-end: `examples/goose-capsule/contribution_demo.py` (offline by
default), with a committed real run at
`examples/goose-capsule/evidence/verification-comment.md`.

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
