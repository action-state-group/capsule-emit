# CrewAI adapter

`CrewAICapsuleEmitter` wraps a **tool object**. You hand it your tool, it hands back
a sealing version that emits one capsule per call (input + output captured
automatically). It works whether your tool is a plain function or a CrewAI
`BaseTool` subclass — and `wrap()` itself has no hard dependency on `crewai`.

```python
from capsule_emit.adapters.crewai import CrewAICapsuleEmitter

emitter = CrewAICapsuleEmitter(operator="acme-co", developer="ops-agent@v1")
```

## Where to put the call

Wrap the tool **before you give it to the crew** — i.e. at the point where you
build the tool list for an Agent/Crew. Two shapes, picked automatically by `wrap()`:

### Option A — a plain callable tool

```python
safe_tool = emitter.wrap(my_tool)          # returns a sealing wrapper
agent = Agent(role="...", tools=[safe_tool])
```

### Option B — a CrewAI `BaseTool` subclass

Same call — `wrap()` detects the class and patches its `._run`, so the *same tool
object* now seals and you can keep passing it as-is:

```python
emitter.wrap(my_base_tool)                 # patches ._run in place
agent = Agent(role="...", tools=[my_base_tool])
```

**The difference:** for a function, `wrap()` returns a **new** wrapped callable (use
the return value); for a `BaseTool`, it patches **in place** (the object you passed
is now sealing). Pass each tool through `wrap()` once. Don't double-wrap.

## Add it yourself

```python
from capsule_emit.adapters.crewai import CrewAICapsuleEmitter      # 1
emitter = CrewAICapsuleEmitter(operator="acme-co", developer="ops-agent@v1")  # 2

tools = [emitter.wrap(t) for t in (write_po, send_invoice)]        # 3  (wrap the consequential ones)
agent = Agent(role="AP clerk", tools=tools)
```

Wrap the tools that **act** (writes, sends, payments); leave pure-read tools
unwrapped if you don't need them on the record.

## Or tell your coding agent

> Add `capsule-emit` to our CrewAI setup. `pip install capsule-emit`, create one
> `CrewAICapsuleEmitter(operator="<our-org>", developer="<this-agent>@<version>")`,
> and pass every **state-changing** tool through `emitter.wrap(...)` before it's
> added to an Agent (use the return value for function tools; `wrap()` patches
> BaseTool subclasses in place). Don't wrap read-only tools, don't change tool
> behavior. Show me the diff first.

## Notes

- Input/output are digest-committed automatically.
- `model=` isn't auto-captured (the wrapper sees the tool, not the LLM) — pass it
  explicitly if you need it sealed.
- Idempotency: wrapping the same `BaseTool` twice would seal twice per call — wrap
  once.
