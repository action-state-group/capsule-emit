# What's consequential? The two-signal rule

An Agent Action Capsule is a *consequential-action record*. Not every tool call
is worth sealing — but the ones that are must never be silently missed.

This page gives you the test. It is deliberately short because a test you cannot
hold in your head will not be applied consistently.

## The rule in one sentence

**Seal what changes the world, plus reads of sensitive data; everything else is
observability.**

That sentence reduces to two independent signals. You need neither to be a
domain expert nor to label every call. You read the signals the runtime already
carries.

---

## Signal 1 — does it change the world?

This is Bertrand Meyer's **Command–Query Separation** (1988): every method is
either a *command* that mutates state or a *query* that returns data — never
both. "Asking a question should not change the answer."

The web encodes the same split as HTTP **safe methods** (RFC 9110, §9.2.1):
`GET`/`HEAD` are read-only; `POST`/`PUT`/`DELETE` are not. MCP encodes it as
the `readOnlyHint` tool annotation. Three ecosystems, one question.

| Runtime signal | Command (seal) | Query (no capsule) |
|---|---|---|
| HTTP verb | POST, PUT, PATCH, DELETE | GET, HEAD, OPTIONS |
| MCP `readOnlyHint` | `false` or absent | `true` |
| MCP `destructiveHint` | `true` | — |
| Commit step present | yes | — |

**A command is consequential and seals; a query does not.**

### When the hint is absent or wrong

MCP annotations are *hints, not enforcement*. A tool server may omit or
misstate them. **An absent `readOnlyHint` is not evidence of a read.**

> **Unknown defaults to sealed / gated — fail-safe, never fail-open.** If the
> runtime signal is missing and you cannot determine the effect, treat the call
> as consequential and seal it. Over-gating a genuinely harmless call is the
> correct cost; an ungated consequential action is the failure this record layer
> exists to prevent.

A trusted hint may only ever *downgrade* effort when present — it can never
silently exempt a call.

### Grey areas Signal 1 already resolves

- **A read that writes a log, cache, or analytics record.** RFC 9110 is
  explicit: incidental side effects do not make a method unsafe. Still a query
  → no capsule.
- **An idempotent write.** MCP's `idempotentHint` governs retry and
  deduplication, not whether the action is consequential. An idempotent `PUT`
  is still a command → seals.
- **A "query" tool that triggers a downstream command** (a search that also
  books). Classify by the *effect*, not the name: the booking is the effect
  boundary. A tool whose name says read but whose effect is a write is a
  mislabeled command.

---

## Signal 2 — is the data sensitive?

A pure read can still be an event you must prove — not for what it *did*, but
for what it *touched*. This is how HIPAA (§164.312(b)) and PCI DSS treat
audit-log requirements: viewing or exporting a patient record is a logged,
auditable access; reading a public price list is not. The determinant is the
**classification of the target resource**, not the operation type.

A query against a resource tagged sensitive (PHI, PII, regulated data, secrets)
is a **privileged access** and seals. An ordinary query against an untagged
resource does not.

### How a privileged read is determined

You do not decide this per call. The resource is tagged once. Regulated
organisations already maintain a data classification (they must, for HIPAA / PCI
/ GDPR) so the sensitive stores are already known. The classifier checks that
tag the same way it checks the read-only hint. An untagged-but-suspicious
resource falls to the fail-safe (treat as sensitive, surface for confirmation)
rather than silently passing.

### Signal 2 is engine-side only

Signal 2 requires knowing the resource identity and its classification tag —
information available to the **engine** (the component that makes decisions and
knows its data model) but not to the **gateway** (which sees traffic bytes, not
data tags). Pushing resource classification down to the wire would add real
complexity for little gain.

**In capsule-emit:** the gateway adapter (`agentgateway`) evaluates Signal 1
only. Decorator adapters (`@emitter.tool()`) seal what you explicitly wrap; use
`action_type="fyi"` to mark reads you chose to wrap, and `seal_reads=False` on
the emitter to skip them entirely. Signal 2 is implemented in the engine layer
using direct `emit()` calls — capsule-emit provides the sealing primitive; the
engine decides when a read is privileged.

---

## The layering in one table

| Layer | Evaluates | Notes |
|---|---|---|
| **Gateway** (`agentgateway`) | Signal 1 only (allow-list at config) | Sees traffic, not data tags. Passes reads un-sealed. |
| **Decorators** (`@emitter.tool()`) | Developer-explicit | Wrap commands. Optionally wrap sensitive reads with `action_type="fyi"` and control with `seal_reads=`. |
| **Engine** | Signal 1 + Signal 2 | Knows resource classification; seals privileged reads directly via `emit()`. |

See [adapter-patterns.md](adapter-patterns.md) for the full how-to-choose guide.

---

## Why reads are not sealed by default

The original reason to seal reads — capturing what the agent saw before it
acted — is better served a different way. Sealing a read proves "the agent saw
X," but the link "saw X, therefore did Y" remains the agent's self-report, which
is the self-grading weakness the whole capsule thesis rejects.

The provable version is already in the capsule format: when an action's
correctness depends on specific input data, **bind those input digests onto the
action capsule** (the grounding path / `value_grounded`). The handful of reads
that actually grounded a decision become input digests *on the action capsule*;
the rest belong to the observability / OTel layer this record system composes
with, not replaces.

---

## References

The two-signal rule borrows established vocabulary rather than coining new terms.

- **Command–Query Separation** — Bertrand Meyer, *Object-Oriented Software
  Construction* (Prentice Hall, 1988). The command-vs-query distinction
  underlying Signal 1.

- **Safe and idempotent method semantics** — IETF **RFC 9110, "HTTP Semantics"**
  (Fielding, Nottingham, Reschke, 2022), §9.2.1–9.2.2. The "incidental side
  effects do not break safety" rule resolves the read-with-logging grey area.

- **Tool risk annotations** (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`) — the **Model Context Protocol** (originated by Anthropic;
  now an AAIF / Linux Foundation project). The runtime-present signal the
  classifier reads — noting MCP's own framing that these are *hints, not
  enforcement*.

- **Auditable access to sensitive data** — **HIPAA Security Rule, 45 C.F.R.
  §164.312(b)** (audit controls) and **PCI DSS** logging requirements. The
  basis for Signal 2 keying on data classification rather than operation type.
