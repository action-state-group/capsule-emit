# Going deeper — and popping out

`capsule-emit` is deliberately small: it **produces** capsules. Two questions
naturally come next, and they go in opposite directions.

- *"How do I really trust this? What's underneath?"* → go **down the stack**.
- *"Wait — could this also **stop** a bad action, not just record it?"* → pop **up**
  to a gateway.

```
              ▲  enforcement (act on the record)
   gateway    │  a compatible gateway reads your manifest and ENFORCES
  (e.g. gopher-ai, OSS)   — HITL, autonomy, effects.  Same files, no emit() change.
  ───────────┼───────────────────────────────────────────────────
   YOU →   capsule-emit   produce: one call, sealed + anchored
              │
   verify   agent-action-capsule   the spec + reference verifier (is this capsule valid?)
              │
   substrate  scitt-cose   COSE_Sign1 signed statements + RFC 9162 receipts
              ▼  (is it provably in the log, without trusting the log?)
```

Everything below `capsule-emit` is **neutral, payload-agnostic substrate** you can
read, verify against, and reimplement. Everything above it is **optional
functionality** you opt into when you want more than a record.

---

## Down the stack — understand & verify deeper

### 1. `agent-action-capsule` — the spec + the verifier
The capsule **format** itself, plus the reference verifier you've been running.
Start here if you want to know exactly what a valid capsule is, or verify in your
own pipeline.

- The reference verifier: `pip install agent-action-capsule` →
  `agent-action-capsule verify ./ledger.jsonl`.
- The Internet-Draft (the wire format, field by field):
  [`draft-mih-scitt-agent-action-capsule`](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/)
  — see also `spec/REGISTRY.md` for reserved members.
- Conformance vectors (known-good and known-bad capsules) live under
  `test-vectors/` — your producer should match them.

### 2. `scitt-cose` — the transparency substrate
One layer down: the **generic SCITT + COSE Receipts** machinery. This is what makes
"anchored" mean something — it verifies a `COSE_Sign1` **Signed Statement** and an
**RFC 9162** inclusion proof, i.e. *"this statement is provably in the log — without
trusting the log operator."* It is **payload-agnostic** (it knows nothing about
capsules) and **not** a hosted log service itself.

- Repo: [`scitt-cose`](https://github.com/action-state-group/scitt-cose) — only
  dependencies are `cbor2` + `cryptography`; COSE is implemented from scratch.
- Read this if you want to verify receipts yourself, run your own transparency log,
  or understand the cryptography under the anchor.

**How they compose:** `capsule-emit` produces a capsule → `agent-action-capsule`
defines and verifies it → `scitt-cose` verifies the COSE signature and the
transparency receipt underneath. Each layer is independently useful and separately
verifiable — on purpose.

---

## Popping out — "could this be a gate?"

You'll hit this moment: *the capsule already knows the rules (your `manifest.md`) and
the verdict — could it just **block** the action when a rule fails, instead of only
recording that it did?*

Yes — but **that's a different job, and not `capsule-emit`'s.** `capsule-emit`
**declares and records**; it never blocks. Enforcement — running the constraints for
real, holding an action for human approval, raising autonomy from *narrate* to
*act*, driving and confirming effects — belongs to a **gateway** that sits **above**
capsule-emit.

The clean part: the gateway reads the **same `manifest.md`** you already wrote and
calls the **same `capsule-emit`** underneath. So:

> **Adopt sealing now with `capsule-emit`. Turn on enforcement later by putting a
> compatible gateway in front — with no change to your `emit()` calls or your
> manifests.**

A compatible gateway — for example **[`gopher-ai`](https://github.com/action-state-group/gopher-ai)**,
an open-source gateway on the **same core** (`agent-action-capsule`) as `capsule-emit` —
adds the enforcement engine: constraint checks that actually block, human-in-the-loop
approval, autonomy dialing, and effect execution + confirmation. Because both rest on
the same core, the capsules are byte-compatible (`capsule_id` identical): you don't
rewrite your agent or re-emit anything — you route its consequential actions through
the gate, and it reads the very capsules you were already producing.

That's the natural progression: **record → verify → enforce**, each an opt-in step on
the same foundation.

---

*Back to the basics: [concepts](concepts.md) · [tutorials](tutorials/) ·
[anatomy](anatomy.md) · [adapters](adapters/).*
