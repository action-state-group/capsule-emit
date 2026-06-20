# Adapters — sealing capsules from your framework

An adapter does one thing: **emit one capsule per tool call**, so you don't write
`emit(...)` by hand at every call site. Same capsule, same anchor, same verify —
they differ only in *where you hook in*, because each framework gives you a
different seam.

| Your stack | Adapter | Class | Where it hooks |
|---|---|---|---|
| MCP / any callable | [mcp.md](mcp.md) | `MCPCapsuleEmitter` | a decorator on the tool function |
| LangChain / LangGraph | [langchain.md](langchain.md) | `LangChainCapsuleEmitter` | a callback handler on the run |
| CrewAI | [crewai.md](crewai.md) | `CrewAICapsuleEmitter` | wraps the tool object |
| Hermes / custom loop | [hermes.md](hermes.md) | `HermesCapsuleEmitter` | a call at your tool boundary |

**Don't see your framework?** They all extend one ~30-line base
(`CapsuleEmitterBase`) — the [Hermes page](hermes.md) is the "any loop" pattern:
one `after_tool(...)` call wherever your code runs a tool. Or just call top-level
`emit()` directly (the [README quickstart](../../README.md)).

Every page has three parts: **where to put the call** (and why there could be more
than one place), **add it yourself** (the literal lines), and **or tell your coding
agent** (a prompt you can paste into Claude Code to do the wiring for you).

All adapters share the same constructor:

```python
Emitter(operator="acme-co",        # the accountable tenant
        developer="my-agent@v1",   # agent identity + version
        anchor=True,               # anchor each capsule (default)
        ledger="ledger.jsonl")     # local append-only trail
```
