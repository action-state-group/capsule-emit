# Chaining — within one agent, and across agents

A single capsule records one action. **Chaining** links capsules into a verifiable
*trail* within one stream. This is where the may/did distinction becomes a sequence.
Cross-organizational chaining guidance is under revision — see below.

## How the link works

Every capsule is identified by its **`capsule_id`** — the SHA-256 of its canonical
content (a *content address*). A capsule links to a prior one by putting that id in
**`chain.parent_capsule_id`**, with a **`relation`**:

```jsonc
"chain": { "parent_capsule_id": "4650e8f9…16f03", "relation": "confirms" }
```

Because the link is a content address, it is **global and position-independent**: it
points at a capsule by *what it is*, not by where it sits or who holds it. Everything
below follows from that one fact.

## Within one agent — the trail

You build a trail by pointing each new capsule at its parent with `confirms=`:

```python
from capsule_emit import seal

# the agent attempts the action
attempt = seal({"po_id": "PO-7781"}, action="write_po", operator="acme-co", developer="po-agent@v1",
               effect={"type": "write_po", "status": "dispatched"})

# later, your system confirms it landed — chained to the attempt
done = seal({"po_id": "PO-7781"}, action="write_po", operator="acme-co", developer="po-agent@v1",
            verdict="confirmed",
            effect={"type": "write_po", "status": "confirmed"},
            confirms=attempt.capsule_id)        # ← chain.parent_capsule_id
```

That turns *approved → executed → confirmed* (and human-in-the-loop approval) into one
verifiable sequence in your ledger. (See [tutorial 2](tutorials/02-confirming-and-chaining.md).)

## Across agents — under revision

> **⚠ Under revision.** This section previously recommended `confirms=` for
> linking capsules across different organizations/ledgers. That guidance is
> being replaced: a dedicated cross-stream reference mechanism has been decided
> and is not yet shipped. **Don't use `confirms=` to link capsules across
> different organizations/ledgers in new integrations** — treat it as valid only
> for links within one stream (see "Within one agent" above). This section will
> be rewritten in full once the replacement ships.

The mechanical fact that doesn't change: because `capsule_id` is a content
address, a capsule in one ledger can reference a capsule in another by id alone
— no shared database is required. What's changing is which relation you use to
express that cross-stream link; this page will be updated when that lands.

> Today `confirms=` writes `relation: "confirms"`. The spec also defines `supersede` /
> `escalate`; richer relation values (and a `relation=` parameter) are
> [registry-extensible](going-deeper.md), not yet surfaced on `seal()`/`carry()`/`received()`/`compose()`.

## An agent has *many* chains — the ledger is a DAG, not a line

Don't picture "the agent's chain." Picture the **ledger as a forest**:

- A capsule has **at most one parent** (`chain.parent_capsule_id`), but **many capsules
  can point at the same parent** (fan-out — several confirmations or supersessions of one
  action), and a capsule can be the parent of many.
- **Most capsules are standalone** (no chain) — a lone action with no follow-up.
- So each consequential action grows its *own* chain (`approved → executed → confirmed`),
  and the ledger is the whole collection — a **directed acyclic graph**.
- It **can't cycle**: a parent's id exists before its child is sealed, so no capsule can
  ever be its own ancestor.
- Distance doesn't matter — a capsule can chain to the *immediately* prior capsule or one
  far earlier; it's the same operation, since it's an id, not a position.

## Verifying a chain

To check a link, you need **both** capsules (or at least the parent's id and bytes).
`agent-action-capsule verify --store` over a ledger checks each capsule's own seal **and**
the chain links it can see:

```bash
agent-action-capsule verify --store ./ledger.jsonl
```

The verifier recomputes each `capsule_id` and confirms a child's `parent_capsule_id`
matches the parent's recomputed id. Tamper with either capsule and the link breaks — the
chain is exactly as trustworthy as the content-addressing underneath it.

## Why cross-agent chaining works at all

It works **because the capsule is a neutral open format**, not a vendor's internal record.
Emitter B can reference emitter A's capsule because both produce the *same* standard
capsule, the `capsule_id` is computed the *same* way everywhere, and any verifier can
check the link from the bytes. A format owned by one vendor couldn't do this across an
org boundary; an open, content-addressed one can. (More: [going deeper](going-deeper.md).)

---

*Related: [concepts](concepts.md) (the `Chain` word) · [anatomy](anatomy.md) (the `chain`
field) · [tutorial 2](tutorials/02-confirming-and-chaining.md) (hands-on).*
