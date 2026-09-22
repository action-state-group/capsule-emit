# LlamaIndex adapter — `LlamaIndexCapsuleListener`

Your LlamaIndex instrumentation tells *you* what your agent did. A capsule turns
each tool call into a record built for **someone who doesn't already trust
you** — your customer, their CISO, an auditor, the other side of a deal —
including the calls that raised and the tool the model invented.

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
   calls that raised.** The agent's `call_tool` step is a span; entering it
   seals a `planned` record, and its exit chains the outcome: a clean
   `ToolOutput` seals `confirmed`; `is_error`, a raise, or a tool the model
   named that does not exist seals `failed` (the outcome observed at the span
   boundary, not the state of the world). Pairing is by span id, which enter,
   exit and drop share by construction — stronger than the model-supplied
   `tool_id`, and correct when the agent fans a turn's tool calls out
   concurrently.
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

The adapter ships a runnable demo — no LLM key, witness off, anchor pointed at a local stub,
four real `FunctionAgent` runs driven by a scripted `FunctionCallingLLM`, so the
agent workflow, the tool executor, the concurrent fan-out and the
instrumentation dispatcher are the real thing: parallel tool calls, a raising
tool, a tool the model invented, and a `return_direct` tool — every record
verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[llamaindex]" "llama-index-workflows<2.24"
python examples/llamaindex-listener/demo.py
```

The second pin is temporary: on `llama-index-workflows` 2.24.0 (2026-09-16)
`agent.run()` raises `TypeError: unhashable type: 'FunctionAgent'` before any
tool runs, with or without this listener installed; on 2.23.x the demo is clean.
The `[llamaindex]` extra carries the same pin from 0.8.3, and
[capsule-emit#182](https://github.com/action-state-group/capsule-emit/issues/182)
tracks lifting it.

You'll watch it seal `planned → confirmed` for `get_price` and `get_stock`
running concurrently, `planned → failed` for the `submit_order` that raises and
for the tool the model named that does not exist, and `planned → confirmed` for
the `return_direct` tool. Ten records, every one `VALID` under the offline composed check (digest recompute and producer signature), then
a fail-closed `capsule-emit evidence` render. The demo seals to a throwaway
ledger and checks it for you. In your own app the wiring is one line —
`LlamaIndexCapsuleListener(operator=..., developer=...).install()` — with the
span contract and options under [Reference](#reference) below.

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
is what raises the bar against a dishonest witness. For a first local run with
**zero egress**, set `CAPSULE_WITNESS=off` — with it off, offline `verify`
proves internal consistency only, and the anti-re-seal property is the part you
turned off. The `operator` and `developer` you pass to the listener seal
into the hash-chained record permanently — use a role/version tag, not personal
data. Inputs and outputs are sealed as digests: data minimization, not
confidentiality — a digest of a low-entropy value can be recovered by
enumeration.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace.
  The listener only seals the agent's `call_tool` span; a raw HTTP request an
  agent makes on the side is never sealed, and a `FunctionTool` called directly
  outside an agent workflow is not a `call_tool` span. Closing that is a
  separate consistency check against an independent log.
- **It records; it never changes the call.** The span handler holds live
  references to the step's arguments and this listener never mutates them —
  see [Observation only](#observation-only). Deny belongs to your gate layer.
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
  (0.8.3).
- It is **not** observability, tracing, or a dashboard, and it carries no score,
  ranking, or reputation — it's the record and the math over it. (If you found
  this via an "observability integrations" listing: this sits *next to* your
  traces as the evidence layer, it doesn't replace them.)

---

## Reference

`capsule-emit[llamaindex]` ships `LlamaIndexCapsuleListener` — a span listener
that seals a planned → outcome chain around every tool call a LlamaIndex agent
makes.

```python
from llama_index.core.agent.workflow import FunctionAgent
from capsule_emit.adapters.llamaindex_listener import LlamaIndexCapsuleListener

listener = LlamaIndexCapsuleListener(operator="acme-co", developer="my-agent@v1")
listener.install()

