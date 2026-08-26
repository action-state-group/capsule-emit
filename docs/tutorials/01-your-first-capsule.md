# Your first capsule

**Goal:** seal one action, see it land in your **witnessed ledger**, and verify it —
the full day-1 loop. ~5 minutes.

## 1. Install

```console
$ pip install capsule-emit
```

## 2. Seal an action

Say your agent just wrote a purchase order. Add one call right after it happens.
Paste this into a file `first.py`:

```python
from capsule_emit import seal

# ... your agent just did this ...
result = {"po_id": "PO-7781"}

cap = seal(
    {"vendor": "Frobozz Supply", "total": "1240.19"},  # what went in — the payload
    action="write_order",                # what the agent did
    operator="acme-co",               # the company on the hook for it
    developer="po-agent@v1",          # which agent + version did it
    agent_output=result,              # what came out
    effect={"type": "write_order", "status": "dispatched"},         # the real-world effect
)

print("sealed:", cap.capsule_id)
print("anchored:", cap.anchored)
```

```console
$ python first.py
sealed: cfed7f490132212ae653a90a3ba472ffa363811af0b963ea14f1d7b7d6fea541
anchored: False
```

That's it. You sealed an action.

- **`sealed:`** is the `capsule_id` — a fingerprint of the whole capsule. Change any
  byte later and this fingerprint won't match. That mismatch *is* the tamper-evidence.
- **`anchored: False`** is expected — the legacy per-capsule anchor channel is an
  explicit opt-in (`seal(..., anchor=True)`), off by default. That's not a gap: by
  default `seal()` instead folds this capsule into your ledger's **witness stream** —
  every ~100 entries (or 15 minutes, whichever comes first) a signed checkpoint over
  the whole ledger is registered with a public log, no opt-in code required. More on
  that in the next section, and in depth in [docs/checkpoint.md](../checkpoint.md).

> Note: `total` above is a **string**, `"1240.19"`, not a bare float. Any non-integer
> number inside a digest-bearing field (`agent_input`/`agent_output`/`effect`) has to
> be a JSON string — bare floats round differently across languages, so they're
> rejected (`FloatInDigestError`) rather than silently hashed inconsistently. Integers
> are fine as-is.

## 3. See your witnessed ledger

Here's the part that matters: that `seal()` didn't just make one record — it
**appended to your ledger**. Every `seal()` adds a line to `ledger.jsonl`, building
the running, witnessed trail of everything your agent does. Look at it:

```console
$ capsule-emit ledger view ./ledger.jsonl

capsule ledger: ./ledger.jsonl  (1 record(s))

  capsule_id      actor                                       verdict       effect                chain    verify
-----------------------------------------------------------------------------------------------------------------
  cfed7f49013221  po-agent@v1                                 executed      write_order:applied               ✓
```

Run `first.py` again and you'll see **2 records**, then 3 — the ledger grows by one
per action. **This trail, witnessed, is the product:** a verifiable record of what
your agent did, that you keep and that anyone can check. (Reading it in depth —
chains and `--json` — is [tutorial 3](03-reading-your-ledger.md).)

## 4. Look inside a capsule

`cap.capsule` is plain JSON. Add this and run again:

```python
import json
print(json.dumps(cap.capsule, indent=2))
```

You'll see the fields below (trimmed). Nothing here is magic — it's an honest record:

```jsonc
{
  "capsule_id": "cfed7f4901322…",
  "action_id":  "write_order/1dce0e9a…",   // the action + a unique id
  "operator":   "acme-co",
  "developer":  "po-agent@v1",
  "model_attestation": {
    "compute_attestation": {
      "agent_input_digest":  "ef1f243d…",   // your input, hashed (the raw text never left your machine)
      "agent_output_digest": "c574d16d…"    // your output, hashed
    }
  },
  "effect":      { "type": "write_order", "status": "dispatched" },
  "disposition": { "verdict_class": "executed", "human_disposed": false }
}
```

Notice your **input and output are hashed, not stored in the clear** — the capsule
proves *what* the values were without putting your vendor names and dollar amounts
into a public log. (More in [anatomy](../anatomy.md).)

## 5. Prove it's real (and catch tampering)

Anyone can verify your whole ledger from the bytes alone — no keys, no account:

```console
$ pip install agent-action-capsule
$ agent-action-capsule verify --store ./ledger.jsonl
```

Edit one character in `ledger.jsonl` and run it again — verification fails. That's
the point: the record is trustworthy to someone who didn't write it. *Why* a public
witness (not just your own file) is what makes that true for an outsider:
[why anchoring makes it trustworthy](../why-anchoring.md).

## You just

Sealed a real action, **watched it land in a witnessed ledger for free**, and
proved anyone can verify it — in one call.

**Don't want to wire this by hand?** If you use MCP, LangChain, or CrewAI, an
adapter does it for you — see [docs/adapters](../adapters/). Or paste this to your
coding agent:

> Add `capsule-emit` to this project: `pip install capsule-emit`, and after each
> action that writes data or has a real-world effect, call `seal({...}, action=...,
> operator="acme-co", developer="<this-agent>@v1", agent_output=..., effect={...})`.
> Show me the diff.

**Next:** [Confirming & chaining →](02-confirming-and-chaining.md)
