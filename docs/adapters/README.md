# Adapters — sealing capsules per framework

An adapter does one thing: **emit one capsule per tool call**, so you don't
write `emit(...)` by hand at every call site.  Same capsule, same anchor,
same verify — adapters differ only in *where they hook in*, because each
framework gives you a different seam.

| Your stack | Adapter | Class | Where it hooks |
|---|---|---|---|
| MCP / any callable | [mcp.md](mcp.md) | `MCPCapsuleEmitter` | decorator on tool function |
| LangChain / LangGraph | [langchain.md](langchain.md) | `LangChainCapsuleEmitter` | callback handler on run |
| CrewAI | [crewai.md](crewai.md) | `CrewAICapsuleEmitter` | wraps tool object |
| Hermes / custom loop | [hermes.md](hermes.md) | `HermesCapsuleEmitter` | call at tool boundary |
| Google ADK | [adk.md](adk.md) | `ADKCapsuleEmitter` | tool callback / event tap |
| Dapr Agents | [dapr_agents.md](dapr_agents.md) | `DaprAgentsCapsuleEmitter` | tool decorator + HITL gate |

**Don't see your framework?**  All adapters extend one ~30-line base
(`CapsuleEmitterBase`) — the [Hermes page](hermes.md) shows the "any loop"
pattern: one `after_tool(...)` call wherever your code runs a tool.  Or call
the top-level `emit()` directly (see the [README quickstart](../../README.md)).

Every page covers three things: **where to put the call** (and why there may
be more than one place), **add it yourself** (the literal lines), and **tell
your coding agent** (a prompt you paste into Claude Code to do the wiring).

All adapters share the same constructor:

```python
Emitter(
    operator="acme-co",        # accountable tenant
    developer="my-agent@v1",   # agent identity + version
    anchor=True,               # anchor each capsule (default)
    ledger="ledger.jsonl",     # local append-only trail
)
```
