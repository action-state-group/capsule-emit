# Microsoft Agent Framework adapter — `capsule_middleware`

Your Agent Framework middleware and OpenTelemetry spans tell *you* what your
agent did. A capsule turns each agent run and each tool call into a record built
for **someone who doesn't already trust you** — your customer, their CISO, an
auditor, the other side of a deal — including the calls that raised and the
calls another middleware refused.

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

1. **The commitment and its outcome are both in the record — for the run and
   for each tool call — including the calls that raised and the calls another
   middleware refused.** Two middleware objects ride the framework's own
   `Agent(..., middleware=[...])` surface: `CapsuleRunMiddleware` on the agent
   seam, `CapsuleFunctionMiddleware` on the function seam. Each seals a
   `planned` record before `call_next()` and chains the outcome after it: a
   clean return seals `confirmed`; a raise seals `failed` and is re-raised
   unchanged (the outcome observed at the middleware boundary, not the state of
   the world); a `MiddlewareTermination` or `MiddlewareFailure` raised by a
   middleware *downstream* seals `blocked` with the effect left `planned` and
   `agent_framework_effect_unobservable: true` — this seam cannot tell whether
   the refusal landed before or after the body ran, so it under-claims. The
   tool call's records sit inside the run's, and each tool record carries the
   model the run middleware saw.
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
   recomputes every digest and outcome link and checks every producer signature it
   finds — no account, no service, no network. It runs the format's reference
   payload verifier plus the producer-envelope check; a record carrying no
   signature at all is not failed by default — `capsule-emit verify` counts such
   records in a one-line summary after the tally, and, from 0.8.3, `--require-signature`
   fails them (`INVALID`, exit 1) for ledgers whose producer always signs ([#185](https://github.com/action-state-group/capsule-emit/issues/185)), and the one check outside it
   is the ledger's checkpoint against the transparency service — see
   [Network behavior](#network-behavior).

## The 10-minute proof

The adapter ships a runnable demo — no LLM key, no live service, witness off, anchor pointed at a local stub, four real `Agent` runs over a
scripted `BaseChatClient` composed with the framework's own
`FunctionInvocationLayer` and `ChatMiddlewareLayer`, so the run loop, the
function-calling loop and both middleware pipelines are the real thing — every
record verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[msft-agent-framework]"
python examples/msft-agent-framework/demo.py
```

You'll watch it seal a run pair plus two tool pairs, each `planned → confirmed`
and chained to its own commitment, for `get_price` and `get_stock` in one turn;
`planned → failed` for the `submit_order` that raises; `planned → blocked` for
the order a second middleware denied with `MiddlewareFailure` (the refusal on
record, marked as somebody else's); and `planned → blocked` with the effect
marked unobservable for the same deny raised as `MiddlewareTermination`.
Eighteen records, every one `VALID` under the offline composed check (digest recompute and producer signature), then a fail-closed
`capsule-emit evidence` render. The demo seals to a throwaway ledger and checks
it for you. In your own agent the wiring is one keyword —
`Agent(..., middleware=capsule_middleware(operator=..., developer=...))` — with
the two seams, ordering and options under [Reference](#reference) below.

## Network behavior

By default the middleware runs an async **checkpoint/witness** stream: after
every 100 records, or at the next seal once 900 seconds have passed (there is no
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
things either way; what you turn off is the outside reference. The `operator`
and `developer` you pass to
`capsule_middleware` seal into the hash-chained record permanently — use a
role/version tag, not personal data. Inputs and outputs are sealed as digests:
data minimization, not confidentiality — a digest of a low-entropy value can be
recovered by enumeration.

## What it does *not* do (so you can trust the part it does)

- **Integrity, not completeness.** `verify` establishes that the records you have are internally
  consistent and, where a signature is present, signed by the key it names. It does **not** prove the tools actually
  ran, or that every call was recorded — a record nobody wrote leaves no trace.
  The middleware only seals what passes through its position in the pipeline:
  a guard placed *before* it denies a call it never sees, and a raw HTTP
  request an agent makes on the side is never sealed — see
  [Ordering is yours to get right](#ordering-is-yours-to-get-right). Closing
  that is a separate consistency check against an independent log.
- **A refusal's effect is a floor, not a measurement.** A downstream
  `MiddlewareTermination` can arrive before or after the tool body ran, and
  this seam cannot tell which — so the record says `planned`, never
  `dispatched` — see [What this seam honestly cannot see](#what-this-seam-honestly-cannot-see).
- **It records; it never changes the call.** The seam is in path — a middleware
  here *could* substitute `context.result`, edit the tool list or raise — and
  this one never does; every capsule says so (`observation_mode="in_path_wrapper"`)
  — see [Observation only](#observation-only). Deny belongs to your gate layer.
- **Tamper-evidence, not tamper-proof.** The digest catches an altered field;
  the signature catches an altered record only once you know which key to
  expect. Neither, by itself, stops the holder from re-sealing the entire chain
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
- It is **not** observability, tracing, or a dashboard, and it carries no score,
  ranking, or reputation — it's the record and the math over it. (If you found
  this via an "integrations" or "observability" listing: this sits *next to*
  the framework's OpenTelemetry traces as the evidence layer, it doesn't replace
  them — see [Relationship to the framework's OpenTelemetry support](#relationship-to-the-frameworks-opentelemetry-support).)

---

## Reference

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
verified against the released **1.16.0** wheel, not the GitHub tree. The 10-minute proof above was
re-run on **1.18.0** with the released `capsule-emit` 0.8.1 wheel.

## Register

```python
from agent_framework import Agent
from capsule_emit.adapters.msft_agent_framework import capsule_middleware

# agent = Agent(
#     chat_client,                       # your provider client
#     "you are a procurement assistant",
#     tools=[get_price, submit_order],
#     middleware=capsule_middleware(operator="acme-co", developer="my-agent@v1"),
# )
middleware = capsule_middleware(operator="acme-co", developer="my-agent@v1")
```

`capsule_middleware()` returns two objects over one shared core:
`CapsuleRunMiddleware` (agent seam) and `CapsuleFunctionMiddleware` (function seam).
The framework's own `categorize_middleware` routes each to its own pipeline by
`isinstance`, so a single list is all you pass.

Per-run instead of per-agent works the same way:

```python
middleware = capsule_middleware(operator="acme-co", developer="a@v1")
# response = await agent.run(prompt, middleware=middleware)
```

## The moments

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
def my_policy_gate(context, call_next):   # your own gate, class- or function-style
    return call_next()

middleware = [*capsule_middleware(operator="acme-co", developer="a@v1"), my_policy_gate]
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
