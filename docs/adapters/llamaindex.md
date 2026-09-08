# LlamaIndex

`capsule-emit[llamaindex]` ships `LlamaIndexCapsuleListener` — a span listener
that seals a planned → outcome chain around every tool call a LlamaIndex agent
makes.

```python
from llama_index.core.agent.workflow import FunctionAgent
from capsule_emit.adapters.llamaindex_listener import LlamaIndexCapsuleListener

listener = LlamaIndexCapsuleListener(operator="acme-co", developer="my-agent@v1")
listener.install()

agent = FunctionAgent(tools=[...], llm=...)
await agent.run("...")
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

## Quickstart

```bash
pip install "capsule-emit[llamaindex]"
python examples/llamaindex-listener/demo.py
```

Hermetic — no LLM key, no live services. It drives four real `FunctionAgent`
runs (parallel tool calls, a raising tool, a tool the model invented, and a
`return_direct` tool) against a scripted model, then ends with an offline
`verify()` over every capsule and a `capsule-emit evidence` render.

## Version

Every contract above was read from the released wheels, not from docs: the
`[llamaindex]` extra pins `llama-index-core>=0.14,<0.15`, and `0.14.24` (with
`llama-index-instrumentation==0.6.0` and `llama-index-workflows==2.23.3`) is the
version each claim was verified against. The upper pin is deliberately tight —
the shape-based detection would very likely survive a wider range, but "likely"
is not a version we have run.
