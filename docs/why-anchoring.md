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
gets you **registered**: the record is now checkable by a party who trusts
neither you nor your runtime.

- **Existed at time T.** The receipt proves this exact capsule was logged at that
  time — so it can't have been built after the fact (hole 3 closed).
- **Omission becomes detectable.** Because the log is append-only and shared, you
  can't quietly drop an entry without it showing (hole 2 made visible).
- **Checkable without trusting you.** Anyone can verify a capsule against the log
  offline — they trust the *log*, not you.

That's the leap from *tamper-evident* (you didn't edit it) to *independently
verifiable* (someone who distrusts you can confirm it) — but it's a leap to
**registered**, not automatically to **witnessed**. A single log, even a shared
one, can still show two different views to two different readers; registration
alone doesn't rule that out. Closing that gap is what a *witness* does (next
section) — `capsule-emit`'s own checkpoint stream (on by default since 0.5.0,
see [`docs/checkpoint.md`](checkpoint.md)) does exactly this, at
*single-witness* strength, once it registers.

## Be precise about what it proves (and doesn't)

Anchoring proves **existence, integrity, and time** — *that this exact record was
sealed and logged when it says.* It does **not** make the recorded claim *true*, and
by itself it does **not** rule out the log showing different histories to different
parties. A capsule that says "the payment settled" is still your runtime's word that
it settled. The honest ladder:

- **Self-attested (a record you keep):** tamper-evident, but trust-the-keeper.
- **Registered / anchored (digest in a shared log):** + existed-at-T,
  omission-resistant, independently checkable — but you're still trusting that
  *one* log operator not to equivocate. This is where `capsule-emit`'s
  per-capsule anchor channel sits — as of 0.5.0 it is an explicit, non-default
  opt-in (`anchor=True` or `CAPSULE_ANCHOR=legacy-on`), not the default path;
  see "In practice" below.
- **Witnessed (single witness):** a Transparency Service has registered a
  signed *checkpoint* over your stream — vouching that the records under it
  **existed, in that order, and haven't been rewritten since** (existence +
  order + non-deletion). `capsule-emit`'s default checkpoint
  ([`docs/checkpoint.md`](checkpoint.md), on since 0.5.0) reaches exactly this
  tier once it registers. It's a real upgrade over self-attested — but it is
  **not** the next tier: one witness can still show a different view to a
  different party, and — like registered/anchored — a witness never vouches
  that the record's *content* is true, only that it exists, is ordered, and
  wasn't deleted.
- **Multi-witness (equivocation-resistant):** the same checkpoint is
  independently co-signed by, or registered to, more than one
  independently-operated log — honestly described as "witnessed up to size S
  at time T" by each — so equivocation now requires collusion. A witness
  operated by *you*, or by a party closely related to you, buys less than one
  with no relationship to you at all (self < peer < independent).
- **Counter-signed / confirmed:** a separate axis from the witnessing ladder —
  when the other party signs the outcome (e.g. the bank signs settlement) or a
  confirmation capsule [chains](concepts.md) to the action.

Anchoring today moves you from the first tier to the second; the default
checkpoint stream moves your *stream* to the third. Don't claim the fourth or
fifth for free.

## What actually leaves your machine

**Only a SHA-256 digest** — the `capsule_id`. Your vendors, amounts, operator,
prompts, and outputs never go to the log. The log learns that *some* record with
*that* fingerprint existed; it learns nothing about its contents. (Verification later
re-hashes your held capsule and checks the fingerprint against the log.)

## Why a *shared* log, not your own

A transparency log **you** run isn't proof to an outsider — it has the same
"trust-the-keeper" problem as your ledger. A **shared** log — one you don't
control — lets a counterparty check your capsule without trusting *you*; it
does not, by itself, mean nobody has to trust the log's *operator* (that's the
registered-vs-witnessed distinction above). `capsule-emit` anchors by default to
the free hosted log at `anchor.agentactioncapsule.org`, a single-operator log;
you can self-host or repoint (`AAC_ANCHOR_URL`), but either way the trust
property only holds when the log is one the *verifier* also trusts.

## In practice

**As of 0.5.0, this per-capsule anchor channel is OFF by default** — the
checkpoint/witness stream (see [`docs/checkpoint.md`](checkpoint.md)) is the
only default egress path. Older guides showing `anchor=True` per seal
describe the pre-0.5.0 surface. The channel still exists, as an explicit,
non-default opt-in kept for one release as a rollback path:

- **Opt-in only** — pass `seal(..., anchor=True)`, or set
  `CAPSULE_ANCHOR=legacy-on` process-wide. Async and non-blocking either way
  — the call doesn't wait on the network.
- **Digest-only**, so it's safe for sensitive payloads.
- **Off by default / explicitly off** — leave `anchor` unset (the default),
  or pass `anchor=False`.
- **Repointable** — `AAC_ANCHOR_URL=…` or `seal(..., anchor_url=…)`; the open-source
  log service is [`capsule-anchor`](https://github.com/action-state-group/capsule-anchor).

---

*Next: [the public log, explained](the-public-log-explained.md) (Merkle proofs, what's
visible, what you can share) · [concepts](concepts.md) · [anatomy](anatomy.md) · or
verify it yourself in [going deeper](going-deeper.md).*
