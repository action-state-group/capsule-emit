<!-- SPDX-License-Identifier: Apache-2.0 -->
# Google ADK adapter — `ADKCapsuleEmitter`

Your ADK agent's callbacks and event stream tell *you* what it did. A capsule
turns each tool call that passes the callback or the event tap into a record
built for **someone who doesn't already trust you** — your customer, their
CISO, an auditor, the other side of a deal — including the calls a policy
refused.

That's the difference between a log and a record. A log is for you. A record is
for the person who has to believe you. Traces answer "what happened?" for the
team that owns the trace; they don't answer "can a stranger confirm this months
later?", because the party that ran the agent also holds and can rewrite the
trace. A capsule is content-addressed and checkable against the capsule format
([`draft-mih-scitt-agent-action-capsule`](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/), an individual
IETF Internet-Draft, not a WG document) by anyone holding the file. Altering a
record's content changes its id; an accepted external checkpoint, below, gives
an outside party a reference to check those ids against.

## What you get, in three claims

1. **One record per completed call, and one per refusal you route through
   the adapter.** The adapter seals one `executed` capsule per completed tool
   call on either observation surface — `after_tool_callback` in-path, or the
   tap over the `Runner` event stream — and each capsule says which surface
   observed it (`observation_mode`: `in_path` or `event_stream`). Wrap your
   policy in `emitter.guard(...)` and a declined call seals a `blocked` capsule
   *and* returns ADK's short-circuit, so the tool never runs and the refusal is
   in the record, not just your logs. A tool that raises never reaches the after-callback; wire ADK's
   `on_tool_error_callback` to `emit_errored` and return `None` from it (a
   recovery dict makes ADK run the after-callback too, and you get a second record
   for the same attempt) — the record it seals carries verdict `executed`, the
   error as output and no effect: it says the attempt raised, not that nothing
   happened.
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
   recomputes every digest and checks every producer signature it
   finds — no account, no service, no network. It runs the format's reference
   payload verifier plus the producer-envelope check; a record carrying no
   signature at all is not failed by default — `capsule-emit verify` counts such
   records in a one-line summary after the tally, and, from 0.8.3, `--require-signature`
   fails them (`INVALID`, exit 1) for ledgers whose producer always signs ([#185](https://github.com/action-state-group/capsule-emit/issues/185)), and the one check outside it
   is the ledger's checkpoint against the transparency service — see
   [Network behavior](#network-behavior).

## The 10-minute proof

The adapter ships a runnable demo — no LLM key, no live service, real ADK
objects: a `FunctionTool` through the tool callbacks, a policy guard that
declines an over-limit order, a raise routed through the `on_tool_error_callback` shape, and real
`Event` objects fed through the event-stream tap out of order the way a
`ParallelAgent` interleaves them — every record verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[adk]"
python examples/adk-capsule/demo.py
```

You'll watch it seal `executed` for a read-only `get_price` with no effect
asserted, `executed` with `effect: dispatched` for a `write_order` whose effect
you declared once at construction, `blocked` for the order the guard refused
(and the `{'error': 'blocked by …'}` ADK receives instead of running it),
`executed` with the error as recorded output for the raise, and two
`event_stream` records paired by function-call id despite the responses
arriving in the wrong order. Every record `VALID` under the offline composed check (digest recompute and producer signature), then a
fail-closed `capsule-emit evidence` render. The demo seals to a throwaway
ledger and checks it for you; the wiring for your own agent is under
[Path 1](#path-1--tool-callbacks) and [Path 2](#path-2--event-stream-tap) below.

## Network behavior

By default the emitter runs an async **checkpoint/witness** stream: after every
100 records, or at the next seal once 900 seconds have passed (there is no
background timer), it posts a *checkpoint* — size, root hash, timestamp; **never
capsule content** — to a witness, and prints a notice before the first attempt.
The default witness is the project's public one at
`witness.agentactioncapsule.org`; `CAPSULE_WITNESS_URL` names another. Each
checkpoint the witness accepts gives an outside party a reference to check the
ledger against — as far as that party trusts the witness to be independent of
the key holder — and that comparison is the one check outside offline `verify`.
Records sealed after the last accepted checkpoint are not yet committed outside
your environment, and dropping records from the end of the ledger is invisible
to offline `verify` until the next checkpoint lands. There is no final
checkpoint at exit: the exit hook only waits for one already in flight, so a run
that seals fewer than 100 records, all within 900 seconds, posts none.
Multi-witness bundling, which the default does not do for you, is what raises
the bar against a dishonest witness. For a first local run with **zero egress**,
set `CAPSULE_WITNESS=off` (the demo does) — offline `verify` checks the same
things either way; what you turn off is the outside reference. `operator` and
`developer` seal into the record permanently — use
a role/version tag, not personal data. What the adapter takes from `tool_context` is under
[What is and isn't recorded](#what-is-and-isnt-recorded).

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually ran, or
  that every call was recorded — a record nobody wrote leaves no trace. The
  emitter seals what passes its callbacks or its event tap — and every
  `BaseTool` passes them: `MCPToolset` tools, `AgentTool`, and A2A's
  agent-as-tool wrapper all resolve to the one `_call_tool_async` site where
  `after_tool_callback` fires (confirmed on google-adk 2.9.1). A raw HTTP request
  an agent makes on the side is never sealed, and a tool that raises on the
  callback path is sealed only if `on_tool_error_callback` or your `except` calls
  `emit_errored`. Closing that is a
  separate consistency check against an independent log.
- **One record per call, not a planned/outcome pair.** Unlike adapters that
  see a call before it runs, ADK hands this one a completed call, so it seals
  the outcome, not a prior commitment; a refusal is a `blocked` record from the
  guard. The exception is a `LongRunningFunctionTool`: what reaches the
  callback is its *pending* placeholder, so that record seals as a dispatch
  (`adk_outcome: pending`), and every later response — which ADK injects as
  function-response events under the same id — seals from the event tap,
  chained to it, as an `update` unless you name the terminal one. Wire only
  the callback and nothing after the placeholder is seen — see
  [Long-running tools](#long-running-tools). Refusals and errors are yours to route, never inferred — see
  [Refusals](#refusals--blocked--denied) and [Errored tool calls](#errored-tool-calls);
  effects are declared per tool name, never inferred — see
  [Effects for consequential tools](#effects-for-consequential-tools).
- **Tamper-evidence, not tamper-proof.** The digest catches an altered field;
  the signature catches an altered record only once you know which key to
  expect. Neither, by itself, stops the holder from re-sealing the entire ledger
  offline — an accepted witness checkpoint is the outside reference for that,
  and why the witness defaults on.
- **Kinds of `verify` — don't conflate them.** Two checks live inside
  `capsule-emit verify` — the digest recompute and the producer signature — and
  both run offline. Checking the ledger's checkpoint against the transparency
  service is outside it, and that comparison is the outside check described
  under Network behavior.
  Never quote a green `capsule-emit verify` as witness verification, and never
  read a valid signature as a name: it proves the key in the record signed it,
  not who holds the key — a ledger re-signed under a fresh key passes it, so does one with the signatures stripped, unless you pass `--require-signature`
  (0.8.3).
- It is **not** observability, tracing, or a dashboard, and it carries no
  score, ranking, or reputation — it's the record and the math over it. (If you
  found this via an "integrations" listing: this sits *next to* your traces as
  the evidence layer, it doesn't replace them.)

---

## Reference

`ADKCapsuleEmitter` records one Agent Action Capsule per completed tool call in a
[Google ADK](https://google.github.io/adk-docs/) agent. It covers **both** ways ADK
apps are built — tool callbacks and the `Runner` event stream — because a
callback-only shim silently emits nothing for the (common) apps that consume the
event stream and never register callbacks.

```bash
pip install "capsule-emit[adk]"
```

Developed and verified against `google-adk` 2.3.x on a production deployment; the
10-minute proof above was re-run on `google-adk` 2.9.1 with the released
`capsule-emit` 0.8.1 wheel. Event parsing is version-tolerant (accessor methods,
else `content.parts`) to absorb ADK's cross-version drift.

## Path 1 — tool callbacks

Pass the emitter's bound callbacks to the agent:

```python
from google.adk.agents import LlmAgent
from capsule_emit.adapters.adk import ADKCapsuleEmitter

def write_order(vendor: str, total_usd: str) -> dict:
    """Place a purchase order."""
    return {"po": "PO-7777"}

def lookup_vendor(name: str) -> dict:
    """Look a vendor up (read-only)."""
    return {"vendor": name}

emitter = ADKCapsuleEmitter(
    operator="acme-co",                       # accountable tenant
    developer="po-agent@v1",                  # agent identity + version
    model={"provider": "google", "model_id": "gemini-2.0-flash"},
)

agent = LlmAgent(
    name="writer",
    model="gemini-2.0-flash",
    tools=[write_order, lookup_vendor],
    after_tool_callback=emitter.after_tool_callback,   # emits one capsule per tool call
    before_tool_callback=emitter.before_tool_callback, # optional pass-through
)
```

`after_tool_callback` emits an `executed` capsule for every completed tool call.
It returns `None` and never alters the tool response.

## Path 2 — event-stream tap

For apps that consume the `Runner` event stream and register no callbacks:

```python
async def handle(runner, uid, sid, msg):
    async for event in runner.run_async(user_id=uid, session_id=sid, new_message=msg):
        emitter.tap_event(event)     # emits a capsule per completed tool call
        ...                          # your own event handling continues
```

Or drain a whole stream (sync or async):

```python
async def drain_async(runner, **kw):
    await emitter.tap_stream(runner.run_async(**kw))   # async stream

def drain_sync(runner, **kw):
    emitter.tap_stream(runner.run(**kw))               # sync stream
```

`tap_event` pairs function-call parts (args) with function-response parts (result)
by `id` across events, so it is correct under `ParallelAgent` concurrency where
calls and responses interleave with no fixed order.

## Effects for consequential tools

Auto-wiring seals every tool call, but asserts **no** world-effect by default (a
read-only call should not manufacture one). To mark a consequential tool without
giving up the callback wiring, declare its effect once at construction — both the
callback and the event tap look it up by tool name:

```python
emitter = ADKCapsuleEmitter(
    operator="acme-co", developer="po-agent@v1",
    effects={"write_order": {"type": "write_order", "status": "dispatched"}},
)
```

Now `write_order` capsules carry the effect; every other (read-only) tool stays
effect-free. Per-call override via the base `emit_capsule(..., effect=...)` still works.

## Refusals — `blocked` / `denied`

Recording a refusal is a **policy** act, so it is not inferred automatically (this
library stays enforcement-neutral — the policy is always yours). The one-liner is
`guard()`, which wraps your predicate in a `before_tool_callback` that **records a
`blocked` capsule when the predicate declines** and returns ADK's short-circuit dict
so the tool does not run:

```python
class policy:
    @staticmethod
    def allows(tool_name: str, args: dict) -> bool:   # your policy, not the library's
        return not (tool_name == "write_order" and float(args.get("total_usd", 0)) > 1000)

before = emitter.guard(policy.allows)   # pass as LlmAgent(..., before_tool_callback=before)
# guard(pred) calls pred(tool_name, args): truthy -> run; falsy -> seal blocked + block
```

Or record it by hand from your own gate:

```python
def before_tool_callback(tool, args, tool_context):
    if not policy.allows(tool.name, args):
        emitter.emit_blocked(tool, args, tool_context, reason="policy")
        return {"error": "blocked by policy"}   # ADK: non-None short-circuits the tool
    return None
```

A `blocked` / `denied` capsule is the auditor-grade evidence that a gate worked.

## Errored tool calls

ADK's `after_tool_callback` fires only after a tool **returns**, so a tool that
*raises* produces no capsule on the callback path. ADK does give you the moment:
wire `on_tool_error_callback` (signature `(tool, args, tool_context, error)`) to
`emit_errored`, and return `None` from it — if the error callback returns a
recovery dict, ADK treats the call as succeeded and the after-callback would
seal a second, `executed` record for the same attempt:

```python
on_error = lambda tool, args, tool_context, error: emitter.emit_errored(tool, args, error, tool_context)
# LlmAgent(..., after_tool_callback=emitter.after_tool_callback, on_tool_error_callback=on_error)
```

Or seal the failed attempt from your own `except` block:

```python
tool, args, tool_context = write_order, {"vendor": "Frobozz", "total_usd": "5.00"}, None

def call_and_seal():
    try:
        return tool(**args)
    except Exception as exc:
        emitter.emit_errored(tool, args, exc, tool_context)
        raise
```

The capsule is `executed` (the attempt happened) with the exception recorded as
output, and **no effect** — a raise must not claim a consequential effect dispatched.
On the event-stream path, an error-shaped `function_response` is sealed like any
other output automatically.

Wire neither `on_tool_error_callback` nor an `except` and a raise leaves no
record. An errored capsule still carries verdict `executed` (the attempt is
real); an auditor distinguishes it by the error-shaped output, not by the verdict.

## Emit-error policy

On the auto-instrumented paths (`after_tool_callback` / `tap_event`) a failed emit is
warned (`RuntimeWarning`) and logged, **never propagated** — the record layer must not
crash the agent's tool path (same policy as the MCP adapter). A failed id-bearing emit
is left un-marked, so the other path may still seal it. Direct calls (`emit_blocked` /
`emit_denied` / `emit_errored`) raise normally; inside `guard()` the block short-circuit
is returned even if sealing the refusal fails — the gate outcome must not depend on the
record layer's health.

## What is and isn't recorded

- **No effect is asserted by default.** A tool call is recorded as `executed`
  without claiming a dispatched world-effect. Read-only tool calls should not
  manufacture effect records. For a consequential tool, attach one explicitly via
  the base `emit_capsule(..., effect={"type": "write_order", "status": "dispatched"})`.
- **`tool_context` is not serialized wholesale.** Only a minimal producer-context
  allow-list — `agent_name`, `function_call_id`, `invocation_id` (correlation
  handles, not end-user PII) — is threaded into `compute_attestation`. The ADK
  `session` (which can carry `user_id` / `session_id`) is deliberately never pulled
  into the content-addressed capsule.
- **Model** is taken from `model=` at construction (ADK does not surface the model
  in the tool-callback signature); per-call override remains available on the base.

## Caveats

- **Wiring both paths is safe for id-bearing calls.** A call carrying a
  `function_call_id` is sealed at most once — the two paths dedup by id, so a
  belt-and-braces setup does not double-count. Dedup needs an id, though: id-less
  calls can't be deduped, so a both-paths setup would double-count *those* — prefer a
  single path when your calls carry no id.
- **Id-less concurrent pairing is best-effort.** When calls carry no
  `function_call_id`, pairing falls back to a per-name FIFO queue — which prevents
  the overwrite/drop, but does not guarantee correct input→output pairing under
  true concurrency. Such a capsule is *integrity-valid and verifies*, yet a
  verifying capsule is not necessarily a correctly-paired one. Supply stable
  `function_call_id`s for exact pairing.
- **Orphan id-bearing responses never borrow id-less input.** A response carrying an
  `id` whose call was never seen (dropped stream, evicted pending entry) seals with
  unknown (`None`) input — it does not consult the id-less FIFO, so an orphan can
  never claim (and mispair) an id-less call's args.
- **Unpaired calls are bounded, not leaked.** A call whose response never arrives (a
  tool that errors without emitting a response, a dropped stream) is held only until
  the pending cap (`max_pending`, default 4096) is hit, then evicted oldest-first with
  a `logging` warning. Retained `results` history and the dedup-id set are likewise
  capped, so a long-lived runner does not grow without bound.
- **Dedup expires with the cap.** The sealed-id history is bounded by `max_pending`;
  after that many newer ids, a *very* late duplicate response could re-seal. Raise the
  cap for extremely long-lived runners if this matters to your ledger.
- **The allow-listed correlation ids are recorded in clear** (the tool payload is
  digest-only). They MUST be opaque handles — never seed `function_call_id` /
  `invocation_id` from an end-user identifier, or the allow-list re-admits into the
  permanent record the very identifier the `session` exclusion keeps out. Treat the
  `tool_context` hygiene above as adapter policy, not a spec guarantee.
- **A call sealed via the callback vs the event tap has a different `capsule_id`.**
  The callback path commits the full context allow-list; the event tap commits only
  the call id. Both verify, and dedup is by `function_call_id` (not `capsule_id`), so
  this is a content-addressing note, not a pairing bug — don't assume path-independent
  ids for the same logical call.

### Long-running tools

ADK's `LongRunningFunctionTool` returns first and finishes later: the function
returns a placeholder (typically `{"status": "pending", ...}`), the framework
emits that as the function response, and progress updates and the actual
result arrive in later turns as function-response parts carrying the **same**
`function_call_id`. `after_tool_callback` fires on the placeholder and never
on what follows, and ADK's `is_long_running` check runs *after* the callbacks
— it gates the event build, not the callback — so nothing in the callback
signature says "pending".

The emitter reads `tool.is_long_running` (callback path) or the model-call
event's `long_running_tool_ids` (tap path) and seals the placeholder as a
dispatch: verdict `executed` (the dispatch did run), `adk_long_running:
"true"` / `adk_outcome: "pending"` in `compute_attestation`, and — only when
you declared an effect for that tool — the declared type with status forced
to `dispatched`. An undeclared tool gets no effect block, exactly as on the
plain path; the adapter does not manufacture a world-effect it was not told
about.

Every later response under that id seals a further record chained to the
previous one by `prior_capsule_id`, marked `adk_outcome: "update"`. The
adapter cannot tell a progress update from the outcome — ADK injects both the
same way — so it never promotes one to "final" or to a `confirmed` effect on
its own. Pass `long_running_final=` (a predicate on the response, e.g.
`lambda r: r.get("status") == "done"`) and the response it names seals as
`adk_outcome: "final"` and closes the chain; your declared effect rides
unchanged either way. The placeholder echoed on the event stream is
recognised by its canonical form and not sealed twice.

Three consequences. A callback-only wiring records the dispatch and nothing
else — tap the event stream (`tap_event` / `tap_stream`) as well if you need
what follows. A pending record whose later responses never arrive stays a
pending record; that is the honest state. And open long-running calls are
bounded by `max_long_running` (default 1024, separate from `max_pending`);
evicting one drops that call's dedup entry *and* its long-running flag, so a
late response seals as a plain, unchained call rather than vanishing or
re-opening the chain, and the log says so. The flag a model-call event sets is
short-lived by design: it is discarded the moment that call's placeholder
seals, so it lives exactly as long as the call-to-response window and is
bounded by `max_pending` like the pairing map, not by `max_long_running`.

## Verify

`capsule-emit verify --store <ledger>.jsonl` runs the format's reference payload
verifier (every `capsule_id` recomputed from its canonical content) plus the
producer-envelope check (a COSE_Sign1 signature over that id under the key the
record names), offline. A green result means each record is internally
consistent and signed by the key it names — not that the key belongs to a
particular producer, and not that the ledger's checkpoint was accepted by the
transparency service; those are the two checks outside it.
