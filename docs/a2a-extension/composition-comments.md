# Draft composition comments — NOT POSTED

**These four comments are drafts only.** Per the v3.1 proposal's §17 governance
sequence and the 2026-09-06 amendment, they go out on the four threads below
BEFORE any `a2aproject/A2A` Issue is opened, so the project can hear back
before committing to a specific composition point. **Steven posts these; the
byline/handle/account is an operator decision this document does not make.**
Do not post from this repo, a bot, or any automated surface.

Each comment: (1) states the concrete question, (2) points at the proposal's
adjacent-work table (§4) rather than restating the whole proposal, (3) links
the provisional spec text so the maintainers can read the actual mechanism
instead of guessing from a summary, and (4) explicitly invites "no, don't
share a composition point" as a valid answer.

Provisional spec text to link from every comment (must resolve before
posting, per the 2026-09-06 amendment — "must resolve to the published
proposal text before any demo"):
`https://agentactioncapsule.org/extensions/a2a-task-evidence/v1`

---

## Comment for `a2aproject/A2A` #1713 (cross-organization accountability / OBO)

> This may be adjacent to what #1713 is scoping, so flagging before opening a
> separate Issue rather than duplicating discussion.
>
> We've been drafting an independent, narrower proposal — portable evidence
> for one terminal A2A Task (content binding + local-log inclusion + a
> Transparency Service registration, orthogonal verification properties,
> explicit `PREFERRED`/`REQUIRED` negotiation before an external effect). No
> delegation credential, no principal-chain semantics — just "what can a
> relying party independently check about a single Task's evidence."
>
> Draft: `<provisional URI above>`
>
> Question: does #1713's post-Task envelope already cover this, or is there
> a composition point worth sharing (e.g. this evidence reference riding
> inside whatever envelope #1713 lands on) before we open a separate Issue?
> Also fine if the answer is "these should stay separate" — just want to ask
> before filing rather than after.

## Comment for `a2aproject/A2A` #1769 (verifier-side artifacts / trust envelope)

> Flagging a possible overlap before opening a new Issue.
>
> We've drafted a narrower, POST-Task-only evidence proposal (no
> `allow`/`attenuate`/`deny` policy engine, no pre-action admission decision
> — just: did a terminal Task produce a portable, independently-verifiable
> evidence bundle, and which of ten orthogonal properties — content binding,
> signature, log inclusion, checkpoint signature, external registration,
> continuity, identity/authority, capture coverage, task binding, outcome
> corroboration — does that bundle actually establish, each reported
> `PASS`/`FAIL`/`NOT_PRESENT`/`NOT_CHECKED`/`INCONCLUSIVE`, never an
> aggregate `trusted: true`).
>
> Draft: `<provisional URI above>`
>
> Question: is there a shared verifier-artifact shape worth aligning on
> between this and #1769's admission/receipt work, or are the two
> intentionally at different points in the Task lifecycle (pre-action vs.
> post-Task) and best left separate? Genuinely asking before filing, not
> asserting an answer.

## Comment for `a2aproject/A2A` #1628 (`trust.signals[]`)

> Flagging before opening a separate Issue, since #1628 is Agent-Card-level
> trust signaling and this is Task-level evidence — related but not the
> same layer.
>
> Draft proposal: a per-Task evidence extension (request-time
> `PREFERRED`/`REQUIRED` declaration, explicit server acceptance before an
> external effect, a `TaskEvidenceReference` in Task metadata, an immutable
> evidence bundle via the existing `GetTask`/Artifact facilities). No
> precomputed trust signal, score, or ranking — deliberately per-Task, not
> aggregate.
>
> Draft: `<provisional URI above>`
>
> Question: should a `trust.signals[]` entry ever reference "this agent
> supports task-level evidence" as a discoverable signal, or is that better
> left purely to the extension advertisement in `AgentCapabilities.extensions`
> (which already does this)? Open to "these don't need to talk to each
> other."

## Comment for `a2aproject/A2A` discussion #1962 (verifiable reputation)

> This discussion is about reputation/aggregate trust; the draft below is
> deliberately NOT that, but the boundary seemed worth naming explicitly.
>
> We've been drafting a per-Task (not aggregate) evidence extension:
> negotiated before work begins, evidenced at Task completion, verified via
> ten orthogonal properties with no aggregate `trusted: true` and no score.
> Aggregate histories, pair accounts, "completed *n* of *m* Tasks," and
> reputation are explicit non-goals (§15 of the draft) — we think reputation
> systems are a legitimate but SEPARATE layer that could consume per-Task
> evidence like this as an input, not replace it.
>
> Draft: `<provisional URI above>`
>
> Question: is per-Task evidence (this draft) a reasonable building block
> for reputation work like this discussion, worth referencing explicitly, or
> would that framing create confusion between the two by association? Asking
> before filing a separate Issue.

---

## After posting (for whoever tracks replies)

Per the amendment: **the Issue on `a2aproject/A2A` proper only goes up after
replies to these four**, not concurrently. Track responses here or in the
outbox; a reply that says "yes, share point X" should get folded into the
Issue's own adjacent-work section (proposal §4) before filing, not left only
in a thread.
