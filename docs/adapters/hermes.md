# Hermes adapter (and the "any custom loop" pattern)

`HermesCapsuleEmitter` is the most explicit adapter: there's no decorator and no
callback — you call `after_tool(...)` yourself at the point a tool finishes. That
makes it the **general pattern for any custom agent loop** that doesn't have a
decorator seam or a callback bus.

```python
from capsule_emit.adapters.hermes import HermesCapsuleEmitter

emitter = HermesCapsuleEmitter(operator="acme-co", developer="hermes-agent@v1")
```

## Where to put the call

You insert `after_tool(...)` at your **tool-execution boundary** — the line where
your loop has just run a tool and has both the inputs and the result in hand. There
are two natural places:

### Option A — in the central dispatcher (every tool)

If your loop runs tools through one function, put it there once and *every* tool
call seals:

```python
def run_tool(name, inputs):
    result = execute_tool(name, inputs)
    emitter.after_tool(name, inputs, result)   # seals every tool
    return result
```

### Option B — around a single consequential call (targeted)

If you only want to seal the actions that matter, call it at that one site:

```python
result = charge_card(amount=40_00)
emitter.after_tool("charge_card", {"amount": 40_00}, result,
                   effect_status="confirmed")   # this one effect, on the record
```

**The difference:** Option A (in the dispatcher) is "seal everything, one place";
Option B (at the call site) is "seal exactly the actions that count." Hermes gives
you `verdict=` and `effect_status=` per call, so it's also where you record a
**refusal** (`verdict="blocked"`) or a **confirmed** effect.

## Add it yourself

```python
from capsule_emit.adapters.hermes import HermesCapsuleEmitter      # 1
emitter = HermesCapsuleEmitter(operator="acme-co", developer="hermes-agent@v1")  # 2

result = execute_tool(name, inputs)
emitter.after_tool(name, inputs, result)                           # 3  (one line at the boundary)
```

## Or tell your coding agent

> Add `capsule-emit` to our custom agent loop. `pip install capsule-emit`, create one
> `HermesCapsuleEmitter(operator="<our-org>", developer="<this-agent>@<version>")`,
> and call `emitter.after_tool(name, inputs, result)` at our tool-execution boundary
> so each consequential tool run is sealed. For a blocked/denied action pass
> `verdict="blocked"`; for an observed effect pass `effect_status="confirmed"`.
> Don't change tool behavior. Show me the diff first.

## Notes

- This is the adapter to copy if your framework isn't covered — one call at the
  boundary is all any adapter ultimately does.
- Input/output are digest-committed automatically; `model=` is explicit (pass it to
  `emit_capsule`/`emit` if you need it sealed).
- `effect_status="confirmed"` plus `confirms=<prior_capsule_id>` (via the base
  `emit_capsule`) is how you chain *dispatched → confirmed* — see [anatomy](../anatomy.md).
