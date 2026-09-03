# OpenAI Agents SDK

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

agent = Agent(name="purchasing", model="gpt-5", tools=[...])
result = await Runner.run(agent, "price the ACME lot")
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
result = await Runner.run(agent, "price the ACME lot", hooks=hooks)
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
| registration | `Runner.run(hooks=...)` | `add_trace_processor(...)` |

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

## Quickstart

```bash
pip install "capsule-emit[openai-agents]"
python examples/openai-agents-listener/demo.py
```

Hermetic — **no API key and no provider call**. The demo drives real
`Runner.run` turns against `agents.testing.ScriptedModel`, the SDK's own shipped
deterministic test double, so the runner, tool executor, tracing pipeline and
lifecycle hooks are all the real thing. It shows concurrent tool calls, the same
failing tool seen by both surfaces (the processor seals `failed`; the hooks can
only record a return), and a sensitive-data-off run, then ends with an offline
`verify()` over every capsule and a `capsule-emit evidence` render.

## Version

Everything above was read from, or measured against, the released
`openai-agents` wheel rather than documentation: the `[openai-agents]` extra
declares `openai-agents>=0.22`, and **0.22.0** is the version the span-ordering
behavior, the `trace_include_sensitive_data` gating, the absent `on_tool_error`,
and the `SpanError` failure path were each verified against.

> **Dependency note.** `openai-agents` requires `mcp<3`, while capsule-emit's
> separate `[mcp]` extra currently declares `mcp>=1.0` with no upper bound and
> `capsule_emit.server` needs `mcp<2`. Installing both extras can resolve `mcp`
> to 2.x and break `capsule_emit.server`. Until the `[mcp]` extra is bounded,
> pin `mcp<2` when you install both; `openai-agents` 0.22.0 and `mcp` 1.29.1
> were verified to coexist.
