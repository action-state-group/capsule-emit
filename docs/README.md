# capsule-emit docs

Everything here is written to be read by a human dev **or** by a coding agent you point at it. No standards background needed.

## Start here

You add one `seal()` call at each consequential action, and you get a **witnessed, verifiable ledger** of what your agent did. That's the floor. Begin with the tutorials:

→ **[Tutorials](tutorials/)** — five-minute, copy-paste sessions, in order:
- **[See a ledger — know what your agent did](tutorials/see-a-ledger.md)** — start from the consumer question: *how do you know what your agent did?* Read two real, verifiable sample ledgers before you produce one.
1. **[Your first capsule](tutorials/01-your-first-capsule.md)** — seal an action, see it witnessed, see it land in your ledger, verify it. *(This is the full day-1 loop.)*
2. **[Confirming & chaining](tutorials/02-confirming-and-chaining.md)** — link "attempted" to "confirmed."
3. **[Reading your ledger](tutorials/03-reading-your-ledger.md)** — the whole trail in one view.
4. **[Declaring rules](tutorials/04-declaring-constraints.md)** — declare now, enforce later.

## Understand it

- **[Concepts in plain words](concepts.md)** — the seven words (capsule, seal, may/did, chain, break, witness, ledger), each tied to a field or a command.
- **[Anatomy of a capsule](anatomy.md)** — exactly what gets sealed: the two-tier structure and how each layer is captured.
- **[Chaining — within one agent, and across agents](chaining.md)** — how capsules link by content address into verifiable trails, and why the ledger is a DAG, not one line. Cross-organizational chaining guidance is under revision — see the page.
- **[Why anchoring makes it trustworthy](why-anchoring.md)** — why a record *you* keep isn't proof to anyone else, and how a shared, append-only log fixes that. The heart of the whole thing.
- **[The public log, explained](the-public-log-explained.md)** — plain-English (+ FAQ): what the transparency log is, how Merkle proofs work, what's visible vs hidden, and what you can progressively share. The page to read before someone asks *"you're putting our data on a public log?"*
- **[Concurrency — one log, one writer](concurrency.md)** — why two processes never write the same ledger at once, what the error looks like, and how to resolve it.

## Wire it into your stack

- **[Adapters](adapters/)** — let MCP / LangChain / CrewAI / Hermes (or any custom loop) emit capsules for you. Each page has a **paste-ready prompt for your coding agent**.
- **Quickstarts** — copy-paste, framework-specific: **[CrewAI](quickstart-crewai.md)** · **[LangGraph](quickstart-langgraph.md)**.
- **[The bundle-request shape](a2a-request-shape.md)** — how a caller asks for a bundle of capsules, and how a producer answers honestly in one of three states; includes the A2A skill binding and a [worked example](../examples/startup-lifecycle-template/).

## Go further

- **[Going deeper — and popping out](going-deeper.md)** — *down* into the spec + `scitt-cose` substrate to verify it yourself; *up* to a compatible enforcement gateway when you want capsules to **block**, not just record.

---

## The shape, in one picture

```
  seal()  →  capsule        the unit: one action, sealed (content-addressed)
     │  appends + witnesses
     ▼
  ledger.jsonl              what you keep: a witnessed, verifiable TRAIL  ← start here
     │  link related capsules
     ▼
  chain (confirms)          approved → executed → confirmed; HITL; disclosure
     │  declare rules in manifest.md
     ▼
  enforce (a compatible gateway, same file)     block, don't just record
```

The **capsule** is the atom; the **witnessed ledger** is the product; **chaining** and **enforcement** are rungs you climb when you need them.
