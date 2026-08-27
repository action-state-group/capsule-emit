# Agno

`capsule-emit[agno]` ships `AgnoCapsuleListener` — a tool hook that seals a
planned → outcome chain around every tool call an agno agent makes.

```python
from agno.agent import Agent
from capsule_emit.adapters.agno_listener import AgnoCapsuleListener

listener = AgnoCapsuleListener(operator="acme-co", developer="my-agent@v1")
agent = Agent(model=..., tools=[...], tool_hooks=[listener.hook])
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
configured. When anchoring is enabled without `anchor_wait`, a row reports that
a statement was **submitted** to the transparency service — a confirmed
registration is a separate outcome, and `anchor_wait` is what makes
`EmitResult.anchored` reflect one. None of this replaces review.

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

## Quickstart

```bash
pip install "capsule-emit[agno]"
python examples/agno-listener/demo.py
```

Hermetic — no LLM key, no live services. It drives real agno tool calls
(success, failure, and a cache hit), then ends with an offline `verify()` over
every capsule and a `capsule-emit evidence` render.

## Version

The hook contract above was read from the released agno wheel, not from docs:
the `[agno]` extra pins `agno>=3.0.0`, and `3.0.0` is the version the
continuation binding, the hook-raises-fails-the-tool behavior, and the
cache-hit replay behavior were each verified against.
