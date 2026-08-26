# Quickstart: CrewAI + capsule-emit

**One wrap call. Every consequential tool call on the record — witnessed by default.**

## Install

```bash
pip install "capsule-emit" "crewai>=0.40"
```

No anchor/witness setup required. As of 0.5.0, every `seal()`-family call
folds into a checkpoint/witness stream by default — no opt-in, no env vars.
See [`docs/checkpoint.md`](checkpoint.md).

## Add it to your crew

```python
from capsule_emit.adapters.crewai import CrewAICapsuleEmitter

emitter = CrewAICapsuleEmitter(
    operator="your-org",
    developer="your-agent@v1",
    ledger="crew-capsules.jsonl",   # optional; default: ledger.jsonl
)

# Wrap a plain callable tool — use the return value
safe_tool = emitter.wrap(my_tool_function)

# Or wrap a BaseTool subclass — patches in place
emitter.wrap(my_base_tool_instance)

agent = Agent(role="...", tools=[safe_tool, my_base_tool_instance])
```

Each tool call emits one capsule: input + output digest-committed, and — by
default, with no config — folded into a witnessed checkpoint stream. Check
where your ledger stands:

```bash
capsule-emit status crew-capsules.jsonl
```

## Legacy per-capsule anchor (off by default)

Before 0.5.0, sealing also posted each capsule's digest individually to a
per-capsule anchor with its own inspectable permalink. That channel still
exists, but as of 0.5.0 it's an explicit, non-default opt-in:

```bash
export CAPSULE_ANCHOR=legacy-on
export AAC_ANCHOR_URL=https://anchor.agentactioncapsule.org/v1/digest
```

```python
emitter = CrewAICapsuleEmitter(
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

Leaf indexes 213–214 in the public log are real capsules from this
quickstart's demo run (2026-07-28, via the legacy per-capsule anchor channel
that was the default at the time):

- `abf7cdbc95d749a704bf4637ffa900e2768f48415ba894a65f24fde79035f22d` (leaf 214)
- `55aa6c7c8d45b9747da25cccc53f54fa696058f20b5255af2e1e54611919558c` (leaf 213)

Full guide: [docs/adapters/crewai.md](adapters/crewai.md)
