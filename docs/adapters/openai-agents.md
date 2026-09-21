# OpenAI Agents SDK adapter — `OpenAIAgentsCapsuleProcessor` / `OpenAIAgentsCapsuleHooks`

Your Agents SDK traces and `RunHooks` tell *you* what your agent did. A capsule
turns each tool call into a record built for **someone who doesn't already trust
you** — your customer, their CISO, an auditor, the other side of a deal —
including the calls that raised, and with each record saying what its own
observation surface could not see.

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

1. **The commitment and its outcome are both in the record — and each record
   says what its surface could not see.** Two listeners, both on the SDK's own
   extension points. The `TracingProcessor` seals a `planned` record when the
   function-tool span starts and chains the outcome when it ends: a clean span
   seals `confirmed`; a `SpanError` seals `failed` (the outcome observed at the
   span boundary, not the state of the world). The `RunHooks` listener seals
   `planned` from `on_tool_start`, with the arguments, and chains `on_tool_end`.
   Neither is a superset of the other, and each record carries the gap: the
   processor's `planned` commits to the tool identity, not the arguments
   (`args_observable: false` — the SDK assigns the span's input after the span
   starts), and the hooks' outcome records a *return*, not a success
   (`verdict_note` — `RunHooksBase` has no `on_tool_error`, and a raising tool reaches `on_tool_end` as an ordinary string, by
   the SDK's default failure handler). Pairing is by `span_id` or
   `tool_call_id`, never arrival order, so it stays correct when one model turn
   fans several tool calls out concurrently.
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

The adapter ships a runnable demo — no API key, no provider call, witness off, anchor pointed at a local stub, four real `Runner.run` turns
against `agents.testing.ScriptedModel`, the SDK's own shipped deterministic test
double, so the runner, the tool executor, the tracing pipeline and the lifecycle
hooks are the real thing — every record verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[openai-agents]"   # openai-agents 0.22 or newer; Python 3.10+
python examples/openai-agents-listener/demo.py
```

You'll watch it seal `planned → confirmed` for two concurrent `get_price`
calls paired by `span_id`; `planned → failed` for a `submit_order` that raises,
seen by the processor, where `span.error` is authoritative; `planned →
confirmed` for the *same* raising tool seen by the hooks, with `verdict_note`
saying it records a return, not a success; and a run with sensitive data off,
where the payload is absent and recorded as absent (`payload_withheld: true`),
never passed off as empty. Ten records, every one `VALID` under the offline composed check (digest recompute and producer signature),
then a fail-closed `capsule-emit evidence` render. The demo seals to a
throwaway ledger and checks it for you. In your own app the wiring is one line
per surface — `set_trace_processors([OpenAIAgentsCapsuleProcessor(operator=..., developer=...)])`
as the demo does (`add_trace_processor` keeps the SDK's default exporter
shipping traces to OpenAI's backend), or
`Runner.run(agent, ..., hooks=OpenAIAgentsCapsuleHooks(operator=..., developer=...))`
— with which to choose, and why, under [Reference](#reference) below. With
tracing disabled (`RunConfig(tracing_disabled=True)` or
`OPENAI_AGENTS_DISABLE_TRACING=1`) the processor surface is silent and the
hooks are the surface that still seals.

## Network behavior

By default the listeners run an async **checkpoint/witness** stream: they
periodically post a *checkpoint* — size, root hash, timestamp; **never capsule
content** — to a transparency service, and print a notice before the first
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
**zero egress**, set `CAPSULE_WITNESS=off` (the demo does) — with it off,
offline `verify` proves internal consistency only, and the anti-re-seal
property is the part you turned off. The `operator` and `developer` you pass to
a listener seal into the hash-chained record permanently — use a role/version
tag, not personal data. Inputs and outputs are sealed as digests: data
minimization, not confidentiality — a digest of a low-entropy value can be
recovered by enumeration.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace.
  The processor seals the SDK's function-tool spans; the hooks see what the
  SDK routes through `on_tool_start`/`on_tool_end`. A *local* MCP server's
  tools are wrapped as ordinary `FunctionTool`s before the run loop sees them,
  so they reach both surfaces like a `@function_tool` does (confirmed on
  openai-agents 0.22.2). Hosted tools — web search,
  file search, code interpreter, hosted MCP — run on the provider's side and
  reach neither surface, and a raw HTTP request an agent makes on the side is
  never sealed. Closing that is a separate
  consistency check against an independent log.
- **Neither surface sees everything, and the two are not merged.** The
  processor's `planned` record has no arguments; the hooks cannot tell a raise
  from a return; with `trace_include_sensitive_data` off the processor sees no
  payload at all. Run both and you get both properties as two independent
  chains — correlating them would mean guessing, and a guess is not evidence —
  see [Why there are two](#why-there-are-two-and-what-each-one-cannot-see).
- **It records; it never changes the call.** Tool guardrails and the approval
  flow are the SDK's; these listeners write nothing back — see
  [Observation only](#observation-only). Deny belongs to your gate layer.
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
  this via an "integrations" or "tracing processors" listing: this sits *next
  to* your traces as the evidence layer, it doesn't replace them.)

---

## Reference

`capsule-emit[openai-agents]` ships two listeners for the
[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/). Both seal a
planned → outcome chain around every tool call; both are registered through the
SDK's own documented extension points, with no fork and no monkeypatching.

## The tracing processor

`OpenAIAgentsCapsuleProcessor` is a subclass of the SDK's
[`TracingProcessor`](https://openai.github.io/openai-agents-python/tracing/)
(`agents.tracing.TracingProcessor`). It is registered with
`add_trace_processor()` — which keeps the SDK's own exporter — or with
`set_trace_processors()`, which replaces the processor list:

```python
from agents import Agent, Runner
from agents.tracing import add_trace_processor
from capsule_emit.adapters.openai_agents_listener import OpenAIAgentsCapsuleProcessor

processor = OpenAIAgentsCapsuleProcessor(operator="acme-co", developer="my-agent@v1")
add_trace_processor(processor)

# agent = Agent(name="purchasing", model="gpt-5", tools=[...])
# result = await Runner.run(agent, "price the ACME lot")   # your model and tools here
```

The processor implements `on_trace_start`, `on_trace_end`, `on_span_start`,
`on_span_end`, `shutdown` and `force_flush`. It consumes `FunctionSpanData`
spans — the SDK's function-tool spans — and ignores agent, turn, task,
generation and response spans. Registration is global, so no per-run wiring is
needed.

## The lifecycle hooks

`OpenAIAgentsCapsuleHooks` is a subclass of `agents.lifecycle.RunHooksBase`
(the class behind `RunHooks`), passed per run:

```python
from capsule_emit.adapters.openai_agents_listener import OpenAIAgentsCapsuleHooks

hooks = OpenAIAgentsCapsuleHooks(operator="acme-co", developer="my-agent@v1")
# result = await Runner.run(agent, "price the ACME lot", hooks=hooks)   # per run
```

Only `on_tool_start` and `on_tool_end` are overridden; every other
`RunHooksBase` method keeps its inherited no-op body, so a future SDK release
that adds hooks does not break this class.

## The chain

| Moment | Capsule |
|---|---|
| before the tool runs | `effect.status="planned"` — the commitment record |
| clean return | `effect.status="confirmed"`, `confirms`-chained to the planned capsule |
| tool errored | `verdict_class="errored"`, `effect.status="failed"`, chained — errors are evidence |

Pairing is on an exact identity, never on arrival order: `span.span_id` for the
processor, `ToolContext.tool_call_id` for the hooks. That matters because a
single model turn can emit several tool calls and the SDK runs them
concurrently — all of their span-start events arrive before any of their
span-end events.

## Why there are two, and what each one cannot see

This is the part worth reading before choosing. The two surfaces observe
genuinely different things, and neither is a superset of the other. What
follows was measured against the released `openai-agents==0.22.0` wheel by
running a real `Runner.run` with both a probe processor and probe hooks
registered, not inferred from documentation.

**The processor cannot commit to the arguments.** The SDK assigns
`FunctionSpanData.input` *inside* the `with function_span(...)` block — that is,
after the span has already started and therefore after `on_span_start` has
already fired. At the only moment when a planned capsule can honestly be
sealed (before the tool runs), the arguments are not on the span yet. The
processor's planned capsule therefore commits to the tool identity, not to its
arguments, and carries `args_observable: false` saying exactly that.

Both the input and output assignments are also guarded by
`RunConfig.trace_include_sensitive_data`. That flag defaults to the
`OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` environment variable (default
true), and when it is off the span carries no payload at all. The capsule then
records `payload_withheld: true`. An absent payload is recorded as absent — it
is never allowed to pass for an empty one.

**The hooks cannot certify success.** `RunHooksBase` defines
`on_llm_start`/`on_llm_end`, `on_agent_start`/`on_agent_end`, `on_handoff`,
`on_tool_start` and `on_tool_end` — there is no `on_tool_error`. A
`@function_tool` that raises is caught by its `failure_error_function` (by
default `default_tool_error_function`), which converts the exception into an
ordinary string result and hands it to the model; the run does not raise.
`on_tool_end` therefore fires with a plain `str` that is indistinguishable from
a tool that legitimately returned that text. Matching on the error message
would be a heuristic, and a heuristic is not evidence. Every outcome capsule
sealed by the hooks carries `verdict_note` recording this: it is evidence that
the tool **returned**, not that it succeeded.

The processor has no such gap. The SDK attaches a `SpanError` to the function
span when a tool fails, so `span.error` is authoritative and the processor
seals `failed`/`errored`.

| | `OpenAIAgentsCapsuleHooks` | `OpenAIAgentsCapsuleProcessor` |
|---|---|---|
| arguments on the *planned* capsule | yes, before execution | no — not yet assigned |
| arguments at all | yes, ungated | only if sensitive data is on |
| outcome payload | yes | only if sensitive data is on |
| errored vs. returned | **no** — no error hook | **yes** — `span.error` |
| pairing key | `tool_call_id` | `span_id` |
| registration | `Runner.run(hooks=...)` | `set_trace_processors([...])` (or `add_trace_processor`, which keeps the default exporter) |

Running both is supported and gives you both properties, at the cost of two
independent chains per tool call. They are deliberately **not** auto-merged:
correlating them would mean matching on `(tool_name, arguments)`, which is
ambiguous for two concurrent identical calls with different outcomes, and a
guessed correlation is not evidence either. Binding a second listener to the
same ledger path emits a `RuntimeWarning` so the duplication is a decision
rather than a later surprise.

## Observation only

The SDK has in-path denial surfaces — tool guardrails, and the tool-approval
flow reachable from `ToolContext`. **This adapter touches none of them.** It
reads events and writes nothing back to the SDK; the span and context objects
are unchanged after both callbacks. Every capsule is stamped
`observation_mode="event_stream"` so no reader attributes an enforcement
decision to it. Deny belongs to a gate layer, not to the evidence layer.

## A listener failure cannot fail your run

`TracingProcessor`'s own contract asks processors to handle errors internally,
and the hooks are awaited on the tool path where an exception would abort the
turn. Every sealing path here is individually guarded: a failure warns
(`RuntimeWarning`) and is skipped, and can never turn a working tool call into
a failed one. The tool's own exception, where one escapes, propagates
unchanged — the listener contributes no exception of its own.

Float arguments are canonicalized to RFC 8785 decimal strings by the shared
adapter funnel, so a `{"qty": 2.5}` argument seals and chains rather than
failing closed. A payload with no canonical form at all (`NaN`, `Infinity`)
fails closed at the digest layer: it warns, seals nothing for that record, and
leaves the run unaffected.

## When an outcome cannot be confirmed

A `confirmed` effect requires a `response_digest`, which is derived from the
observed output or, failing that, from the capsule this one chains to. If a
tool returns no payload *and* the record has no parent — for instance after the
pending-call table has evicted a very old entry — there is nothing to derive one
from. The outcome is then sealed with `effect.status="dispatched"` (it went out,
the outcome is unconfirmed) and carries `outcome_unconfirmable: true`, rather
than claiming a `confirmed` that nothing supports or dropping the record
entirely.

## What is and is not claimed

Inputs and outputs are **digested, never stored**: the ledger carries
`agent_input_digest` and `agent_output_digest` and no raw values.

Each capsule is **sealed** and verifies offline — content digests and chain
links over what the listener recorded. `verify()` checks structure and
consistency: it proves the record's integrity, not that the tools executed, and
not that a third party has countersigned anything. Capsules are self-attested
(`assurance.attestation_mode = "self_attested"`) unless a stronger mode is
configured. When anchoring is enabled without `anchor_wait`, a row reports that
a statement was **submitted** to the transparency service — a confirmed
registration is a separate outcome, and `anchor_wait` is what makes
`EmitResult.anchored` reflect one. None of this replaces review.

## Configuration

Both listeners accept the shared adapter configuration — `operator`,
`developer`, `ledger`, `anchor`, `anchor_url`, `anchor_wait`, `model`,
`max_results` — plus `max_pending` (the bound on remembered planned-capsule ids,
default 256). The core is exposed as `listener.core`, with `listener.last` and
`listener.results` passthroughs.

## Testing without the SDK

Sealing logic lives in `OpenAIAgentsListenerCore`, whose `open_call` /
`close_call` take plain values keyed by an opaque call key. The full behavior is
exercised without the SDK installed; the tests that drive a real `Runner.run`
are `importorskip`'d.

## Version

Everything above was read from, or measured against, the released
`openai-agents` wheel rather than documentation: the `[openai-agents]` extra
declares `openai-agents>=0.22`, and **0.22.0** is the version the span-ordering
behavior, the `trace_include_sensitive_data` gating, the absent `on_tool_error`,
and the `SpanError` failure path were each verified against. The 10-minute proof above was re-run on **0.22.2**
with the released `capsule-emit` 0.8.1 wheel.

> **Dependency note.** `openai-agents` requires `mcp<3`, while capsule-emit's
> separate `[mcp]` extra currently declares `mcp>=1.0` with no upper bound and
> `capsule_emit.server` needs `mcp<2`. Installing both extras can resolve `mcp`
> to 2.x and break `capsule_emit.server`. Until the `[mcp]` extra is bounded,
> pin `mcp<2` when you install both; `openai-agents` 0.22.0 and `mcp` 1.29.1
> were verified to coexist.
