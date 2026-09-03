# Microsoft Agent Framework

`capsule-emit[msft-agent-framework]` ships two middleware objects that seal a
planned → outcome chain around every **agent run** and every **tool call** a
[Microsoft Agent Framework](https://github.com/microsoft/agent-framework) agent makes.

They ride the framework's own public middleware surface — `Agent(..., middleware=[...])` —
so there is no fork, no monkeypatch, and no subclassing of anything you own.

## Install

```bash
pip install "capsule-emit[msft-agent-framework]"
```

Pinned to `agent-framework-core>=1.16.0,<2`. Every line reference in this page was
verified against the released **1.16.0** wheel, not the GitHub tree.

## Register

```python
from agent_framework import Agent
from capsule_emit.adapters.msft_agent_framework import capsule_middleware

agent = Agent(
    chat_client,
    "you are a procurement assistant",
    tools=[get_price, submit_order],
    middleware=capsule_middleware(operator="acme-co", developer="my-agent@v1"),
)
```

`capsule_middleware()` returns two objects over one shared core:
`CapsuleRunMiddleware` (agent seam) and `CapsuleFunctionMiddleware` (function seam).
The framework's own `categorize_middleware` routes each to its own pipeline by
`isinstance`, so a single list is all you pass.

Per-run instead of per-agent works the same way:

```python
response = await agent.run(prompt, middleware=capsule_middleware(operator="acme-co", developer="a@v1"))
```

## What you get

| Moment | Capsule |
|---|---|
| before the run / tool call | `effect.status="planned"` — the commitment record |
| clean return | `effect.status="confirmed"`, `confirms`-chained to the planned capsule |
| exception | `verdict_class="errored"`, `effect.status="failed"`, chained — errors are evidence |
| another middleware refuses | `verdict_class="blocked"`, effect left `"planned"`, chained — a refusal that took effect is evidence, not silence |

Every capsule is signed, digest-only, and independently verifiable offline. No capsule
*content* ever leaves the process. capsule-emit does witness checkpoints by default — a
signed size/root-hash/timestamp of the log, never payload — and prints a one-time notice
saying so before its first network attempt; set `CAPSULE_WITNESS=off` (or pass
`anchor=False` and `witness=False`) if you want a fully offline run.

## Run it

This is a complete, keyless program. `BaseChatClient` on its own has no
function-calling loop (`Agent` logs *"the provided chat client does not support function
invoking"*, `agent_framework/_agents.py:874`); composing the framework's own
`FunctionInvocationLayer` and `ChatMiddlewareLayer` over it gives you the real loop with
a scripted transport — no API key, no network.

```python
import asyncio
import json
import os
import tempfile
from pathlib import Path

# Fully offline: no checkpoint and no anchor leaves this process.
os.environ.setdefault("CAPSULE_WITNESS", "off")

from agent_framework import (
    Agent,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)

from capsule_emit.adapters.msft_agent_framework import capsule_middleware
from capsule_emit.verification import verify_capsule


class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """A keyless stand-in for a real provider client. Replace with OpenAIChatClient etc."""

    OTEL_PROVIDER_NAME = "scripted"

    def __init__(self, turns, **kw):
        super().__init__(**kw)
        self.model = "scripted-demo-model"
        self.turns = list(turns)
        self.index = 0

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        async def _turn():
            turn = self.turns[min(self.index, len(self.turns) - 1)]
            self.index += 1
            contents = (
                [Content.from_text(turn)]
                if isinstance(turn, str)
                else [
                    Content.from_function_call(call_id=cid, name=name, arguments=args)
                    for name, args, cid in turn
                ]
            )
            return ChatResponse(
                messages=[Message(role="assistant", contents=contents)],
                response_id=f"scripted-{self.index}",
            )

        return _turn()


def get_price(sku: str) -> str:
    """Look up the list price for a SKU."""
    return {"SKU-1": "12.00 USD"}.get(sku, "unknown")


async def main(ledger):
    client = ScriptedChatClient(
        [[("get_price", {"sku": "SKU-1"}, "call-1")], "SKU-1 is 12.00 USD."]
    )
    agent = Agent(
        client,
        "you are a procurement assistant",
        name="procurement",
        tools=[get_price],
        middleware=capsule_middleware(
            operator="acme-co", developer="my-agent@v1", ledger=ledger, anchor=False
        ),
    )
    response = await agent.run("price for SKU-1?")
    print("agent said:", response.text)


with tempfile.TemporaryDirectory() as td:
    ledger = Path(td) / "ledger.jsonl"
    asyncio.run(main(ledger))

    capsules = [json.loads(line) for line in ledger.read_text().splitlines()]
    for capsule in capsules:
        result = verify_capsule(capsule)
        parent = capsule.get("chain", {}).get("parent_capsule_id")
        print(
            f"  {'PASS' if result.ok else 'FAIL'}  {capsule['action_id'].split('/')[0]:16s}"
            f" {capsule['effect']['status']:10s}"
            f" {'chained to ' + parent[:8] if parent else 'root'}"
        )
    assert all(verify_capsule(c).ok for c in capsules)
```

Output:

```
agent said: SKU-1 is 12.00 USD.
  PASS  procurement.run  planned    root
  PASS  get_price        planned    root
  PASS  get_price        confirmed  chained to <id>
  PASS  procurement.run  confirmed  chained to <id>
```

Four capsules for one run: the run's commitment and outcome, and the tool call's
commitment and outcome nested inside it.

## The two seams

The framework offers three middleware categories (`agent_framework/_middleware.py:143`).
This adapter uses two of them:

| Seam | Base class | Context | What the capsule records |
|---|---|---|---|
| agent | `AgentMiddleware` (`_middleware.py:535`) | `AgentContext` (`:154`) | one run: the messages in, the response out |
| function | `FunctionMiddleware` (`_middleware.py:594`) | `FunctionInvocationContext` (`:270`) | one tool call: the arguments in, the result out |

`categorize_middleware` (`_middleware.py:1708`) tests `AgentMiddleware` **before**
`FunctionMiddleware`, so one object inheriting both would be silently filed as agent-only.
That is why this adapter ships two objects rather than one, and why `capsule_middleware()`
exists to hand you both.

If you only want tool-call records, keep the run middleware installed (it is what
supplies the model attribution — see below) and pass `seal_runs=False`:

```python
mw = capsule_middleware(operator="acme-co", developer="my-agent@v1", seal_runs=False)
```

## Observation only

This seam is *in path*. A middleware here can substitute `context.result`, add or remove
tools mid-run (`FunctionInvocationContext.add_tools` / `remove_tools`,
`_middleware.py:354`/`:391`), raise `MiddlewareTermination` to stop the function-calling
loop gracefully, or `MiddlewareFailure` to abort the run fail-closed.

**This adapter does none of those.** It reads the context, seals, and always calls
`call_next()`. It never writes `context.result`, never touches the live tool list, and
never originates a control-flow exception. Deny belongs to the gate layer, not to the
evidence layer. Every capsule carries `observation_mode="in_path_wrapper"` so a reader
knows the seam *could* have intervened and this component chose not to.

Every exception that comes back out of `call_next()` is re-raised unchanged. That matters
most for `MiddlewareFailure`, whose own docstring says: *"Middleware must not catch
`MiddlewareFailure` (let it propagate through `call_next()`): swallowing it converts a
fail-closed abort back into a running — and possibly unguarded — loop"*
(`_middleware.py:116`).

## A sealing failure cannot fail your agent

The two seams punish a raising middleware differently, and **both punishments are silent**:

- **Function seam.** An ordinary exception from function middleware is absorbed into a
  tool-error result and the loop keeps running
  (`_tools.py:1640-1641`: `except Exception as exc: return _function_execution_error_result(...)`).
  A careless evidence layer therefore hands the model an error the tool never produced.
  Nothing crashes, so nothing surfaces.
- **Agent seam.** `AgentMiddlewarePipeline.execute` suppresses only `MiddlewareTermination`
  (`_middleware.py:1080`); every other exception propagates out of `Agent.run` and fails
  the whole run.

Every sealing path in this adapter is individually guarded: a broken ledger or anchor
endpoint warns (`RuntimeWarning`) and is skipped. Raw floats in a tool payload fail closed
at the digest layer — no capsule for that record, a warning, and the run unaffected.

The test suite proves this against the framework's real error handling rather than
asserting it in prose, and it also pins the hazard: a deliberately careless middleware
*does* corrupt the tool result, which is why the guarantee is not optional.

## What this seam honestly cannot see

When a **downstream** middleware raises `MiddlewareTermination` or `MiddlewareFailure`, it
arrives here as an exception out of `call_next()`. The refusal is somebody else's, it took
effect, and it is sealed: `verdict_class="blocked"`, `effect.status` left at `"planned"`.

`"planned"` is deliberate and conservative. `MiddlewareTermination` can be raised *before*
a downstream middleware calls `call_next()` (a cache hit, a policy deny — the body never
ran) or *after* it (stop the loop, but this call did run). **From this seam the two are
indistinguishable.** The reserved effect-status set is
planned/dispatched/confirmed/failed/reverted, and an unknown status would derive
`effect_mode="dispatched_unconfirmed"` — a claim that something dispatched when it may not
have. Under-claiming beats over-claiming, so the capsule records `"planned"` and stamps:

| Marker | Meaning |
|---|---|
| `agent_framework_blocked_by` | the exception class that imposed the refusal |
| `agent_framework_effect_unobservable` | `True` — the effect status is a floor, not a measurement |
| `agent_framework_result_present` | whether `context.result` was already set when the refusal reached us |
| `agent_framework_block_note` | the sentence above, in the record itself |

## Ordering is yours to get right

Middleware earlier in the list wraps middleware later in it. A guard placed **before** the
capsule middleware denies a call the capsule middleware never sees — and therefore never
records. Put the capsule middleware first if you want its refusals on the record:

```python
middleware=[*capsule_middleware(operator="acme-co", developer="a@v1"), my_policy_gate]
```

## Model attribution

`AgentContext` carries the agent, and `RawAgent` stores its client at `self.client`
(`_agents.py:885`) with the model id read from `client.model` (`_agents.py:902`); the
provider name is the client's own OTel identifier, `BaseChatClient.OTEL_PROVIDER_NAME`
(`_clients.py:271`).

`FunctionInvocationContext` carries **no** agent. So the run middleware publishes what it
captured into a `contextvars.ContextVar` for the duration of `call_next()`. Tool calls run
inside the run's task tree and inherit that context, which means a process running two
agents on different models concurrently still attributes each tool call correctly —
proven in the suite, not assumed.

Without the run middleware installed, tool capsules fall back to the `model=` you passed
at construction, or to none. Never to a guess.

## Privacy

Inputs and outputs are digested, never stored. The ledger carries
`agent_input_digest` / `agent_output_digest` inside the compute attestation and nothing
else of the payload. Framework objects are projected before digesting: `Content` and
`Message` through their own `to_dict()`, pydantic argument models through
`model_dump(mode="json")`, and raw `bytes` replaced by `"<omitted:N bytes>"` so a
binary-returning tool cannot silently produce no capsule at all.

One shape is worth knowing: after `await call_next()`, `context.result` at the function
seam is **a list of `Content`**, not the tool's bare return value — a `def add(a, b) -> int`
returning `5` arrives as `[Content(type="text", text="5")]`. That is what gets digested.

## Relationship to the framework's OpenTelemetry support

Agent Framework is natively instrumented with OpenTelemetry, and its observability guide
covers OTLP exporters and vendors that consume them. This adapter is **not** an OTLP
exporter and does not replace one. Traces answer *what happened, for debugging*; capsules
answer *what can be proven afterwards, to someone who was not there* — a signed,
hash-chained, independently verifiable record with no trust in the emitting process.
Run both: they do not overlap and they do not interfere.

## Related

- [`docs/adapters/README.md`](./README.md) — the adapter family and what they share
- [`docs/anatomy.md`](../anatomy.md) — what is inside a capsule
- [`docs/chaining.md`](../chaining.md) — how `confirms` chains are read
- [`examples/msft-agent-framework/demo.py`](../../examples/msft-agent-framework/demo.py) —
  four runs, hermetic, offline verification and an evidence render
