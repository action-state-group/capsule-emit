# The public log, explained (and what it does and doesn't reveal)

You don't need any of this to *use* `capsule-emit` — anchoring is on by default and it
just works. But the moment a teammate, security reviewer, or counterparty asks *"wait,
you're putting our stuff on a **public** log?"*, you'll want to be able to answer
calmly. This page is that answer, in plain English. Skim the parts you need.

*(Followed the `*` next to "public log" in the README? This is it — the one-line version
is in [What's actually on it?](#whats-actually-on-it-the-important-part) below: only a
fingerprint and a timestamp are logged, never your data.)*

## What is the "public log"?

It's a **transparency log** — an append-only, tamper-evident list that anyone can
read and check, run by an operator but built so the operator **can't cheat**. The
same kind of log secures HTTPS (Certificate Transparency logs record every TLS
certificate so a sneaky one can't hide). Ours records **capsule fingerprints**.

Two properties make it special:
- **Append-only.** Entries can be *added*, never edited or deleted. History can't be
  rewritten.
- **Independently checkable.** You don't have to trust the operator's word — the math
  (below) lets anyone verify the log is behaving.

Think of it as a **public notary's ledger that only ever grows** — and one where you
can prove an entry is in it, and prove the notary never tore a page out.

## What's actually on it? (the important part)

**Only a fingerprint and a time.** When you `emit()`, the only thing sent to the log
is the capsule's **SHA-256 digest** (its `capsule_id`) — a fixed-length, one-way hash.

The log learns: *"some record with fingerprint `9fddfcec…` existed at 18:01 UTC."* It
learns **nothing** about what that record contains. Your vendors, amounts, operator,
prompts, and outputs **never leave your machine.**

| On the public log | Stays with you (in the capsule you hold) |
|---|---|
| the digest (`capsule_id`) | the action, operator, developer |
| the time it was logged | the input/output (themselves stored as hashes) |
| the inclusion receipt | the effect, verdict, chain links |

So "public log" ≠ "public data." It's a public list of **opaque fingerprints**. A
fingerprint can confirm a record you're *shown* is the real one; it can't reveal a
record you're *not* shown.

## How the math makes it trustworthy (Merkle, the dummies version)

The log stores fingerprints as the leaves of a **Merkle tree**: hash the leaves in
pairs, hash those pairs, and so on up to a single **root hash**. The root is one
"master fingerprint" of the *entire* log — change any leaf and the root changes.

Two things fall out of that, and they're the whole point:

- **Inclusion proof ("I'm in the log").** To prove your capsule is in a log of
  millions, you don't download the log — you get a **receipt**: your leaf plus a
  handful of sibling hashes (about log₂(N) of them — ~20 for a million entries). Anyone
  can re-hash those up to the root and check it matches the log's signed root. *Like
  proving a word is in the dictionary by showing its page and a couple of neighbors —
  not by handing over the whole dictionary.*
- **Consistency proof ("the log didn't rewrite history").** Anyone can check that a
  newer root still contains everything the older root did — so the operator **can't
  quietly drop or alter** a past entry without every watcher noticing.

The operator **signs** each root, so you're checking a signature + some hashes — no
trust in their good intentions required. That signed receipt is what
[RFC 9162](https://www.rfc-editor.org/rfc/rfc9162) (the Certificate Transparency
standard) specifies, and it's what `agent-action-capsule verify` checks.

## What can be seen, what can't, and what you can *choose* to share

There are levels of disclosure, and **you control how far up you go, per party:**

1. **The receipt only** — proves *a* record existed at time T. Reveals nothing about
   contents. (Hand this to someone who just needs to know "something happened, on the
   record.")
2. **The capsule itself** (`cap.capsule`) — proves *what happened and that it was
   authorized*: the action, operator/developer, the may/did verdict, the effect, the
   chain. It's **already content-private** — your inputs and outputs are in it only as
   *digests*, never raw — so you can show the whole capsule and still expose no prompts,
   vendors, or amounts.
3. **The capsule + a raw value you held back** — to prove what an input or output
   actually *was*, reveal the value you kept; they re-hash it and match the digest in the
   capsule. You choose which values, for which party. (Granularity is per input/output
   blob — each is committed as one digest.)
4. **Field by field** — reveal *some* fields (the amount) while withholding others (the
   vendor), each still provably part of the capsule. This needs **per-field salted
   commitments** — the [selective-disclosure companion spec](going-deeper.md) (SD-JWT
   style), **not** the default producer (which digests each input/output as one blob).
   Levels 1–3 work today.

The progression is the feature: **start by proving it exists, reveal more only as
trust or need grows — to exactly the parties who need it.**

## How to explain it in one sentence

> "We don't put our data anywhere public — only a one-way fingerprint of each action
> goes to a shared, append-only log, so anyone we choose to show the record to can
> confirm it's real and unaltered, without trusting us, and without us exposing
> anything we didn't mean to."

---

## FAQ

**Is our private/customer data on a public log?**
No. Only a SHA-256 digest is logged. The content stays with you. The log is a list of
fingerprints, not records.

**What if the log operator is malicious — can they fake or hide things?**
They can't rewrite history (consistency proofs catch it) and can't forge your entry
(it's your capsule's hash, and the receipt is checked against a signed root). The
worst a bad operator can do is refuse service or try to show different roots to
different people ("split-view") — which independent monitors are designed to catch,
and which is exactly why the log must be **shared/independent**, not one you run for
yourself.

**Can someone forge a receipt?**
No — a receipt only verifies if the sibling hashes actually recompute the log's signed
root for your exact capsule. Wrong capsule or tampered receipt → verification fails.

**What about GDPR / "right to be forgotten" on an append-only log?**
Only digests are on the log, and a one-way hash of data you hold isn't the personal
data itself — the deletable content lives with you, not on the log. So append-only and
erasure aren't in conflict. *(Not legal advice — check with your counsel for your
jurisdiction.)*

**Do I have to be online? What if I don't want to anchor at all?**
Anchoring is async and non-blocking, and fully optional: `emit(..., anchor=False)`
seals locally and skips the network. You can anchor later, or never. Without an
anchor you still have a tamper-evident record — just self-attested, not
independently witnessed ([why that matters](why-anchoring.md)).

**Can I use my own log instead of the hosted one?**
Yes — `AAC_ANCHOR_URL=…` or `emit(..., anchor_url=…)`; the log service
([`capsule-anchor`](https://github.com/action-state-group/capsule-anchor)) is
open-source. Just remember the trust property only holds when the log is one the
*verifier* also trusts — a log only you control isn't proof to an outsider.

**What does someone need to verify my capsule — a key? An account? Network?**
None. `pip install agent-action-capsule` and check from the bytes alone. The receipt
carries the proof; the verifier needs no key, no account, and (for the capsule itself)
no network.

**Why a digest and not, say, encryption?**
A digest is one-way — there's nothing to decrypt and no key to leak. It commits to the
content (so you can later prove what it was) without ever transmitting or storing the
content. Encryption would mean the data left your machine; a digest means it didn't.

---

*Related: [why anchoring makes it trustworthy](why-anchoring.md) ·
[anatomy of a capsule](anatomy.md) · [going deeper](going-deeper.md).*
