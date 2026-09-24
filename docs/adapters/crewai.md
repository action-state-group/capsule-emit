# CrewAI adapter

Your CrewAI event bus already tells *you* what your crew did. A capsule turns each
tool call into a record built for **someone who doesn't already trust you** — your
customer, their CISO, an auditor, the other side of a deal — including the calls a
tool tried to make and didn't complete.

That's the difference between a log and a record. A log is for you. A record is for
the person who has to believe you. Traces answer "what happened?" for the team that
owns the trace; they don't answer "can a stranger confirm this months later?",
because the party that ran the crew also holds and can rewrite the trace. A capsule
is content-addressed and independently checkable — and, once an accepted witness
checkpoint covers it, checkable against a reference outside your control — so it
can.

## What you get, in three claims

1. **The attempt and its outcome are both in the record — including the ones that
   didn't go through.** Each tool call seals a `planned` record on
   `ToolUsageStartedEvent`; the outcome chains to it. If a guard *raises* to block a
   call, CrewAI fires `ToolUsageErrorEvent` and it seals a `failed` record
   (*"proposed, did not happen"*). If a guard instead *returns* a denial,
   `ToolUsageFinishedEvent` fires and it seals `confirmed` with the denial in the
   recorded output. Either way the decision is in the record, not just your logs.
