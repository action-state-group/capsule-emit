# Hermes adapter — `HermesCapsuleEmitter` (and the "any custom loop" pattern)

Your own agent loop's logs tell *you* what it did. A capsule turns each tool
run into a record built for **someone who doesn't already trust you** — your
customer, their CISO, an auditor, the other side of a deal — sealed by one call
at your tool-execution boundary, including the actions you refused.

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

1. **One record per tool run, sealed by one call where your loop has the inputs
   and the result in hand.** `emitter.after_tool(name, inputs, result)` seals
   verdict `executed` with the inputs and the result digested and effect
   `dispatched` — the tool ran; nothing in that record claims the outcome
   landed. You say more only when you can see more: `effect_status="confirmed"`
   for an effect you observed (it requires a non-`None` result, or it raises),
   `verdict="blocked"` with `effect_status="planned"` for an action your loop
   refused (a blocked call cannot carry a dispatched effect; the adapter raises
   if you try). Nothing is inferred: this adapter has no decorator and no
   callback, so what it records is exactly what you told it at the boundary —
   which is also why it is the pattern to copy for any framework not covered
   here.
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

There is no framework to install — the proof is the wiring, run as-is. It
seals three records the way a loop would: a lookup, a charge whose effect the
loop observed, and a wire it refused; then it runs the CLI verifier over the
ledger:

```python
import os, pathlib, subprocess, sys, tempfile

os.environ.setdefault("CAPSULE_WITNESS", "off")  # zero egress for the proof
from capsule_emit.adapters.hermes import HermesCapsuleEmitter

ledger = pathlib.Path(tempfile.mkdtemp()) / "ledger.jsonl"
emitter = HermesCapsuleEmitter(operator="acme-co", developer="my-loop@v1",
                               ledger=str(ledger), anchor=False)

emitter.after_tool("get_price", {"sku": "SKU-1"}, {"price": "12.00"})
emitter.after_tool("charge_card", {"amount": 4000}, {"txn": "t-1"},
                   effect_status="confirmed")          # an effect you observed
emitter.after_tool("wire_funds", {"amount": 10_000_000}, None,
                   verdict="blocked", effect_status="planned")  # a refusal

print(subprocess.run([sys.executable, "-m", "capsule_emit.cli", "verify",
                      "--store", str(ledger)], capture_output=True, text=True).stdout)
```

```
  VALID
  VALID
  VALID

3/3 VALID
```

Three records, each `VALID` under the digest recompute and its producer signature. `pip install capsule-emit` is the only dependency. In your loop the
wiring is the one `after_tool` line at the boundary — where to put it, and the
two options, under [Reference](#reference) below.

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
property is the part you turned off. The `operator` and `developer` you pass to the emitter seal into the hash-chained record permanently — use a
role/version tag, not personal data. Inputs and outputs are sealed as digests:
data minimization, not confidentiality — a digest of a low-entropy value can be
recovered by enumeration.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace,
  and here every record is one your loop chose to write. Closing that is a
  separate consistency check against an independent log.
- **Dispatched is not confirmed.** `after_tool`'s default record says the tool
  ran; `dispatched → confirmed` is an explicit chain you build with
  `emit_capsule(..., prior_capsule_id=...)` — see [Notes](#notes).
- **No model in the record unless you pass it.** The boundary sees the tool,
  not the model that chose it; `model=` is explicit.
- **It records; it never changes the call.** Your loop already ran the tool
  when it calls `after_tool`. Deny belongs to your gate layer, and a refusal
  is recorded only because you passed `verdict="blocked"`.
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

`HermesCapsuleEmitter` is the most explicit adapter: there's no decorator and no
callback — you call `after_tool(...)` yourself at the point a tool finishes. That
makes it the **general pattern for any custom agent loop** that doesn't have a
decorator seam or a callback bus.

```python
from capsule_emit.adapters.hermes import HermesCapsuleEmitter

emitter = HermesCapsuleEmitter(operator="acme-co", developer="hermes-agent@v1")
```

## Where to put the call

You insert `after_tool(...)` at your **tool-execution boundary** — the line where
your loop has just run a tool and has both the inputs and the result in hand. There
are two natural places:

### Option A — in the central dispatcher (every tool)

If your loop runs tools through one function, put it there once and *every* tool
call seals:

```python
def execute_tool(name, inputs):      # your dispatcher
    return {"ok": True}

def run_tool(name, inputs):
    result = execute_tool(name, inputs)
    emitter.after_tool(name, inputs, result)   # seals every tool
    return result
```

### Option B — around a single consequential call (targeted)

If you only want to seal the actions that matter, call it at that one site:

```python
def charge_card(amount: int) -> dict:  # a consequential call
    return {"txn": "t-1"}

result = charge_card(amount=40_00)
emitter.after_tool("charge_card", {"amount": 40_00}, result,
                   effect_status="confirmed")   # this one effect, on the record
```

**The difference:** Option A (in the dispatcher) is "seal everything, one place";
Option B (at the call site) is "seal exactly the actions that count." Hermes gives
you `verdict=` and `effect_status=` per call, so it's also where you record a
**refusal** (`verdict="blocked"`) or a **confirmed** effect.

## Add it yourself

```python
from capsule_emit.adapters.hermes import HermesCapsuleEmitter      # 1
emitter = HermesCapsuleEmitter(operator="acme-co", developer="hermes-agent@v1")  # 2

def execute_tool(name, inputs): return {"ok": True}                # your dispatcher
name, inputs = "get_price", {"sku": "SKU-1"}
result = execute_tool(name, inputs)
emitter.after_tool(name, inputs, result)                           # 3  (one line at the boundary)
```

## Or tell your coding agent

> Add `capsule-emit` to our custom agent loop. `pip install capsule-emit`, create one
> `HermesCapsuleEmitter(operator="<our-org>", developer="<this-agent>@<version>")`,
> and call `emitter.after_tool(name, inputs, result)` at our tool-execution boundary
> so each consequential tool run is sealed. For a blocked/denied action pass
> `verdict="blocked", effect_status="planned"`; for an observed effect pass `effect_status="confirmed"`.
> Don't change tool behavior. Show me the diff first.

## Notes

- This is the adapter to copy if your framework isn't covered — one call at the
  boundary is all any adapter ultimately does.
- **Dispatched by default, never confirmed by default.** `after_tool()` seals
  `effect={"type": <tool_name>, "status": "dispatched"}` unless you pass
  `effect_status=`. The *dispatched → confirmed* chain requires explicit calls;
  this adapter seals what happened, not whether the real-world effect completed.
- Input/output are digest-committed automatically; `model=` is explicit (pass it to
  `emit_capsule`/`emit` if you need it sealed).
- To **chain** *dispatched → confirmed*, drop to the base `emit_capsule(..., prior_capsule_id=<id>)`
  — `after_tool` doesn't take a parent id, but it returns an `EmitResult`
  whose `.capsule_id` is the parent you pass. And `effect_status="confirmed"` needs a non-`None`
  `tool_output` (a confirmed effect requires a response digest), or it raises — see
  [anatomy](../anatomy.md).