# agent = FunctionAgent(tools=[...], llm=...); await agent.run("...")   # your tools and model here
```

`install()` registers on LlamaIndex's process-wide instrumentation dispatcher, so
it needs no change to the agent, the tools, or the run loop. To scope it
yourself:

```python
from llama_index.core.instrumentation import get_dispatcher
get_dispatcher().add_span_handler(listener.span_handler)
```

Call `listener.uninstall()` when you are done. `install()` mutates process-global
state; a library that installs a listener should take it back off again.

| Moment | Capsule |
|---|---|
| agent enters its tool step | `effect.status="planned"` — the commitment record |
| tool returns cleanly | `effect.status="confirmed"`, `confirms`-chained to the planned capsule |
| `ToolOutput.is_error` | `verdict_class="errored"`, `effect.status="failed"`, chained — errors are evidence |
| the tool step itself raises | `verdict_class="errored"`, `effect.status="failed"`, chained, error type recorded |

## Why the span surface and not the callback manager

LlamaIndex offers three plausible taps. Only one of them carries a tool payload
in the released wheel, and it is not the one the name suggests. All three were
checked against `llama-index-core==0.14.24` /
`llama-index-instrumentation==0.6.0`, by grep over the installed package and by
an executed `FunctionAgent` run.

**The legacy `CallbackManager` is dead for tools.** `CBEventType` still declares
`FUNCTION_CALL` and `AGENT_STEP`, but nothing in the wheel dispatches either —
`grep -rn 'CBEventType.FUNCTION_CALL'` matches only the two enum definitions. The
event types still emitted are LLM, QUERY, EMBEDDING, RETRIEVE, CHUNKING,
SYNTHESIZE, NODE_PARSING, TREE, SUB_QUESTION, RERANKING, TEMPLATING and
EXCEPTION. A `CallbackManager` installed on `Settings` saw **zero events** across
a complete agent run with two tool calls.

**The instrumentation *event* surface has no tool payload either.**
`AgentToolCallEvent` exists and carries `arguments` + `tool`, but it likewise has
zero dispatch sites, and there has never been a matching *result* event. What the
agent path actually emits is `WorkflowStepOutputEvent`, whose only payload field
is `output: str` — a truncated human summary, not the call.

**The instrumentation *span* surface carries both sides.** The agent's tool step
`call_tool` is wrapped by the dispatcher's `span(...)` decorator, so:

- `span_enter` receives the bound `ToolCall` — `tool_name`, `tool_kwargs`, `tool_id`;
- `span_exit` receives the returned `ToolCallResult` — the same three fields plus
  `tool_output: ToolOutput` (`content`, `raw_input`, `raw_output`, `is_error`) and
  `return_direct`;
- `span_drop` receives the exception.

Full payload on both sides, and registration is one global call.

## Pairing is by span id

Enter, exit and drop for one call share a span id by construction. That is a
stronger key than `tool_id`: the agent fans a model turn's tool calls out as
concurrent tasks, so before/after events for different tools interleave, and
`tool_id` comes from the model with no uniqueness guarantee. The `tool_id` is
still recorded on both capsules as evidence, under
`compute_attestation.llamaindex_tool_id`.

## Detection is by payload shape, not span name

`call_tool` is defined on `BaseWorkflowAgent` and re-defined on `AgentWorkflow`,
and the span id is built from `__qualname__` when the wrapped step is not a bound
method — so the span *name* is already two different strings today. A span is a
tool dispatch if its bound arguments carry an object with `tool_name` +
`tool_kwargs` + `tool_id` and **no** `tool_output`; its outcome is a tool result
if the returned object has `tool_id` **and** `tool_output`.

Both carves are load-bearing, and each one keeps the listener off an adjacent
span that would otherwise double-count:

- `aggregate_tool_results` *enters* with a `ToolCallResult` bound to the same
  parameter name — excluded by the `tool_output` carve, or it would seal a second
  planned capsule per call.
- `FunctionTool.acall` *exits* with a bare `ToolOutput`, which has no `tool_id` —
  excluded by the `tool_id` carve, or it would seal a duplicate outcome per call.

## Observation only

A span handler's return value feeds only the handler's own bookkeeping; nothing
it returns reaches the agent. The one genuine in-path capability is that
`bound_args` holds **live references** to the step's arguments, so mutating
`bound_args.arguments["ev"].tool_kwargs` would change the call that then runs.

**This listener never mutates `bound_args`, anything reachable from it, or the
result.** That is recorded here so the capability is not lost — deny belongs to
the gate layer, not the evidence layer. Every capsule is stamped
`compute_attestation.observation_mode = "event_stream"`, and a test asserts the
non-mutation directly rather than in prose.

## A listener failure cannot fail your agent — but it can go silent

The dispatcher already wraps every span-handler call in `except BaseException:
pass`. So unlike agno — where a raising hook is reported as *the tool's* failure —
a careless listener here cannot break the agent. It can only **fail silently**,
which for an evidence layer is the worse failure.

Every sealing path is therefore wrapped so that failures **warn**
(`RuntimeWarning`) instead of vanishing. Raw floats in tool payloads fail closed
at the digest layer, which means no capsule for that record and a warning, with
the agent run unaffected.

## Payload projection

Tool arguments and `ToolOutput` are projected into canonical-JSON-safe shapes
before digesting. `bytes` become `<omitted:N bytes>`; an object that no canonical
JSON encoding accepts becomes `<TypeName>` — a marker saying *an object of this
type was here*, which is honest and canonicalizable where the object itself is
neither. Without this an image-returning tool would fail the digest and silently
produce no outcome capsule at all: fail-closed in the wrong place.

`ToolOutput.raw_input` is deliberately **not** carried into the outcome capsule.
It is the *input*, already digested on the planned capsule, and digesting it again
under `agent_output_digest` would put the same bytes under two different names.

Floats are deliberately left alone rather than stringified, so they still fail
closed at the digest layer.

## Model capture

The model stamped on a tool capsule is *the most recent model observed under the
same root span as that tool call*. That is a statement about what was observed,
not an inference: the `call_tool` span carries `instance=None`, so there is no
agent or LLM handle at the tool boundary itself. In the observed lineage the LLM
span and the tool span are siblings under one run span, and the LLM span always
precedes the tool span.

Where this can mis-attribute: a multi-agent workflow whose sub-agents use
*different* LLMs under one root, running concurrently. Then "most recent under
this root" may not be the model that chose this particular call. Pass
`capture_model=False` plus an explicit `model=` if that distinction matters to
you.

## What is and is not claimed

Inputs and outputs are **digested, never stored**: the ledger carries
`agent_input_digest` and `agent_output_digest` and no raw values.

Each capsule is **sealed** and verifies offline — content digests and chain links
over what the listener recorded. `verify()` checks structure and consistency: it
proves the record's integrity, not that the tools executed, and not that a third
party has countersigned anything. Capsules are self-attested
(`assurance.attestation_mode = "self_attested"`) unless a stronger mode is
configured. When anchoring is enabled without `anchor_wait`, a row reports that a
statement was **submitted** to the transparency service — a confirmed
registration is a separate outcome, and `anchor_wait` is what makes
`EmitResult.anchored` reflect one. None of this replaces review.

A capsule records that the agent's tool step was entered and what it returned. It
does not claim the tool's side effect reached its destination; that is what
`effect_mode` is for, and these capsules carry `runtime_claimed`.

## Configuration

`LlamaIndexCapsuleListener` accepts the shared adapter configuration —
`operator`, `developer`, `ledger`, `anchor`, `anchor_url`, `anchor_wait`,
`model`, `max_results` — plus `capture_model` and `max_pending`. The core is
exposed as `listener.core`, with `listener.last` and `listener.results`
passthroughs.

`max_pending` (default 256) bounds three tables — pending planned-capsule ids,
span parent links, and per-root models — so a long run that sees enters without
matching exits cannot grow any of them without bound. The listener's own
`BaseSpanHandler` hooks all return `None`, which keeps LlamaIndex's `open_spans` /
`completed_spans` bookkeeping empty; a process-wide handler that filled those
would grow forever.

## Testing without llama-index

Sealing logic lives in `LlamaIndexListenerCore`, whose `on_span_enter` /
`on_span_exit` / `on_span_drop` take plain duck-typed values. The full behaviour
is exercised with llama-index import-blocked; the tests that drive a real
`FunctionAgent` are `importorskip`'d.

## Version

Every contract above was read from the released wheels, not from docs: the
`[llamaindex]` extra pins `llama-index-core>=0.14,<0.15`, and `0.14.24` (with
`llama-index-instrumentation==0.6.0` and `llama-index-workflows==2.23.3`) is the
version each claim was verified against; the 10-minute proof above was re-run on
`llama-index-core` 0.14.24 with the released `capsule-emit` 0.8.1 wheel and
`llama-index-workflows` 2.23.x — 2.24.0 breaks `FunctionAgent.run()` (see the
proof's pin), which is why the extra now pins below it. The upper pin is deliberately tight —
the shape-based detection would very likely survive a wider range, but "likely"
is not a version we have run.