2. **Each record is addressed by the digest of its own canonical content, and
   signed.** The digest is self-consistency: anyone holding the file recomputes
   it, so a changed field changes the id and the link from the outcome to its
   record stops resolving. The producer signature proves the key named in the
   record signed that id — and nothing more until you pin the producer's key
   through a channel you already trust: the signature and key id sit outside
   the digest, so a ledger re-signed under a fresh key passes offline `verify`
   and still matches its checkpoint. What an outside party can check the key
   holder against is an accepted external checkpoint — see
   [Network behavior](#network-behavior).
3. **You re-check it offline.** `capsule-emit verify --store <ledger>.jsonl`
   recomputes every digest and outcome link and checks every producer signature it
   finds — no account, no service, no network. A record carrying no signature at all is not failed by default — `capsule-emit verify` counts such
   records in a one-line summary after the tally, and, from 0.8.3, `--require-signature`
   fails them (`INVALID`, exit 1) for ledgers whose producer always signs ([#185](https://github.com/action-state-group/capsule-emit/issues/185)); the one check outside it is
   the ledger's checkpoint against the transparency service.

## The 10-minute proof

The adapter ships a runnable demo — a hermetic local transparency stub (no external
network) — that drives capsule-emit's listener on the real CrewAI event bus with one
succeeding tool-usage event and one failing one, then verifies every sealed record offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[crewai]"
python examples/crewai-listener/demo.py
```

You'll see the sealed capsules (`planned → confirmed` and `planned → failed`, chained)
and each record `verify: OK`. The demo seals to a throwaway ledger and checks it for
you; wiring the listener into a real crew (register once before `crew.kickoff()`) is in
the reference below.

## Network behavior

By default the listener runs an async **checkpoint/witness** stream: after every
100 records, or at the next seal once 900 seconds have passed (there is no
background timer), it posts a *checkpoint* — size, root hash, timestamp; **never
capsule content** — to a witness, and prints a notice before the first attempt.
The default witness is the project's public one at
`witness.agentactioncapsule.org`; `CAPSULE_WITNESS_URL` names another. Each
checkpoint the witness accepts gives an outside party a reference to check the
ledger against — as far as that party trusts the witness to be independent of
the key holder. There is no final checkpoint at exit, so a run that seals fewer
than 100 records, all within 900 seconds, posts none. Wiring is one listener
instantiated before `crew.kickoff()` (see below); for a first local run with **zero
egress**, set `CAPSULE_WITNESS=off` — offline `verify` checks the same things
either way; what you turn off is the outside reference. `operator`
and `developer` seal into the hash-chained record permanently — use a role/version tag,
not personal data.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` proves the records you have are exactly
  what was sealed and in order. It does **not** prove the tools actually ran, or that
  every call was recorded — a record nobody wrote leaves no trace. The listener only
  seals calls that go through CrewAI's tool bus; a direct MCP-client call or a raw HTTP
  request an agent makes on the side is never sealed. Closing that is a separate
  consistency check against an independent log.
- **Tamper-evidence, not tamper-proof.** The digest catches an altered field;
  the signature catches an altered record only once you know which key to
  expect. Neither, by itself, stops the holder from re-sealing the entire
  chain offline — an accepted witness checkpoint is the outside reference for
  that, and why the witness defaults on.
- **Kinds of `verify` — don't conflate them.** Two checks live inside
  `capsule-emit verify` — the digest recompute and the producer signature — and
  both run offline. Checking the ledger's checkpoint against the transparency
  service is outside it, and that comparison is the outside check described
  under Network behavior.
  Never quote a green `capsule-emit verify` as witness verification, and never
  read a valid signature as a name: it proves the key in the record signed it,
  not who holds the key — a ledger re-signed under a fresh key passes it, so does one with the signatures stripped, unless you pass `--require-signature`
  (0.8.3).
- It is **not** observability, tracing, or a dashboard, and it carries no score,
  ranking, or reputation — it's the record and the math over it. (If you found this via
  an "observability integrations" listing: this sits *next to* your traces as the
  evidence layer, it doesn't replace them.)

---

## Reference — two integration shapes, pick by coverage:

- **`CapsuleEventListener` (recommended)** — a CrewAI `BaseEventListener` on the
  event bus: register once, and each tool call the bus emits seals a capsule.
  Zero per-tool code. This is CrewAI's own sanctioned third-party mechanism.
- **`CrewAICapsuleEmitter.wrap()`** — wrap individual tools for surgical
  coverage (only the consequential ones on the record).

## The event-bus listener (register once)

```python
from capsule_emit.adapters.crewai_listener import CapsuleEventListener

CapsuleEventListener(operator="acme-co", developer="ops-crew@v1")
# ...then run your crew as usual:
crew.kickoff()
```

Requires `pip install "capsule-emit[crewai]"`. What seals:

| Bus event | Capsule |
|---|---|
| `ToolUsageStartedEvent` | `effect.status="planned"` (the commitment record) |
| `ToolUsageFinishedEvent` | `effect.status="confirmed"`, chained to the planned capsule (`chain.relation="confirms"`) |
| `ToolUsageErrorEvent` | `verdict="errored"`, `effect.status="failed"`, chained |
| `CrewKickoffStarted/Completed/FailedEvent` | `fyi` capsules (`include_lifecycle=False` to disable) |
| LLM call events | off by default (`include_llm=True` to enable) |

- **Replay-safe:** honors the bus's `is_replaying()` — per CrewAI's guidance,
  no capsule seals while a replay is dispatching.
- **Isolation-safe:** the bus already isolates handler exceptions; the listener
  additionally never raises. An unreachable anchor endpoint warns — the crew
  run is unaffected.
- Runnable end-to-end demo (hermetic, no LLM key, local stub anchor):
  [`examples/crewai-listener/`](../../examples/crewai-listener/).

## The tool wrapper (surgical)

`CrewAICapsuleEmitter` wraps a **tool object**. You hand it your tool, it hands back
a sealing version that emits one capsule per call (input + output captured
automatically). It works whether your tool is a plain function or a CrewAI
`BaseTool` subclass — and `wrap()` itself has no hard dependency on `crewai`.

```python
from capsule_emit.adapters.crewai import CrewAICapsuleEmitter

emitter = CrewAICapsuleEmitter(operator="acme-co", developer="ops-agent@v1")
```

## Where to put the call

Wrap the tool **before you give it to the crew** — i.e. at the point where you
build the tool list for an Agent/Crew. Two shapes, picked automatically by `wrap()`:

### Option A — a plain callable tool

```python
safe_tool = emitter.wrap(my_tool)          # returns a sealing wrapper
agent = Agent(role="...", tools=[safe_tool])
```

### Option B — a CrewAI `BaseTool` subclass

Same call — `wrap()` detects the class and patches its `._run`, so the *same tool
object* now seals and you can keep passing it as-is:

```python
emitter.wrap(my_base_tool)                 # patches ._run in place
agent = Agent(role="...", tools=[my_base_tool])
```

**The difference:** for a function, `wrap()` returns a **new** wrapped callable (use
the return value); for a `BaseTool`, it patches **in place** (the object you passed
is now sealing). Pass each tool through `wrap()` once. Don't double-wrap.

## Add it yourself

```python
from capsule_emit.adapters.crewai import CrewAICapsuleEmitter      # 1
emitter = CrewAICapsuleEmitter(operator="acme-co", developer="ops-agent@v1")  # 2

tools = [emitter.wrap(t) for t in (write_po, send_invoice)]        # 3  (wrap the consequential ones)
agent = Agent(role="AP clerk", tools=tools)
```

Wrap the tools that **act** (writes, sends, payments); leave pure-read tools
unwrapped if you don't need them on the record.

## Or tell your coding agent

> Add `capsule-emit` to our CrewAI setup. `pip install capsule-emit`, create one
> `CrewAICapsuleEmitter(operator="<our-org>", developer="<this-agent>@<version>")`,
> and pass every **state-changing** tool through `emitter.wrap(...)` before it's
> added to an Agent (use the return value for function tools; `wrap()` patches
> BaseTool subclasses in place). Don't wrap read-only tools, don't change tool
> behavior. Show me the diff first.

## Notes

- **No effect block by default.** Wrapped tools seal capsules with no `effect` key;
  the *dispatched → confirmed* chain requires an explicit `seal(..., confirms=...)` call. This adapter
  records the action; effect-chain coverage is the boundary/gate layer above it.
- Input/output are digest-committed automatically.
- `model=` isn't auto-captured (the wrapper sees the tool, not the LLM) — pass it
  explicitly if you need it sealed.
- Idempotency: wrapping the same `BaseTool` twice would seal twice per call — wrap
  once.

**Since 0.5.0:** the top-level producer verb is `seal()` (the former `emit()` was
renamed). The adapter class and `wrap()` are unchanged; only the standalone call you
drop to for an explicit effect-chain link is now `seal()`.
