# Quickstart: LangGraph + capsule-emit

**One callback. Every tool call in your graph on the record — witnessed by default.**

## Install

```bash
pip install "capsule-emit" langchain-core langgraph
```

No anchor/witness setup required. As of 0.5.0, every `seal()`-family call
(the LangChain callback handler included) folds into a checkpoint/witness
stream by default — no opt-in, no env vars. See [`docs/checkpoint.md`](checkpoint.md).

## Add it to your graph

```python
from capsule_emit.adapters.langchain import LangChainCapsuleEmitter
from langchain_core.runnables import RunnableConfig

emitter = LangChainCapsuleEmitter(
    operator="your-org",
    developer="your-agent@v1",
    ledger="graph-capsules.jsonl",   # optional; default: ledger.jsonl
)

# Inside a node, pass the emitter as a callback on tool.invoke()
def my_node(state):
    result = my_tool.invoke(
        {"arg": state["value"]},
        config=RunnableConfig(callbacks=[emitter]),
    )
    return {"results": [result]}
```

`on_tool_start` / `on_tool_end` fire per tool invocation. Input + output are
digest-committed, and — by default, with no config — folded into a witnessed
checkpoint stream. Check where your ledger stands:

```bash
capsule-emit status graph-capsules.jsonl
```

## LangGraph note

No dedicated LangGraph adapter needed — the LangChain callback interface is the
shared surface across LangGraph graphs and plain LangChain chains. The `run_id`
kwarg threads input→output correctly for sequential and parallel tool calls.

## Legacy per-capsule anchor (off by default)

Before 0.5.0, sealing also posted each capsule's digest individually to a
per-capsule anchor with its own inspectable permalink. That channel still
exists, but as of 0.5.0 it's an explicit, non-default opt-in:

```bash
export CAPSULE_ANCHOR=legacy-on
export AAC_ANCHOR_URL=https://anchor.agentactioncapsule.org/v1/digest
```

```python
emitter = LangChainCapsuleEmitter(
    operator="your-org",
    developer="your-agent@v1",
    anchor=True,   # legacy per-capsule anchor — off by default since 0.5.0
)
```

With that opt-in, each capsule gets a permalink:

```
https://anchor.agentactioncapsule.org/v1/inclusion/<capsule_id>
```

## See it live

Leaf indexes 215–216 in the public log are real capsules from this
quickstart's demo run (2026-07-28, via the legacy per-capsule anchor channel
that was the default at the time):

- `527b10bfc1f36d2437a7c6b8d9997b6cb571c047e078862c3ac4d0075f218a1d` (leaf 215)
- `eea8fb21d1db8990c9d2b0fa9561726cf9ea6412d7609a717ec99c6dcc80f424` (leaf 216)

Full guide: [docs/adapters/langchain.md](adapters/langchain.md)
