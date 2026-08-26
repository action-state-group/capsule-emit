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
