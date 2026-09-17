# Strands Agents adapter — `StrandsCapsuleListener`

Your Strands hooks tell *you* what your agent did. A capsule turns each tool
call into a record built for **someone who doesn't already trust you** — your
customer, their CISO, an auditor, the other side of a deal — including the
calls that raised and the calls a hook cancelled.

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
   calls that raised and the calls a hook cancelled.** `BeforeToolCallEvent`
   seals a `planned` record; `AfterToolCallEvent` chains the outcome to it: a
   clean `ToolResult` seals `confirmed`; an error result or a raise seals
   `failed` (the outcome observed at the hook boundary, not the state of the
   world); and when another hook set `cancel_tool` on the before-event, the
   record seals `blocked` with the effect left `planned` — the SDK does not
   invoke a cancelled tool, and this listener, which never sets `cancel_tool`,
   attests that the cancellation was someone else's. Pairing is by the SDK's
   own `toolUseId`, so it stays correct under the default
   `ConcurrentToolExecutor`, where every tool call in a turn is its own asyncio
   task and events interleave freely.
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

One thing Strands adds: a hook can set `AfterToolCallEvent.retry` and the
executor re-runs the call under the *same* `toolUseId`. From the second attempt
on, the record carries `strands_attempt` and `strands_retry_of` (the previous
attempt's planned capsule), so two attempts never read as two independent calls.

## The 10-minute proof

The adapter ships a runnable demo — no LLM key, no live service, witness off,
four real `strands.Agent` runs against a scripted `strands.models.Model`
subclass, so the event loop, the concurrent executor and the hook registry are
the real thing: two concurrent tool calls, a raising tool, a call cancelled in
path by a different hook, and a hook-forced retry — every record verified
offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[strands]"
python examples/strands-listener/demo.py
```

You'll watch it seal `planned → confirmed` for `get_price` and `get_stock`
running concurrently, `planned → failed` for the `submit_order` that raises,
`planned → blocked` marked `cancelled-by-another-hook` for the order a second
hook refused (the tool never ran), and two chained pairs for the retried
`get_price` with the second marked `attempt-2-of` the first. Twelve records,
every one `PASS` on the offline payload verifier (the CLI line adds the
signature check), then a fail-closed `capsule-emit evidence`
render. The demo seals to a throwaway ledger and checks it for you; the one-line
wiring for your own agent is under [Reference](#reference) below.

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
The `operator` and `developer` you pass to the listener seal into the
hash-chained record permanently — use a role/version tag, not personal data.
Inputs and outputs are sealed as digests: data minimization, not
confidentiality — a digest of a low-entropy value can be recovered by
enumeration.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace.
  The listener only seals calls that raise `BeforeToolCallEvent` /
  `AfterToolCallEvent`; a raw HTTP request an agent makes on the side is never
  sealed, and model calls and multi-agent node events are deliberately not
  subscribed. Closing that is a separate consistency check against an
  independent log.
- **It records refusals; it never makes them.** `cancel_tool` is writable and
  this listener never writes it — see
  [Observation only](#observation-only-and-why-that-is-a-choice).
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
  this via a catalog or "integrations" listing: this sits *next to* your traces
  as the evidence layer, it doesn't replace them.)

---

## Reference

`capsule-emit[strands]` ships `StrandsCapsuleListener` — a `HookProvider` that
seals a planned → outcome chain around every tool call a Strands agent makes.

```python
from strands import Agent
from capsule_emit.adapters.strands_listener import StrandsCapsuleListener

listener = StrandsCapsuleListener(operator="acme-co", developer="my-agent@v1")
agent = Agent(model=None, tools=[], hooks=[listener])  # your model and tools here
```

`hooks=[...]` is a public `Agent` constructor kwarg (`strands/agent/agent.py`
in the 1.54.0 wheel). There is no fork, no subclass and no monkeypatch: the SDK
dispatches on `isinstance(hook, HookProvider)` (`agent.py`) and `HookProvider`
is a `@runtime_checkable` Protocol (`strands/hooks/registry.py`), so the check
is structural — it tests for a `register_hooks` attribute and nothing more. You can
also register against a live agent with `agent.hooks.add_hook(listener)`.

| Moment | Capsule |
|---|---|
| `BeforeToolCallEvent` | `effect.status="planned"` — the commitment record |
| clean `ToolResult` | `effect.status="confirmed"`, `confirms`-chained to the planned capsule |
| error result or tool exception | `verdict_class="errored"`, `effect.status="failed"`, chained — errors are evidence |
| cancelled by another hook | `verdict_class="blocked"`, effect stays `"planned"`, chained — refusals are evidence too |

## Observation only, and why that is a choice

`BeforeToolCallEvent.cancel_tool` is writable (`strands/hooks/events.py`) and
the executor honours it **in path**: setting it short-circuits execution and
substitutes an error tool result (`strands/tools/executors/_executor.py`).
That makes this hook surface deny-capable.

**This listener never writes it.** Recording and refusing are different jobs, and
mixing them makes the record less trustworthy, not more: an evidence layer that
can also change outcomes has to be trusted about its own behaviour before its
records mean anything. Deny belongs to the gate layer.

What the listener does do is record a cancellation that *somebody else* performed.
When another hook cancels a call, the outcome capsule carries
`verdict_class="blocked"` with `strands_cancelled_by_hook: true` and
`observation_mode: "event_stream"` in its compute attestation — so a reader can
see that a refusal took effect **and** that capsule-emit was not the thing that
refused.

The effect record stays `"planned"` on that capsule rather than taking an ad-hoc
`"cancelled"` status. The reserved effect statuses are
`planned | dispatched | confirmed | failed | reverted` (§5.2); an unrecognised
status derives `effect_mode: "dispatched_unconfirmed"`, which would claim the tool
dispatched when it never ran. `planned` is the spec's carve for exactly this: a
call that was committed to and never executed.

## Pairing, and why concurrency is safe

`BeforeToolCallEvent` and `AfterToolCallEvent` are separate events, so the planned
capsule id has to be carried from one to the other. The key is
`tool_use["toolUseId"]` — the SDK's own per-call identifier
(`strands/types/tools.py`). No FIFO heuristic, no `(tool, args)` fingerprint.

That matters because the default executor is `ConcurrentToolExecutor`
(`strands/tools/executors/concurrent.py`): every tool call in a model turn runs
as its own asyncio task, so before/after events for different tools interleave
freely. Two calls to the *same* tool with the *same* arguments in one turn still
have distinct `toolUseId`s, and each outcome chains to its own commitment record.

`max_pending` (default 256) bounds the pairing table, so a run that sees
before-events without matching after-events cannot grow it without bound.

## Retries — the nearest thing Strands has to replay

There is no result cache at the hook boundary, but there is a retry loop.
`AfterToolCallEvent.retry` is writable by any hook, and when it is set the executor
discards the result and re-enters its loop (in `_executor.py`),
firing `BeforeToolCallEvent` **again for the same `toolUseId`**.

From attempt 2 onward the listener stamps `strands_attempt` (1-based) and
`strands_retry_of` (the previous attempt's planned capsule id) into the compute
attestation, so two attempts do not read as two independent calls. Pass
`include_attempt_marker=False` to switch it off.

The listener deliberately does **not** report `AfterToolCallEvent.retry` as it saw
it. After-events use reverse callback ordering, and any hook may flip that flag
after ours runs, so reading it would be a partial observation dressed up as a fact.
A second before-event for the same id is not a partial observation — it is the
executor having actually re-run the call. That is what gets recorded.

## A listener failure cannot fail your agent

`HookRegistry.invoke_callbacks_async` catches only `InterruptException`; every
other exception from a callback propagates. Two consequences, both verified in the
1.54.0 wheel:

- The **before**-hook is invoked *outside* the executor's `try` block
 (in `_executor.py`), so a raising before-callback aborts the entire tool stream.
- The **after**-hook is invoked *inside* it. A raising after-callback is
 caught by `except Exception as e`, which then builds an error result and
 invokes the after-hook **again** — where it raises a second time,
 uncaught.

Either way a careless listener turns a working tool call into a failed agent turn,
and the after case double-fires. Every sealing path in this adapter is therefore
individually guarded: failures warn (`RuntimeWarning`) and are skipped. Raw floats
in tool payloads fail closed at the digest layer — no capsule for that record, a
warning, and the agent run unaffected. The test suite proves this against the real
executor rather than asserting it in prose.

## Sync callbacks on an async surface

The callbacks this provider registers are synchronous. Strands supports async
callbacks on the tool path (`invoke_callbacks_async` awaits coroutine callbacks,
`registry.py`), but the sync dispatcher `invoke_callbacks` raises `RuntimeError`
if *any* registered callback is async. Sealing is a local append, not a
network wait, so sync callbacks cost nothing on the async path and keep the provider
usable on every dispatcher, present and future.

## Binary tool results

A `ToolResult` content block (`strands/types/tools.py`) holds exactly one of
`text`, `json`, `image`, `document` or `video`. Text and json blocks are evidence
and are digested as-is; image/document/video blocks are raw `bytes`, which no
canonical JSON encoding accepts. Those blocks are replaced with
`{"image": "<omitted:N bytes>"}` before digesting.

Without that projection an image-returning tool would fail the digest and silently
produce no outcome capsule at all — fail-closed in the wrong place.

## What is and is not claimed

Inputs and outputs are **digested, never stored**: the ledger carries
`agent_input_digest` and `agent_output_digest` and no raw values. Digesting is
data-minimization and integrity, not confidentiality — a digest of a low-entropy
value can be recovered by enumeration, so treat an anchored ledger accordingly.

Each capsule is **sealed** and verifies offline — content digests and chain links
over what the listener recorded. `verify` checks structure and consistency: it
proves the record's integrity, not that the tools executed, and not that a third
party has countersigned anything. Capsules are self-attested
(`assurance.attestation_mode = "self_attested"`) unless a stronger mode is
configured. When anchoring is enabled without `anchor_wait`, a row reports that a
statement was **submitted** to the transparency service — a confirmed registration
is a separate outcome, and `anchor_wait` is what makes `EmitResult.anchored` reflect
one. None of this replaces review.

## Configuration

`StrandsCapsuleListener` accepts the shared adapter configuration — `operator`,
`developer`, `ledger`, `anchor`, `anchor_url`, `anchor_wait`, `model`,
`max_results` — plus `include_attempt_marker` and `max_pending`. The core is
exposed as `listener.core`, with `listener.last` and `listener.results`
passthroughs.

The model is captured from the agent when it exposes one: every first-party
provider returns `model_id` from `Model.get_config()` (verified on the bedrock, openai and anthropic providers), and the provider
name is the model class's module leaf. A model that answers neither is skipped
rather than guessed at.

Only the two tool-call events are registered. `BeforeModelCallEvent` /
`AfterModelCallEvent` (`events.py` / ) and the multi-agent
`Before/AfterNodeCallEvent` ( / ) exist and are deliberately not
subscribed: model calls are volume, not evidence, and the node-level surface is a
separate design question.

## Testing without strands

Sealing logic lives in `StrandsListenerCore`, whose `on_before_tool_call(event)` /
`on_after_tool_call(event)` take duck-typed event objects. The full behavior is
exercised without strands installed; the tests that drive a real `strands.Agent`
are `importorskip`'d.

## Version

The hook contract above was read from the released wheel, not from docs: the
`[strands]` extra pins `strands-agents>=1.54.0,<2`. The API surface described
here was verified against `strands-agents` 1.54.0; the 10-minute proof above was
re-run on 1.56.0 with the released `capsule-emit` 0.8.1 wheel.
