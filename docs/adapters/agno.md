# Agno adapter — `AgnoCapsuleListener`

Your Agno agent's tool hooks tell *you* what it did. A capsule turns each tool
call into a record built for **someone who doesn't already trust you** — your
customer, their CISO, an auditor, the other side of a deal — including the calls
that raised.

That's the difference between a log and a record. A log is for you. A record is
for the person who has to believe you. Traces answer "what happened?" for the
team that owns the trace; they don't answer "can a stranger confirm this months
later?", because the party that ran the agent also holds and can rewrite the
trace. A capsule is content-addressed and checkable against the capsule format
([`draft-mih-scitt-agent-action-capsule`](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/), an individual
IETF Internet-Draft, not a WG document) by anyone holding the file. Altering a
record's content changes its id; the external checkpoint, below, pins the ids
as of the last accepted checkpoint — for everyone, the key holder included.

## What you get, in three claims

1. **The commitment and its outcome are both in the record — including the
   calls that raised.** The hook seals a `planned` record *before* the tool
   runs and chains the outcome to it: a clean return seals `confirmed`; a tool
   that raises seals `failed` and re-raises the same exception unchanged. That
   attests the outcome observed at the hook boundary, not the state of the
   world — a raise on the response path after a side effect landed is still
   `failed`. Because Agno hooks are middleware — the hook wraps the call and
   holds the planned id as a local variable — the chain link is structural,
   with no pairing table to get wrong under concurrency. An outcome whose own
   `planned` seal failed is written unchained and marked `unchained_reason` —
   not evidence of a commitment.
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

Because Agno can serve a repeat call from its tool cache — the hook still runs,
the tool body doesn't — the listener never claims a fresh execution it can't
see. When an identical `(tool, arguments)` call was already confirmed by this
listener, the repeat's record carries `agno_replay_of` pointing at the earlier
confirmed capsule: *an identical call was already confirmed; Agno may have
served this one from cache.* A pointer for the reader, not an observation of the cache — and the
"identical" judgment is the listener's own, from a fingerprint it keeps in a
bounded in-memory table; it is not something a reader recomputes from the ledger.

## The 10-minute proof

The adapter ships a runnable demo — witness off, anchor pointed at a local stub
(no external network), real Agno `FunctionCall.execute()` tool calls, one that
succeeds, one that raises, and one served from Agno's cache, every record
verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[agno]"
python examples/agno-listener/demo.py
```

You'll watch it seal `planned → confirmed` for `get_price`, `planned → failed`
for `submit_order` (sealed as evidence, then the `RuntimeError` propagates as
Agno would raise it), and `planned → confirmed` marked `replay-of` for the
cached repeat — the demo's own counter shows the tool body ran once for two
`get_price` calls; the record shows only that an identical call was already
confirmed. Every record `PASS` on offline verify, then a fail-closed
`capsule-emit evidence` render. The demo seals to a throwaway ledger and checks
it for you. In your own agent the wiring is one line —
`Agent(..., tool_hooks=[listener.hook])` for `run`, `tool_hooks=[listener.async_hook]`
for `arun` (Agno's hooks are direction-specific: the wrong one records nothing and
only logs a warning) — with the hook contract and options
under [Reference](#reference) below.

## Network behavior

By default the listener runs an async **checkpoint/witness** stream: it
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
is what raises the bar against a dishonest witness. For a first local run with **zero egress**, set
`CAPSULE_WITNESS=off` — with it off, offline `verify` proves internal
consistency only, and the anti-re-seal property is the part you
turned off.
`operator` and `developer` seal into the hash-chained record permanently — use a
role/version tag, not personal data.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace.
  The listener only seals calls that pass through Agno's tool-hook chain; a
  raw HTTP request an agent makes on the side is never sealed, a hook
  registered on the sync path never sees calls driven through `arun`/`aexecute`
  (register `listener.async_hook` for those), and a call that stops before the
  hook chain — a `requires_confirmation` tool the user rejects, an
  `external_execution` tool the app runs itself — leaves no record here. Closing that is a separate
  consistency check against an independent log.
- **Tamper-evidence, not tamper-proof.** The digest catches an altered field;
  the signature catches an altered record only once you know which key to
  expect. Neither, by itself, stops the holder from re-sealing the entire chain offline — that's what the external witness is for, and why it
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
  this via an "observability integrations" listing: this sits *next to* your
  traces as the evidence layer, it doesn't replace them.)

---

## Reference

`capsule-emit[agno]` ships `AgnoCapsuleListener` — a tool hook that seals a
planned → outcome chain around every tool call an agno agent makes.

```python
from agno.agent import Agent
from capsule_emit.adapters.agno_listener import AgnoCapsuleListener

