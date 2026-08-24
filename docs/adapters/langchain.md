# LangChain

Two integration shapes ship in `capsule-emit[langchain]`:

## `LangChainCapsuleListener` — recommended

A `BaseCallbackHandler` registered once via `config={"callbacks": [listener]}`;
every tool call seals evidence with a planned → outcome chain:

```python
from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

listener = LangChainCapsuleListener(operator="acme-co", developer="my-agent@v1")
agent.invoke(..., config={"callbacks": [listener]})
```

| LangChain callback | Capsule |
|---|---|
| `on_tool_start` | `effect.status="planned"` — the commitment record |
| `on_tool_end` | `effect.status="confirmed"`, `confirms`-chained to the planned capsule |
| `on_tool_error` | `verdict_class="errored"`, `effect.status="failed"`, chained — errors are evidence |
| root `on_chain_start/end/error` | fyi lifecycle capsules (root runs only; `include_lifecycle=False` to disable) |
| `on_llm_start` / `on_chat_model_start` | model auto-capture threaded into tool capsules; LLM call capsules off by default (`include_llm=True`) |

Pairing is `run_id`-exact (LangChain supplies it), so concurrent calls to the
same tool chain correctly. Handlers never raise into the host application;
raw floats in tool payloads fail closed at the digest layer (warning, no
capsule, run unaffected). Each capsule verifies offline — content digests and chain links over what the listener recorded. `verify()` checks structure and consistency: it proves the record's integrity, not that the tools executed. Tamper-evidence to a third party comes from the anchoring/receipt path (a sealed digest registered with a transparency service); none of it replaces review.

Quickstart: `python examples/langchain-listener/demo.py` (hermetic — no LLM
key, no live services; ends with offline `verify()` + a `capsule-emit
evidence` render).

## `LangChainCapsuleEmitter` — surgical

The original single-capsule-per-tool-call handler
(`capsule_emit.adapters.langchain.LangChainCapsuleEmitter`): one confirmed
capsule per completed tool call, no planned/outcome chain, errors not sealed.
Use when you want minimal ledger volume for a narrow surface; the listener is
the recommended default.
