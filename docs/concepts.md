# Concepts, in plain words

Seven words cover everything. Each one is a real field you can see or a command you
can run — no theory required.

### Capsule
A sealed receipt of one thing your agent did. Who did it, what they did, what
happened — bundled and hashed so it can't be quietly changed. It's plain JSON
(`cap.capsule`). **Everything else is about what you can do with capsules.**

### Seal
What `emit()` does: it takes the action, hashes the contents, and stamps an id
(`capsule_id`) that's a fingerprint of the whole thing. Hand the capsule to anyone —
they can re-hash it and check the fingerprint matches.

### may / did
The honesty bit. *Approved* isn't *executed*, and *executed* isn't *confirmed*. A
capsule records the verdict (`disposition.verdict_class`) **and** whether the effect
was just **dispatched** (attempted) or **confirmed** (you saw it land). So nobody can
present "tried to charge the card" as "charged the card."

### Chain
Actions link together. A *confirm* (or a human approval) is its own capsule that
points back at the one before it (`chain.parent_capsule_id`), by content address — so a
*different* agent can chain to yours by id alone. String them up and
*attempted → approved → confirmed* becomes one trail you can follow and verify.
→ `emit(..., confirms=earlier_id)` · [within & across agents](chaining.md)

### Break
The reason any of this is worth trusting: change one byte of a sealed capsule and the
fingerprint stops matching, so `verify` returns **invalid**. The break is the proof.
It's why your capsule means something to someone who doesn't trust you — your own
logs can't do that.

### Anchor
Writing the fingerprint (just the fingerprint — never your data) to a public, append-
only list, so there's outside evidence the capsule existed at a certain time. On by
default, free, no signup. Anyone can later check your capsule against the list.
→ off with `emit(..., anchor=False)`. *Why this is what turns "my word" into proof
anyone can check: [why anchoring makes it trustworthy](why-anchoring.md).*

### Ledger
Your local running file of capsules — the trail of everything sealed so far. View it
as a table or pull it as JSON.
→ `capsule-emit ledger view ledger.jsonl`

---

One more pairing worth knowing:

### Declare → Enforce
You write your action's rules in a `manifest.md` (plain English: "the math must add
up"). `capsule-emit` only **records** them. Later, a compatible gateway reads the
**same file** and **enforces** them — with **no change** to your code. Adopt sealing
now; switch on enforcement when you're ready.

---

Want to *see* these instead of read them? Do the [tutorials](tutorials/) — they're
five-minute, copy-paste sessions. Want the byte-level detail? [anatomy](anatomy.md).
Ready for more — verify it yourself, or make capsules actually *block* a bad action?
[Going deeper — and popping out](going-deeper.md).
