# LangChain adapter — `LangChainCapsuleListener`

Your LangChain callbacks tell *you* what your agent did. A capsule turns each
tool call into a record built for **someone who doesn't already trust you** —
your customer, their CISO, an auditor, the other side of a deal — including the
calls the agent tried to make and didn't complete.

That's the difference between a log and a record. A log is for you. A record is
for the person who has to believe you. Traces answer "what happened?" for the
team that owns the trace; they don't answer "can a stranger confirm this months
later?", because the party that ran the agent also holds and can rewrite the
trace. A capsule is content-addressed and independently checkable, so it can.

## What you get, in three claims

1. **The attempt and its outcome are both in the record — including the ones
   that didn't go through.** Each tool call seals a `planned` record and chains
   the outcome to it. A guard that *raises* to block a call seals a `failed`
   record (*"proposed, did not happen"*); a guard that *returns* a denial seals
   `confirmed` with the denial in the recorded output. Either way the decision is
   in the record, not just your logs.
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
   finds — no account, no service, no network. A record carrying no signature at all is not failed by default — `capsule-emit verify` counts such
   records in a one-line summary after the tally, and, from 0.8.3, `--require-signature`
   fails them (`INVALID`, exit 1) for ledgers whose producer always signs ([#185](https://github.com/action-state-group/capsule-emit/issues/185)); the one check outside it is
   the ledger's checkpoint against the transparency service; the Python API for
   each check is under [Verification](#verification).

## The 10-minute proof

The adapter ships a runnable demo — a hermetic local transparency stub (no
external network), real `langchain-core` tools, a call that succeeds and one
that's refused, every record verified offline:

```shell
git clone https://github.com/action-state-group/capsule-emit
cd capsule-emit
python -m venv .venv && . .venv/bin/activate
pip install "capsule-emit[langchain]"
python examples/langchain-listener/demo.py
```

You'll watch it seal `planned → confirmed` for the call that works and
`planned → failed` for the one that raises, each record `VALID` under the offline composed check (digest recompute and producer signature), and a fail-closed `capsule-emit evidence` render. The demo seals to a
throwaway ledger and checks it for you; the step-by-step version of that code —
and how to keep a ledger of your own — is under [Example](#example) below.

## LangGraph

Does a LangGraph `StateGraph` need a different hook, or does the listener
above already cover it? Tested against the released wheel, not inferred:
**already covered, no new hook needed.** LangGraph routes every `ToolNode`
call through the exact same LangChain callback manager this listener already
attaches to. This section adds `pip install "langgraph>=1.2,<2" langgraph-prebuilt`
on top of the `[langchain]` extra — nothing else is LangGraph-specific.
A runnable version of the proof below ships as
[`examples/langchain-listener/langgraph_demo.py`](https://github.com/action-state-group/capsule-emit/blob/main/examples/langchain-listener/langgraph_demo.py).

### The call chain

Traced at `langgraph-prebuilt==1.1.0` (tag `prebuilt==1.1.0`, resolved SHA
`3614e88c58af63f597764218646e85c49952b2da`), against `langgraph==1.2.12` and
`langchain-core==1.6.4`:

1. The compiled graph (`Pregel`) *is* a `langchain_core.runnables.Runnable` —
   `libs/langgraph/langgraph/pregel/protocol.py:25`.
2. `Pregel.invoke` calls `self.stream(input, config, ...)` — the top-level
   `config` you pass (with `callbacks`) flows into the run loop —
   `libs/langgraph/langgraph/pregel/main.py:3803`.
3. `run_with_retry` calls `task.proc.invoke(task.input, config)` —
   `pregel/_retry.py:585`.
4. Every graph node, `ToolNode` included, extends `RunnableCallable`
   (`_internal/_runnable.py:278`), whose `.invoke()` calls
   `get_callback_manager_for_config(config, self.tags)` then
   `patch_config(config, callbacks=run_manager.get_child())` — stock LangChain
   callback propagation, a chained child callback manager —
   `_runnable.py:400-409`.
5. `ToolNode._func` / `_afunc` (`tool_node.py:793,828`) build a per-call
   `config_list` from that child config.
6. **`response = tool.invoke(call_args, config)`** — `tool_node.py:958`
   (sync); **`response = await tool.ainvoke(call_args, config)`** —
   `tool_node.py:1105` (async).

`tool.invoke(call_args, config)` is exactly the call shape
`LangChainCapsuleListener` already hooks in plain LangChain —
`config["callbacks"]` still carries the chained callback manager, so
`on_tool_start` / `on_tool_end` / `on_tool_error` fire identically whether the
tool was called directly or reached through a compiled graph.

### The wiring

Same as any other LangChain runnable — pass it in `config` on `invoke()`:

```python
result = graph.invoke(
    {"messages": [HumanMessage(content="...")]},
    config={"callbacks": [listener]},
)
```

`compile()` itself takes no `callbacks` argument — there's no compile-time
registration path. If you'd rather not pass `config` on every call, LangChain's
own `Runnable.with_config` binds default callbacks onto the compiled graph
once (tested: `graph.compile().with_config(callbacks=[listener])`, then plain
`.invoke(...)` calls with no `config` kwarg still seal) — that's a LangChain
mechanism, not a LangGraph one.

### The executed proof

`StateGraph(MessagesState)` with a `chatbot` node (a scripted
`GenericFakeChatModel`, no API key) emitting a tool call, wired through
`ToolNode([get_weather])` and `tools_condition`, looping back to `chatbot` for
the final answer:

```
[human] "What's the weather in Austin?"
[ai] tool_calls=[{'name': 'get_weather', 'args': {'city': 'Austin'}, ...}]
[tool] 'It is sunny and 72F in Austin.'
[ai] 'It is sunny and 72F in Austin.'

=== ledger file: 4 records sealed ===
record 1: action_id='chain_started/...'   effect=None (root-run fyi capsule)
record 2: action_id='get_weather/...'     effect={'status': 'planned', ...}
record 3: action_id='get_weather/...'     effect={'status': 'confirmed', ...} parent=<record 2's capsule_id> relation=confirms
record 4: action_id='chain_completed/...' effect=None (root-run fyi capsule)
```

`capsule-emit verify --store ledger.jsonl --require-signature` → `4/4 VALID`.

**Two tool calls in one turn (parallel `ToolNode`).** A turn where the model
calls `get_weather` and `get_population` together: `ToolNode`'s default
executor runs both concurrently, so the two `planned` records land *before*
either `confirmed` record — and each `confirmed` still chains to its own
tool's `planned` capsule, not the other one's, because pairing is by
LangChain's `run_id`, not call order:

```
record 1: action_id='get_population/...' effect={'status': 'planned', ...}
record 2: action_id='get_weather/...'    effect={'status': 'planned', ...}
record 3: action_id='get_population/...' effect={'status': 'confirmed', ...} parent=record 1's capsule_id
record 4: action_id='get_weather/...'    effect={'status': 'confirmed', ...} parent=record 2's capsule_id

pairing check [get_population]: match=True
pairing check [get_weather]:    match=True
```

Both sealed, both correctly paired, `verify_store` all `ok=True`.

**A tool that raises.** `ToolNode`'s default `handle_tool_errors=True` only
intercepts its own `ToolInvocationError` (bad arguments); a plain exception
raised from inside the tool body still propagates all the way up through
`tool.invoke()` — so `on_tool_error` fires and the graph invocation itself
raises, either way:

```
[graph raised] ValueError('no such order: Z-000')

record 1: action_id='flaky_lookup/...' verdict_class=executed effect={'status': 'planned', ...}
record 2: action_id='flaky_lookup/...' verdict_class=errored  effect={'status': 'failed', ...} parent=record 1's capsule_id
```

`verify_store` all `ok=True` on both records — the failure is sealed as
evidence, not lost.

### What it does not see

- **Graph-level node events.** Only `ToolNode`-routed calls seal. The
  `chatbot` node's own execution — the LLM call itself, and any plain
  (non-tool) node's start/end — is not a tool call and is not sealed by this
  listener; `include_lifecycle` (default on) gives you one `fyi` capsule for
  the *whole graph invocation's* root chain start/end/error, not a capsule per
  node.
- **`interrupt()`, tested.** `langgraph.types.interrupt()` raises
  `GraphInterrupt` to pause a run. When called from *inside* a tool body, that
  exception propagates through `tool.invoke()` exactly like a real error:
  `on_tool_error` fires and the listener seals `verdict_class="errored"`,
  `effect.status="failed"` — **a paused-for-approval tool call is
  indistinguishable in the ledger from a genuinely broken one.** Resuming with
  `Command(resume=...)` re-enters the tool function from the top — LangGraph
  checkpoints at the graph-step level, not inside a node — so any code that
  ran *before* the `interrupt()` call runs again for real. Tested with a side
  effect counter placed before the `interrupt()` call: the counter reads 1
  after the first (paused) invoke and 2 after resume, confirming the
  pre-interrupt code re-executed rather than being skipped. The listener seals
  a second, correctly independent `planned → confirmed` pair for that second
  real execution — it does not know or record that the first attempt was
  "the same call, paused," only that two tool calls with two distinct
  `run_id`s happened, one of which errored and one of which succeeded.
- **Checkpointer replay, tested.** Rewinding to a checkpoint taken *before* a
  tool ran (`app.get_state_history()` then `app.invoke(None, config=<earlier
  checkpoint>)`, LangGraph's own "time travel") and letting the graph move
  forward again is a **genuine second execution**, not a replay of a cached
  result — LangGraph has no result cache at the tool-call level. Tested: a
  real call counter read 1 after the original run and 2 after rewind+resume;
  the listener correctly sealed two independent `planned → confirmed` pairs
  (4 capsules total for one tool), each chained to its own planned capsule,
  none re-sealed or duplicated. From the listener's perspective, "resume from
  before the tool ran" and "call the tool again" are the same event, and
  that's what gets recorded — correctly, since that's what actually happened.

## How it works

The listener is a standard LangChain
[`BaseCallbackHandler`](https://docs.langchain.com/oss/python/langchain/callbacks). Attach it to any
`invoke()` and it seals the tool lifecycle:

| LangChain callback | Capsule |
|---|---|
| `on_tool_start` | `effect.status = "planned"` |
| `on_tool_end` | `effect.status = "confirmed"`, chained to the planned capsule |
| `on_tool_error` | `verdict = "errored"`, `effect.status = "failed"`, chained |
| root chain start/end/error | lifecycle capsules (`include_lifecycle`, default on) |

**The two-record chain is the point.** The `planned` capsule is written *before*
the tool runs; the `confirmed` capsule is written after and carries
`chain: {parent_capsule_id: ..., relation: "confirms"}` pointing back at it. A
record of an intent that has no confirmation, or a confirmation with no prior
intent, is visible as such to anyone reading the ledger.

LLM call events are **not** sealed by default (`include_llm=False`) — token
traffic is volume, not evidence. The model identity is still captured onto the
tool capsules.

## Prerequisites

```shell
pip install "capsule-emit[langchain]>=0.7.0"
```

## Example

This example runs with no API key and no network — the evidence path is
exercised by a plain tool invocation, so you can confirm the behavior before
wiring it to a model.

<Steps>
  <Step title="Attach the listener">
    `operator` and `developer` are required and are stamped on every capsule.

    ```python
    import json, os, tempfile

    os.environ.setdefault("CAPSULE_WITNESS", "off")  # see Network behavior

    from langchain_core.tools import tool
    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener
    from capsule_emit.verification import verify_capsule

    ledger_path = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")

    listener = LangChainCapsuleListener(
        operator="acme-corp",             # tenant/org identifier
        developer="support-agent@1.4.0",  # agent name + version
        ledger=ledger_path,
        anchor=False,
    )
    ```
  </Step>
  <Step title="Run a tool with the listener attached">
    Pass the listener in `config={"callbacks": [...]}` on any `invoke()`, or
    register it globally per LangChain's callback docs.

    ```python
    @tool
    def issue_refund(order_id: str, amount_usd: str) -> str:
        """Refund an order. amount_usd is an exact decimal string, not a float."""
        return f"refunded {order_id} ${amount_usd}"

    result = issue_refund.invoke(
        {"order_id": "A-1029", "amount_usd": "42.50"},
        config={"callbacks": [listener]},
    )
    ```

    <Note>
    Float tool arguments chain correctly as of 0.7.0 — they are canonicalized
    to RFC 8785 decimal strings before digesting (#128, fixed by #135). On
    releases before 0.7.0, a float argument silently dropped the planned
    capsule and left the outcome unchained.
    </Note>
  </Step>
  <Step title="Read the ledger">
    ```python
    records = [json.loads(line) for line in open(ledger_path)]

    for record in records:
        chain = record.get("chain")
        parent = chain["parent_capsule_id"][:16] + "..." if chain else "-"
        print(
            f"  {record['effect']['status']:9s} "
            f"capsule_id={record['capsule_id'][:16]}... "
            f"parent={parent} "
            f"ledger_mode={record['assurance']['ledger_mode']}"
        )
    ```

    ```text
    planned   capsule_id=d136b3ca318808f4... parent=- ledger_mode=standalone
    confirmed capsule_id=9ca0668a9120310b... parent=d136b3ca318808f4... ledger_mode=chained
    ```
  </Step>
</Steps>

## Verification

`verify_capsule` recomputes the capsule identifier and every digest from the
record's own content. Passing `store=` lets it resolve the chain link. It does
**not** check the producer signature — that is the second, separate check below.

```python
from capsule_emit.verification import verify_capsule

for record in records:
    verdict = verify_capsule(record, store=records)
    notes = ", ".join(f"{f.severity}:{f.code}" for f in verdict.findings) or "none"
    print(f"  {record['effect']['status']:9s} ok={verdict.ok} findings={notes}")
```

```text
planned   ok=True findings=info:unknown_registry_value
confirmed ok=True findings=info:unknown_registry_value
```

`verify_capsule` returns a `VerificationResult` with `.ok`, `.findings`,
`.errors` and `.assurance`. Findings are graded: the `info` finding above
reports that `effect.type="issue_refund"` is not a value seeded in the spec's
registry — informational, and explicitly not a rejection.

To check a whole ledger, `verify_store(records)` returns a **list** of
`VerificationResult` — one per record. Guard the empty case:

```python
from capsule_emit.verification import verify_store

results = verify_store(records)
assert results and all(r.ok for r in results)
```

### Checking the producer signature

Each record's `signature` field is a hex-encoded COSE_Sign1 envelope over the
capsule id. Authenticate it with the spec package's verifier:

```python
from agent_action_capsule.producer_envelope import verify_producer_envelope

for record in records:
    envelope = bytes.fromhex(record["signature"])
    sig = verify_producer_envelope(record["capsule_id"], envelope)
    print(f"  {record['effect']['status']:9s} signature_ok={sig.ok}")
```

A tampered envelope fails with `envelope_signature_invalid`; an envelope
replayed onto a different capsule fails with `envelope_payload_mismatch`. On
success the result carries the raw Ed25519 public key that signed the record —
whether that key is authorized for the stated operator is caller policy.

The two checks together are what "independently verifiable" means on this page:
`verify_capsule` proves the content is what the identifier commits to and the
chain is consistent; `verify_producer_envelope` proves who sealed it.

## Network behavior

Two channels exist, both off or content-free, and both worth knowing about
before you run this in production:

- **Witnessing** is **on by default**. Once enough ledger entries accumulate, a
  signed checkpoint — log size, root hash, timestamp, *never* capsule content —
  is POSTed to the default witness endpoint. Disable with `CAPSULE_WITNESS=off`.
- **Anchoring** is off unless enabled. When on, it is fire-and-forget by
  default: `EmitResult.anchored` reports that a **submission was made**, not
  that an anchor was confirmed. Set `anchor_wait=<seconds>` to block for a
  genuine outcome; without it, do not read the field as a receipt.

## Limits

- The signing key is generated and held locally. `assurance.attestation_mode`
  reads `self_attested`: the records are tamper-evident and independently
  checkable, but they attest that *this process* said so. They are not a
  third-party attestation of what the model actually did.
- `effect.effect_attestation` reads `runtime_claimed` — the confirmation is the
  framework's report that the tool returned, not proof of the external effect.
- Available for Python only. There is no JavaScript/TypeScript package.

## Resources

- [GitHub](https://github.com/action-state-group/capsule-emit)
- [PyPI](https://pypi.org/project/capsule-emit/)
- [Specification](https://github.com/action-state-group/agent-action-capsule)
- [Issue tracker](https://github.com/action-state-group/capsule-emit/issues)
