# LangChain adapter

`LangChainCapsuleEmitter` is a **callback handler**. You don't wrap individual
tools — you hand it to LangChain and it fires on `on_tool_start` / `on_tool_end`,
emitting one capsule per completed tool call. It captures the tool input and output
automatically.

```python
from capsule_emit.adapters.langchain import LangChainCapsuleEmitter   # needs: pip install langchain-core

emitter = LangChainCapsuleEmitter(operator="acme-co", developer="research-agent@v1")
```

## Where to put the call

A LangChain callback can be attached at two scopes — same handler, different reach.

### Option A — per invocation (narrow)

Pass it in `config={"callbacks": [...]}` on the specific `.invoke()` you care about.
Only that run is sealed. Use this when you want to seal **one workflow** and leave
the rest of your app untouched.

```python
agent.invoke(payload, config={"callbacks": [emitter]})
```

### Option B — constructor-level (every run)

Attach it where you build the agent/executor so **every** run through that object is
sealed, no matter who calls it. Use this for a service where all tool calls should
be on the record.

```python
agent = AgentExecutor(agent=..., tools=..., callbacks=[emitter])
agent.invoke(payload)   # sealed; so is every other call
```

**The difference:** Option A scopes sealing to *one call* (explicit, opt-in per
run); Option B scopes it to *the agent* (every run, can't be forgotten). Reach
differs; the capsule is identical.

## Add it yourself

```python
from capsule_emit.adapters.langchain import LangChainCapsuleEmitter   # 1
emitter = LangChainCapsuleEmitter(operator="acme-co", developer="research-agent@v1")  # 2

agent.invoke(payload, config={"callbacks": [emitter]})                # 3  (+ existing callbacks, if any)
```

If you already pass callbacks, just add `emitter` to the list — order doesn't
matter.

## Or tell your coding agent

> Add `capsule-emit` to our LangChain agent. `pip install capsule-emit langchain-core`,
> create one `LangChainCapsuleEmitter(operator="<our-org>", developer="<this-agent>@<version>")`,
> and attach it as a callback at the **AgentExecutor constructor** so every run is
> sealed (append to existing `callbacks`, don't replace). Don't change tool or
> prompt logic. Show me the diff first.

## Notes

- Tool input/output are captured from `on_tool_start`/`on_tool_end` and
  digest-committed automatically.
- **Model auto-capture is the one place LangChain can give us the model** (via
  `on_llm_start`). That enhancement is in progress (inbox `[capsule-emit-adoptability]`);
  until it lands, pass `model=` explicitly if you need it sealed.
- `on_tool_error` discards the pending capsule — errored tool calls don't silently
  seal as successes.
