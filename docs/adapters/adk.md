<!-- SPDX-License-Identifier: Apache-2.0 -->
# Google ADK adapter

`ADKCapsuleEmitter` records one Agent Action Capsule per completed tool call in a
[Google ADK](https://google.github.io/adk-docs/) agent. It covers **both** ways ADK
apps are built — tool callbacks and the `Runner` event stream — because a
callback-only shim silently emits nothing for the (common) apps that consume the
event stream and never register callbacks.

```bash
pip install capsule-emit google-adk
```

Developed and verified against `google-adk` 2.3.x on a production deployment. Event
parsing is version-tolerant (accessor methods, else `content.parts`) to absorb ADK's
cross-version drift.

## Path 1 — tool callbacks

Pass the emitter's bound callbacks to the agent:

```python
from google.adk.agents import LlmAgent
from capsule_emit.adapters.adk import ADKCapsuleEmitter

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
async for event in runner.run_async(user_id=uid, session_id=sid, new_message=msg):
    emitter.tap_event(event)     # emits a capsule per completed tool call
    ...                          # your own event handling continues
```

Or drain a whole stream (sync or async):

```python
await emitter.tap_stream(runner.run_async(...))   # async stream
emitter.tap_stream(runner.run(...))               # sync stream
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
agent = LlmAgent(..., before_tool_callback=emitter.guard(policy.allows))
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
*raises* produces no capsule on the callback path. Seal the failed attempt from your
own `except` block:

```python
try:
    result = tool(**args)
except Exception as exc:
    emitter.emit_errored(tool, args, exc, tool_context)
    raise
```

The capsule is `executed` (the attempt happened) with the exception recorded as
output, and **no effect** — a raise must not claim a consequential effect dispatched.
On the event-stream path, an error-shaped `function_response` is sealed like any
other output automatically.

Two caveats: on the callback path this is a **manual opt-in** — ADK has no
"tool raised" callback, so a tool you never wrap in `try/except` leaves its raise
uncaptured. And an errored capsule still carries verdict `executed` (the attempt is
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

## Verify

Every capsule this adapter emits verifies against the reference verifier — same as
any other producer. See the repository quickstart for `verify`.
