# LangChain listener quickstart

One config line, every tool call seals a capsule:

```python
from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

listener = LangChainCapsuleListener(operator="acme-co", developer="my-agent@v1")
agent.invoke(..., config={"callbacks": [listener]})
```

Run the end-to-end demo (no LLM key, no live services — hermetic stub anchor):

```
pip install "capsule-emit[langchain]"
python examples/langchain-listener/demo.py
```

What it shows: a real langchain-core pipeline (root chain + two tools, one of
which fails) sealing six capsules — planned → confirmed chains for the
successful tool, planned → failed for the error (errors become evidence, not
silence) — then offline `verify()` over every record and a fail-closed
`capsule-emit evidence` render of the run.

Each capsule verifies offline — content digests and chain links over what the listener recorded. `verify()` checks structure and consistency: it proves the record's integrity, not that the tools executed. Tamper-evidence to a third party comes from the anchoring/receipt path (a sealed digest registered with a transparency service); none of it replaces review.

## LangGraph

The same listener, unmodified, seals tool calls a compiled LangGraph
`StateGraph` routes through `ToolNode` — no LangGraph-specific code in the
adapter. See [`langgraph_demo.py`](langgraph_demo.py):

```
pip install "capsule-emit[langchain]" "langgraph>=1.2,<2" langgraph-prebuilt
python examples/langchain-listener/langgraph_demo.py
```

What it shows: two tool calls in one AI turn (parallel `ToolNode` execution)
sealing two independently-paired planned/confirmed chains, plus a raising
tool sealing planned/failed — six capsules total, all `VALID` under the
offline composed check. See the "LangGraph" section of
[`docs/adapters/langchain.md`](../../docs/adapters/langchain.md) for the
call-chain proof and what this does *not* cover (checkpoint replay,
`interrupt()`).
