# Why anchoring makes it trustworthy

The capsule is tamper-evident on its own. So why bother writing its digest to a
public log? Because **tamper-evidence and trust are different things**, and the gap
between them is the entire reason this project exists.

## A record you keep is only as good as you are

Say you seal every action into a perfect, hash-chained ledger that you hold. It's
tamper-evident — change a byte and verification fails. Is that *proof* to a
counterparty, an auditor, or a regulator?

No. A record you keep has three holes that tamper-evidence can't close:

1. **False from the start.** Tamper-evidence proves you didn't change an entry *after*
   sealing — not that the entry was ever true. You can cryptographically seal a lie.
2. **Cherry-picked.** You decide which entries to show. A perfect ledger you disclose
   selectively still lets you hide the inconvenient action.
3. **Built after the fact.** Nothing in a file you control proves *when* it existed —
   you could assemble a clean, consistent ledger *after* a dispute starts.

All three survive a tamper-evident log, because they're not about tampering — they're
about the fact that **the party who kept the record is not a disinterested witness.**
This is the precise sense in which *"your logs are your own word."*

## What an anchor adds

Anchoring writes the capsule's **digest** to a **shared, append-only transparency
log** — one that no single party owns. In return you get an
[RFC 9162](https://www.rfc-editor.org/rfc/rfc9162) **inclusion-proof receipt**. That
brings in the missing thing: an **independent witness**.

- **Existed at time T.** The receipt proves this exact capsule was logged at that
  time — so it can't have been built after the fact (hole 3 closed).
- **Omission becomes detectable.** Because the log is append-only and shared, you
  can't quietly drop an entry without it showing (hole 2 made visible).
- **Checkable without trusting you.** Anyone can verify a capsule against the log
  offline — they trust the *log*, not you.

That's the leap from *tamper-evident* (you didn't edit it) to *independently
verifiable* (someone who distrusts you can confirm it).

## Be precise about what it proves (and doesn't)

Anchoring proves **existence, integrity, and time** — *that this exact record was
sealed and logged when it says.* It does **not** make the recorded claim *true*. A
capsule that says "the payment settled" is still your runtime's word that it
settled. The honest tiers:

- **Self-attested (a record you keep):** tamper-evident, but trust-the-keeper.
- **Anchored (digest in a shared log):** + existed-at-T, omission-resistant,
  independently checkable — cross-party.
- **Counter-signed / confirmed:** strongest — when the other party signs the outcome
  (e.g. the bank signs settlement) or a confirmation capsule
  [chains](concepts.md) to the action.

Anchoring moves you from the first tier to the second. Don't claim the third for free.

## What actually leaves your machine

**Only a SHA-256 digest** — the `capsule_id`. Your vendors, amounts, operator,
prompts, and outputs never go to the log. The log learns that *some* record with
*that* fingerprint existed; it learns nothing about its contents. (Verification later
re-hashes your held capsule and checks the fingerprint against the log.)

## Why a *shared* log, not your own

A transparency log **you** run isn't proof to an outsider — it has the same
"trust-the-keeper" problem as your ledger. The value comes from the log being
**shared and independent**: that's what a counterparty can rely on without trusting
you. `capsule-emit` anchors by default to the free hosted log at
`anchor.agentactioncapsule.org`; you can self-host or repoint (`AAC_ANCHOR_URL`),
but the trust property only holds when the log is one the *verifier* also trusts.

## In practice

- **On by default**, async and non-blocking — `emit()` doesn't wait on the network.
- **Digest-only**, so it's safe for sensitive payloads.
- **Off-able** — `emit(..., anchor=False)` for offline/local-only.
- **Repointable** — `AAC_ANCHOR_URL=…` or `emit(..., anchor_url=…)`; the open-source
  log service is [`capsule-anchor`](https://github.com/action-state-group/capsule-anchor).

---

*Next: [the public log, explained](the-public-log-explained.md) (Merkle proofs, what's
visible, what you can share) · [concepts](concepts.md) · [anatomy](anatomy.md) · or
verify it yourself in [going deeper](going-deeper.md).*
