# TRANSLATION — one surface, three registers

capsule-emit is **one** set of records and verbs, described in three vocabularies for three
readers. Developers read the **dev** column; auditors/GRC readers read the **auditor** column;
the I-Ds, registries, and verifier display strings use the **spec** column only. A claim never
lives in a name (the display-string deny-list — *verified/certified/compliant/guaranteed/pass/
approved/trust-*/tamper-proof* — applies in all three): names say what a thing *is*, never that it
is good.

> Developers already **log** what their code does; `seal()` is that same habit at the effect boundary, made provable. "log" here is the familiar reference point, not a capsule-emit verb.

## Nouns

| Concept | **Dev** | **Auditor** | **Spec** |
|---|---|---|---|
| your record | **receipt** / capsule | the signed record the producer authored; a self-attested claim until witnessed | capsule (AAC record) |
| the witness artifact | **stamp** | an independent party's confirmation the record's history existed at a time — never a statement about content | witness countersignature / RFC 9942 Receipt over the checkpoint |
| the log | **your log** / ledger | the producer's append-only history; tamper-evident by construction | CLL (Checkpointed Local Log, MMR) |
| the commitment | **checkpoint** ("the 32 bytes") | one signed value committing the entire history to date; the only thing that leaves the producer | CLL checkpoint (COSE_Sign1) |
| the witnessing party | **witness** ("the notary") | the party that makes the log checkable by a stranger; a *checkpoint-aware* witness also verifies continuity | witness / Transparency Service (SCITT) |
| an action's account | **proof** (of an action) | the signed membership claim: which records constitute one action | composition capsule |
| foreign bytes brought in | **received** | someone else's already-signed record, committed as-transmitted; not re-asserted by the holder | `as-transmitted` CPB binding |
| the hand-over file | **bundle** | digests + proofs + stamps; proves integrity/order/completeness, carries no content | bundle (record + inclusion proof + checkpoint + stamp + consistency proof) |
| content release | **disclose** | a deliberate, recorded release of underlying content to a named audience; itself logged | disclosure record |

## Verbs

| You type | **Dev** | **Auditor** | **Spec** |
|---|---|---|---|
| `seal(payload)` | "make a provable record" | mints a signed, logged, cadence-witnessed record | canonicalize (CPB/jcs) → sign (COSE_Sign1) → append (CLL) → checkpoint |
| `seal(who(...), can(...), did(...), audit(...))` | "bind these into one account" | membership claim over cited records, one per slot; asserts nothing new. Each member may be a fresh payload **or a receipt you already sealed** (referenced, never re-signed) | composition capsule (slot-annotated members) |
| `received(bytes, type=...)` — standalone or nested in `seal()`/a slot | "bring in someone else's signed record" | localizes a foreign signed artifact as-transmitted; the holder asserts nothing about its content | `as-transmitted` carry |
| `push()` | "checkpoint now" | forces an immediate checkpoint (tightens the time bracket) | CLL checkpoint issuance |
| `verify` | "check any of it, offline" | recomputes signatures, inclusion, checkpoint-chain consistency; grades honestly | scitt-cose verification |
| `status` | "where's everything on the ladder" | ladder position + witnessing lag per record | ladder + lag render |

The four slots are `who` / `can` / `did` / `audit` (identity / authority-or-mandate / the action / oversight). `can` is the authority slot — "what the agent *may* do." There is no `compose()`/`carry()`/`may()`: composition **is** nesting slot verbs inside `seal()`, and authority is `can`.

## The ladder

| Rung | **Dev** | **Auditor** | **Spec** |
|---|---|---|---|
| unsigned entry | "logged, not signed" | log-integrity only: order, completeness, tamper-evidence of the sequence — no authorship | unsigned CLL entry |
| **self-attested** | "signed by me" | record integrity by the producer's key alone; no third-party confirmation | `attestation_mode=self_attested` |
| **witnessed** | "an independent log confirmed it existed" | existence + order + completeness checkable **without trusting the producer** | witnessed checkpoint (≥1 valid stamp) |
| **countersigned** | "someone else vouched" | a non-producer's signed claim citing the record: counterparty or disinterested operator | counterparty / operator countersignature |

## Evidence status (the auditor's working vocabulary)

| **Dev** | **Auditor** | **Spec** |
|---|---|---|
| "not there" | absent — no evidence supplied (≠ failed) | `absent` |
| "there but unchecked" | present, not yet verified | `present_unverified` |
| "checked, good" | recomputed and matches | `checked_passed` |
| "checked, bad" | recomputed and does NOT match — a positive finding, not a gap | `checked_failed` |

## What it does NOT prove (the honesty rows)
- **A witnessed record is not a true record.** The stamp attests existence/order/completeness, never accuracy. A perfectly witnessed log of false records is a perfectly witnessed set of false records.
- **A derived verdict is not an adjudicated one.** `verify` never grades up: absence ≠ forgery, unsigned ≠ invalid, model-assisted ≠ deterministic.
- **Coverage is the range, not the population.** Completeness proves the committed range; whether it is *all* the traffic is a separate, human-signed scope census.
- **Registration ≠ Receipt ≠ Witnessing.** *Registration* is the act, a *Receipt* is what the service returns, *witnessing* is a party co-signing the checkpoint so the log can't equivocate.

## Three roles (not three products)
- **Record registration (legacy).** Attests one digest existed by time T — existence only, no order/completeness. The pre-CLL mechanism, strictly weaker than witnessing; opt-in legacy in 0.5.0, never a default.
- **Witnessing (default).** Attests your entire history — order, completeness, non-equivocation — as of a ~200-byte checkpoint; every record inherits via its inclusion proof. The default in 0.5.0. The default witness endpoint accepts **only** checkpoints, which makes the privacy claim *structural*: nothing payload-adjacent leaves by default, auditable from the endpoint's surface.
- **Countersigning.** A named party vouches about the record itself. It is a rung in the ladder and the spec; it is **not offered as a service today**. It compounds with witnessing, never substitutes for it.

*"anchor" is retired as service vocabulary (it collides with "trust anchor"). The legacy hostname stays only because existing Receipts reference it; docs call that surface "record registration (legacy)."*
