# Your first capsule

**Goal:** seal one action, anchor it, see it land in your **ledger**, and verify it — the full day-1 loop. ~5 minutes.

## 1. Install

```console
$ pip install capsule-emit
```

## 2. Seal an action

Say your agent just wrote a purchase order. Add one call right after it happens.
Paste this into a file `first.py`:

```python
from capsule_emit import emit

# ... your agent just did this ...
result = {"po_id": "PO-7781"}

cap = emit(
    action="write_order",                # what the agent did
    operator="acme-co",               # the company on the hook for it
    developer="po-agent@v1",          # which agent + version did it
    agent_input={"vendor": "Frobozz Supply", "total": 1240.19},  # what went in
    agent_output=result,              # what came out
    effect={"type": "write_order", "status": "dispatched"},         # the real-world effect
)

print("sealed:", cap.capsule_id)
print("anchored:", cap.anchored)
```

```console
$ python first.py
sealed: 96d457260535f3dc9997bf8df474459cb97ed82cf862800def11d5c2bae689ba
anchored: True
```

That's it. You sealed an action.

- **`sealed:`** is the `capsule_id` — a fingerprint of the whole capsule. Change any
  byte later and this fingerprint won't match. That mismatch *is* the tamper-evidence.
- **`anchored: True`** means an anchor was **submitted** to the free public log — it's
  async/fire-and-forget, so `True` means "submission fired"; to confirm it actually
  landed, you check the capsule's digest against the log. (Offline? Pass `anchor=False`
  and you'll see `anchored: False` — everything else still works.)

## 3. See your anchored ledger

Here's the part that matters: that `emit()` didn't just make one record — it
**appended to your ledger**. Every `emit()` adds a line to `ledger.jsonl`, building
the running, anchored trail of everything your agent does. Look at it:

```console
$ capsule-emit ledger view ./ledger.jsonl

capsule-emit ledger: ledger.jsonl  (1 record(s))

capsule_id      action                  operator        effect/status           verdict       chain
---------------------------------------------------------------------------------------------------
96d457260535f3  write_order                acme-co         write_order:dispatched     executed
```

Run `first.py` again and you'll see **2 records**, then 3 — the ledger grows by one
per action. **This trail, anchored, is the product:** a verifiable record of what
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
  "capsule_id": "96d45726…",
  "action_id":  "write_order/39530d9c…",   // the action + a unique id
  "operator":   "acme-co",
  "developer":  "po-agent@v1",
  "model_attestation": {
    "compute_attestation": {
      "agent_input_digest":  "3c2c9123…",   // your input, hashed (the raw text never left your machine)
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
anchor (not just your own file) is what makes that true for an outsider:
[why anchoring makes it trustworthy](../why-anchoring.md).

## You just

Sealed a real action, **anchored it for free, watched it land in your ledger**, and
proved anyone can verify it — in one call.

**Don't want to wire this by hand?** If you use MCP, LangChain, or CrewAI, an
adapter does it for you — see [docs/adapters](../adapters/). Or paste this to your
coding agent:

> Add `capsule-emit` to this project: `pip install capsule-emit`, and after each
> action that writes data or has a real-world effect, call `emit(action=..., operator="acme-co",
> developer="<this-agent>@v1", agent_input=..., agent_output=..., effect={...})`. Show me the diff.

**Next:** [Confirming & chaining →](02-confirming-and-chaining.md)
