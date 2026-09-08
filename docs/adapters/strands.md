# Strands Agents

`capsule-emit[strands]` ships `StrandsCapsuleListener` — a `HookProvider` that
seals a planned → outcome chain around every tool call a Strands agent makes.

```python
from strands import Agent
from capsule_emit.adapters.strands_listener import StrandsCapsuleListener

listener = StrandsCapsuleListener(operator="acme-co", developer="my-agent@v1")
agent = Agent(model=..., tools=[...], hooks=[listener])
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

## Quickstart

```bash
pip install "capsule-emit[strands]"
python examples/strands-listener/demo.py
```

Hermetic — no LLM key, no live services. It drives four real `strands.Agent` runs
against a scripted `strands.models.Model` (the SDK's own test-fixture pattern), so
the event loop, the concurrent tool executor and the hook registry are all the real
thing: two concurrent tool calls, a raising tool, a call cancelled in path by a
different hook, and a hook-forced retry. It then ends with an offline `verify`
over every capsule and a `capsule-emit evidence` render.

## Version

The hook contract above was read from the released wheel, not from docs: the
`[strands]` extra pins `strands-agents>=1.54.0,<2`. The API surface described
here was verified against `strands-agents` 1.54.0 and holds on the current 1.55.x.
