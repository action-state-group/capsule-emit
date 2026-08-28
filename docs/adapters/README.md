# Adapters — sealing capsules per framework

An adapter does one thing: **seal one capsule per tool call**, so you don't
write `seal(...)` by hand at every call site.  Same capsule, same witness,
same verify — adapters differ only in *where they hook in*, because each
framework gives you a different seam.

**Since 0.5.0:** the top-level producer verb is `seal()` (the former `emit()` was
renamed to `seal()` / `received()`). Adapters are thin shells over one shared base
(`CapsuleEmitterBase`) that call the same primitive internally; their class names and
methods are unchanged.

| Your stack | Adapter | Class | Where it hooks |
|---|---|---|---|
| MCP / any callable | [mcp.md](mcp.md) | `MCPCapsuleEmitter` | decorator on tool function |
| LangChain / LangGraph | [langchain.md](langchain.md) | `LangChainCapsuleEmitter` | callback handler on run |
| CrewAI | [crewai.md](crewai.md) | `CrewAICapsuleEmitter` | wraps tool object |
| Hermes / custom loop | [hermes.md](hermes.md) | `HermesCapsuleEmitter` | call at tool boundary |
| Google ADK | [adk.md](adk.md) | `ADKCapsuleEmitter` | tool callback / event tap |
| Dapr Agents | [dapr_agents.md](dapr_agents.md) | `DaprAgentsCapsuleEmitter` | tool decorator + HITL gate |

**Quickstarts** (copy-paste, framework-specific, five minutes): **[CrewAI](../quickstart-crewai.md)** · **[LangGraph](../quickstart-langgraph.md)**.

**Don't see your framework?**  All adapters extend one ~30-line base
(`CapsuleEmitterBase`) — the [Hermes page](hermes.md) shows the "any loop"
pattern: one `after_tool(...)` call wherever your code runs a tool, and it's
the adapter to copy wholesale if nothing else fits. Or call the top-level
`seal()` directly at your own call site (see the [README quickstart](../../README.md)).

Every page covers three things: **where to put the call** (and why there may
be more than one place), **add it yourself** (the literal lines), and **tell
your coding agent** (a prompt you paste into Claude Code to do the wiring).

All adapters share the same constructor:

```python
Emitter(
    operator="acme-co",        # accountable tenant
    developer="my-agent@v1",   # agent identity + version
    anchor=False,              # legacy per-capsule anchor — off by default since 0.5.0
    ledger="ledger.jsonl",     # local append-only trail
)
```

Sealing is witnessed by default (no config needed): the checkpoint/witness push
goes to `witness.agentactioncapsule.org` (`POST /checkpoints`) — see
[`docs/checkpoint.md`](../checkpoint.md). The legacy `anchor` channel is off by
default as of 0.5.0 — `anchor=True` (or `CAPSULE_ANCHOR=legacy-on`) opts back into
the older, non-default per-capsule anchor channel.