listener = AgnoCapsuleListener(operator="acme-co", developer="my-agent@v1")
agent = Agent(model=None, tools=[], tool_hooks=[listener.hook])  # your model and tools here
```

`tool_hooks` is accepted on `Agent`, on `Team`, and on the `@tool` decorator, so
the same hook can be scoped to a whole agent or to a single tool.

| Moment | Capsule |
|---|---|
| before the tool runs | `effect.status="planned"` — the commitment record |
| clean return | `effect.status="confirmed"`, `confirms`-chained to the planned capsule |
| exception | `verdict_class="errored"`, `effect.status="failed"`, chained — errors are evidence |

Use `listener.async_hook` on `arun`/`aexecute` paths. Agno skips async hooks on
sync tool calls (it logs a warning and moves on), so register the hook that
matches the path you drive.

## Why this one has no pending map

The CrewAI and LangChain listeners receive separate start/end/error callbacks
and carry a `run_id`-keyed table to pair a start with its outcome. Agno's tool
hooks are middleware: the hook receives a continuation and wraps the call, so
both records are sealed inside a single invocation and the planned capsule id
is a local variable. The chain link is structural rather than reconstructed,
and there is no pairing heuristic to get wrong under concurrency.

## The hook contract

Agno fills hook arguments **by parameter name**, not by position: it inspects
`signature(hook).parameters` and passes only the names it finds, drawn from
`agent`, `team`, `run_context`, `name`, `function_name`, `function`, `func`,
`function_call`, `args`, `arguments`.

One detail is easy to get backwards: `function`, `func`, and `function_call`
all bind to the **continuation** — the rest of the hook chain with the tool at
its centre — not to the tool's own entrypoint. Calling it is what runs the
tool. The listener's hook declares `function_name`, `function_call`,
`arguments`, and `agent`; renaming any of them would silently change what agno
passes, so they are fixed in the signature rather than collected with
`**kwargs`.

`listener.hook` returns the same object on every access, so the callable in
`Agent.tool_hooks` is the one you registered.

## A listener failure cannot fail your tool

Agno runs the hook chain inside the same `try` as the tool entrypoint. An
exception raised by a hook is therefore reported as *the tool's* failure, and
the tool never executes — a broken ledger would otherwise take a working tool
down with it. Every sealing path in this adapter is wrapped: failures warn
(`RuntimeWarning`) and are skipped. Raw floats in tool payloads fail closed at
the digest layer, which means no capsule for that record and a warning, with
the agent run unaffected.

This is a stronger requirement than LangChain's, whose callback manager absorbs
handler exceptions on its own via `raise_error=False`. The test suite proves it
against agno's real error handling rather than asserting it in prose.

The tool's *own* exception propagates unchanged — the listener seals it and
re-raises the same object.

## Replay and agno's tool cache

Agno caches tool results (`Function.cache_results`). On a cache hit **the hook
chain still runs but the entrypoint does not**. The hook boundary cannot
observe that distinction, so the listener does not claim it: `planned` means
the call was committed to, `confirmed` means a result came back for it.

When an identical `(tool, arguments)` call has already been confirmed by the
same listener, the repeat's compute attestation carries `agno_replay_of` (the
earlier confirmed capsule id) and `agno_replay_note`. That is a pointer for a
reader, not an assertion that the tool re-ran. Pass
`include_replay_marker=False` to switch it off; `max_seen` (default 256) bounds
the table.

Failed calls are not remembered as replay sources — only a confirmed outcome
can be replayed.

## What is and is not claimed

Inputs and outputs are **digested, never stored**: the ledger carries
`agent_input_digest` and `agent_output_digest` and no raw values.

Each capsule is **sealed** and verifies offline — content digests and chain
links over what the listener recorded. `verify()` checks structure and
consistency: it proves the record's integrity, not that the tools executed, and
not that a third party has countersigned anything. Capsules are self-attested
(`assurance.attestation_mode = "self_attested"`) unless a stronger mode is
configured. The checkpoint/witness stream reports its own outcome on
`EmitResult.witness_outcome` (`checkpoint_queued` until a checkpoint is
actually countersigned); a queued checkpoint is not a confirmed registration.
None of this replaces review.

### Checking the producer signature

Each record's `signature` field is a hex-encoded COSE_Sign1 envelope over the
capsule id. Authenticate it with the spec package's verifier:

```python
import os, tempfile
os.environ.setdefault("CAPSULE_WITNESS", "off")   # see Network behavior
from capsule_emit import read_ledger
from capsule_emit.adapters.agno_listener import AgnoCapsuleListener
from agent_action_capsule.producer_envelope import verify_producer_envelope

ledger = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")
listener = AgnoCapsuleListener(operator="acme-co", developer="my-agent@v1", ledger=ledger, anchor=False)
# the framework-free core takes any callable as the continuation — one planned + one confirmed record
listener.core.wrap_call("get_price", lambda **kw: {"sku": kw["sku"], "px": "12.00"}, {"sku": "SKU-9"})
records = read_ledger(ledger)

for record in records:
    envelope = bytes.fromhex(record["signature"])
    sig = verify_producer_envelope(record["capsule_id"], envelope)
    print(f"  {record['effect']['status']:9s} signature_ok={sig.ok}")
```

A tampered envelope fails with `envelope_signature_invalid`; an envelope
replayed onto a different capsule fails with `envelope_payload_mismatch`. On
success the result carries the raw Ed25519 public key that signed the record —
whether that key is authorized for the stated operator is caller policy.
`verify_capsule` proves the content is what the identifier commits to and the
chain is consistent; `verify_producer_envelope` proves who sealed it.

## Configuration

`AgnoCapsuleListener` accepts the shared adapter configuration — `operator`,
`developer`, `ledger`, `anchor`, `anchor_url`, `anchor_wait`, `model`,
`max_results` — plus `include_replay_marker` and `max_seen`. The core is
exposed as `listener.core`, with `listener.last` and `listener.results`
passthroughs.

Agno emits no chain/run lifecycle events at the tool-hook boundary, so unlike
the LangChain listener there is no `include_lifecycle` option here.

## Testing without agno

Sealing logic lives in `AgnoListenerCore`, whose `wrap_call(tool_name,
call_next, arguments)` takes a plain callable as the continuation. The full
behavior is exercised without agno installed; the tests that drive real agno
`FunctionCall.execute()` / `.aexecute()` are `importorskip`'d.

## Version

The hook contract above was read from the released agno wheel, not from docs:
the `[agno]` extra pins `agno>=3.0.0`; the continuation binding, the
hook-raises-fails-the-tool behavior, and the cache-hit replay behavior were
verified against `3.0.0`, and the 10-minute proof above was re-run on
`3.0.10` with the released `capsule-emit` 0.8.1 wheel.
