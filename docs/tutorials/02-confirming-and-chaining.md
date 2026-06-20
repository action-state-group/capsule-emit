# Confirming & chaining

**Goal:** link "the agent tried to do X" to "X actually happened" — and see why that
distinction matters.

## The problem this solves

When your agent says it charged a card, did the charge *go through*, or did the
agent just *attempt* it? A log that says "charged card" can't tell you. Capsules
keep these separate on purpose:

- **dispatched** = the agent attempted the effect.
- **confirmed** = you observed the effect actually happened.

A *confirmed* capsule **points back** at the *dispatched* one — that link is a
**chain**. Now you have a trail: *attempted → confirmed*, both sealed.

## Do it

```python
from capsule_emit import emit

# 1) the agent attempts the action
attempt = emit(
    action="write_po", operator="acme-co", developer="po-agent@v1",
    effect={"type": "write_po", "status": "dispatched"},   # attempted
)

# 2) later, your system confirms it really landed
done = emit(
    action="write_po", operator="acme-co", developer="po-agent@v1",
    verdict="confirmed",
    effect={"type": "write_po", "status": "confirmed"},    # observed
    confirms=attempt.capsule_id,                           # ← the chain link
)

print("attempt:", attempt.capsule_id[:12])
print("confirm:", done.capsule_id[:12], "→ confirms", attempt.capsule_id[:12])
```

```console
$ python confirm.py
attempt: 1008e6fcf94a → ...
confirm: 7430c9d886eb → confirms 1008e6fcf94a
```

The second capsule carries a `chain` block pointing at the first:

```jsonc
"chain": { "parent_capsule_id": "1008e6fc…", "relation": "confirms" }
```

and its effect gains a `response_digest` — proof of *what* you observed, not just
that you observed something.

## Why this is also how human approval works

The same chain models a human in the loop:

- agent proposes → capsule (`status: "deferred"`)
- human approves → capsule that `confirms=` the proposal
- agent executes → capsule that `confirms=` the approval

Each step is sealed and linked, so "a human approved this" is part of the
verifiable record — not a line in a log you'd have to trust. This is also the
backbone of **selective disclosure**: because each link commits by hash, you can
show someone the *approval chain* without revealing the underlying business data.

## You just

Turned a single action into a verifiable *trail* — attempted vs. confirmed — and saw
how the same mechanism records human approvals.

**Next:** [Reading your ledger →](03-reading-your-ledger.md)
