# The bundle-request shape — a graduated evidence-request primitive

A short reference for one small, reusable shape: how a caller asks a producer for a
bundle of sealed capsules, and how the producer answers — honestly, in exactly one of
three states, regardless of who asked.

This is a **request/response envelope**, not a new capsule field. It composes with
everything already in this repo: what travels over the wire is `{subject, coverage,
derivation, deadline}` in, `{status, ...}` out; what gets returned (when returned) is
capsules you already know how to seal and verify.

## Why this shape

Two independent integrations converged on the same request pattern: "give me a bundle
of your history, covered to some anchor, optionally re-derived under a named rule, by
some deadline." Rather than let each integration invent its own request vocabulary,
this shape standardizes it once so any caller and any producer can agree on the wire
format without agreeing on anything else.

## The request

```json
{
  "subject": {
    "kind": "full_history" | "account" | "record" | "range",
    "id": "<string, when kind == \"account\">",
    "digest": "<sha256 hex, when kind == \"record\">",
    "range": { "from": "<opaque cursor>", "to": "<opaque cursor>" }
  },
  "coverage": {
    "kind": "pin" | "min_freshness",
    "digest": "<sha256 hex of a checkpoint, when kind == \"pin\">",
    "min_freshness": "<ISO 8601 duration or timestamp, when kind == \"min_freshness\">"
  },
  "derivation": {
    "kind": "token" | "c_digest",
    "token": "<registry-governed derivation name, when kind == \"token\">",
    "c_digest": "<sha256 hex of a sealed derivation/template capsule, when kind == \"c_digest\">"
  },
  "deadline": "<ISO 8601 timestamp, optional>"
}
```

| Field | Required | Meaning |
|---|---|---|
| `subject` | ✅ | What is being asked for. Exactly one `kind`, never inferred from which other fields are present. |
| `subject.kind = full_history` | | Everything the producer holds for this relationship. No `id`/`digest`/`range`. |
| `subject.kind = account` | | A named subject the producer tracks by `id` (opaque to the requester — an identifier the producer already publishes, never a slot the requester invents). |
| `subject.kind = record` | | One specific capsule, addressed by its own digest. |
| `subject.kind = range` | | A bounded span, addressed by two opaque cursors the producer defines (e.g. checkpoint sequence numbers). |
| `coverage` | ✅ | The completeness guarantee the response must satisfy. `pin` asks for exact coverage up to a named checkpoint digest; `min_freshness` asks for coverage no staler than a duration or timestamp. |
| `derivation` | optional | Requests the bundle **re-derived** under a named fold instead of returned as raw digests. `token` names a registered derivation; `c_digest` names one by the digest of a sealed capsule that declares it (see [the startup-lifecycle-template example](../examples/startup-lifecycle-template/) for a worked `c_digest`). Omitted entirely means "bundle only, no derivation." |
| `deadline` | optional | When the requester needs an answer by. Advisory — the producer's actual SLA is out of band. |

`subject`, `coverage`, and `derivation` are each **tagged unions**: the `kind` field is
mandatory and selects which sibling fields apply. A request MUST NOT populate sibling
fields for a `kind` it did not select — a `subject.kind == "full_history"` request
carries no `id`/`digest`/`range` at all, not empty ones.

## The response — three states, never a fourth

```json
{ "status": "artifact", "artifact": { "...": "one or more capsules, or capsule digests" } }
{ "status": "refused", "refusal": { "reason_class": "...", "detail": "...", "capsule_id": "..." } }
{ "status": "absent" }
```

- **`artifact`** — the bundle, satisfying the requested `coverage` and (if asked)
  `derivation`. Shape of `artifact` is subject-kind-specific and out of scope for this
  document; it is capsules or capsule digests, never a new payload format.
- **`refused`** — the producer declines. `reason_class` is a stable, registry-style
  string (e.g. `not_authorized`, `coverage_unavailable`, `derivation_unsupported`) —
  never free text alone. The refusal itself SHOULD be sealed as a capsule
  (`refusal.capsule_id`), so "I asked; they refused, for this stated reason" is itself
  evidence a requester can hold.
- **`absent`** — the subject does not exist. Distinct from `refused`: absence is a fact
  about the world, not a decision the producer made. Returning `refused` for a subject
  that never existed (to avoid confirming what does exist) is a legitimate choice a
  producer's policy can make — but it MUST be a deliberate `refused` with its own
  `reason_class`, never a silent reshaping of `absent` into `refused` or vice versa.

A response MUST be exactly one of these three. There is no fourth state (e.g. a bare
transport-level 404 or 403 in place of a reason-classed body) — the whole point of the
shape is that "why didn't I get it" is always answerable from the response body alone.

## Caller-invariance — MUST

**The producer MUST compute `status` and (when `refused`) `reason_class` identically
for every caller with the same standing**, and MUST NOT reshape the response based on
caller identity beyond what standing legitimately determines. Standing (who is entitled
to what) is a policy question entirely outside this protocol; this protocol only
guarantees that the *mechanics* of asking, granting, and refusing are honest and
uniform. Concretely:

- Two callers with identical standing asking the identical request MUST receive the
  identical `status` and, on refusal, the identical `reason_class`.
- A producer MAY vary the *artifact's* disclosure scope by caller standing (that is
  what `derivation` and coverage tiers are for) — it MUST NOT vary *whether the request
  was answered honestly*.

## Both sides sealable

Both the request and the response MAY themselves be sealed as capsules. Doing so turns
the negotiation into evidence in its own right: "I asked for X under coverage Y; they
answered `refused: not_authorized`" is a citable, gradeable record independent of
whatever policy governed the answer. Sealing either side is optional per this document;
a producer that always seals refusals gets a durable, checkable refusal log for free.

## The A2A skill binding

Callers reach this shape over A2A as a named skill, replacing the older ad hoc
`get_history` pattern:

```json
{
  "id": "bundle-request",
  "name": "Evidence Bundle Request",
  "description": "Requests a graduated bundle of sealed capsules — full history, a named subject, a single record, or a range — under a stated coverage guarantee, optionally re-derived under a named fold. Responds with an artifact, a reason-classed signed refusal, or an explicit absent — never a bare 404.",
  "tags": ["evidence", "capsule", "audit", "a2a"],
  "inputModes": ["application/json"],
  "outputModes": ["application/json"]
}
```

Request/response bodies for this skill are exactly the shapes above.

### `get_history` — deprecated alias

`get_history(subject_id)` (no coverage, no derivation, no reason-classed refusal) is
the **deprecated alias** for the degenerate case:

```json
{ "subject": { "kind": "full_history" }, "coverage": { "kind": "min_freshness", "min_freshness": "PT0S" } }
```

Implementations SHOULD register `bundle-request` as the canonical skill and MAY keep
routing `get_history` calls to this same request internally for one deprecation window.
New callers should call `bundle-request` directly; `get_history` MUST NOT gain new
capabilities (coverage tiers, derivation) — those are `bundle-request`-only.

## Schema

The formal JSON Schema for both the request and response bodies is at
[`schemas/bundle-request.schema.json`](schemas/bundle-request.schema.json).

## Worked example

[`examples/startup-lifecycle-template/`](../examples/startup-lifecycle-template/) seals
a template capsule and uses its `capsule_id` as a `derivation.c_digest` value — a
concrete, runnable instance of the `c_digest` derivation form above.
