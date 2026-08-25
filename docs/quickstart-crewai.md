# Quickstart: CrewAI + capsule-emit

**Two env vars. One wrap call. Every consequential tool call on the record.**

## Install

```bash
pip install "capsule-emit" "crewai>=0.40"
export AAC_ANCHOR_URL=https://anchor.agentactioncapsule.org/v1/digest
export AAC_LEDGER=crew-capsules.jsonl   # optional; default: ledger.jsonl
```

## Add it to your crew

```python
from capsule_emit.adapters.crewai import CrewAICapsuleEmitter

emitter = CrewAICapsuleEmitter(
    operator="your-org",
    developer="your-agent@v1",
)

# Wrap a plain callable tool — use the return value
safe_tool = emitter.wrap(my_tool_function)

# Or wrap a BaseTool subclass — patches in place
emitter.wrap(my_base_tool_instance)

agent = Agent(role="...", tools=[safe_tool, my_base_tool_instance])
```

Each tool call emits one capsule: input + output digest-committed, anchored on
the live transparency log. Open your permalink:

```
https://anchor.agentactioncapsule.org/v1/inclusion/<capsule_id>
```

## See it live

Leaf indexes 213–214 in the public log are real capsules from this quickstart's
demo run (2026-07-28):

- `abf7cdbc95d749a704bf4637ffa900e2768f48415ba894a65f24fde79035f22d` (leaf 214)
- `55aa6c7c8d45b9747da25cccc53f54fa696058f20b5255af2e1e54611919558c` (leaf 213)

Full guide: [docs/adapters/crewai.md](adapters/crewai.md)
