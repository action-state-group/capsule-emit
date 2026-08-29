# Startup lifecycle template — a worked example of intension vs. extension

A runnable, worked example answering one question: **"what are we bundling?"** — when
does a set of capsules mean "a complete startup," and how do you name that meaning
without naming any one run?

```
examples/startup-lifecycle-template/
├── template.json      # the declared expected-set (the intension)
├── completeness.py    # the diff check (order + completeness), independently testable
├── run_example.py     # the full worked example
└── README.md           # this file
```

## Run it

```bash
cd examples/startup-lifecycle-template
python run_example.py
```

No network required.

## The mechanism

A **template** declares an expected set of `action_id`s, an order, and a completeness
rule — this is the **intension**: what *should* be there. It names no run; it is a
reusable, registrable declaration, illustrated here by `template.json`'s 7-step
startup sequence (`infra.dnssec_enabled` → … → `site.demo_published.walkthrough`).

A concrete run cites the actual sealed capsule digests it produced, in the order they
happened — this is the **extension**: what *is* there. `run_example.py` simulates one,
deliberately incomplete and out of order, so the check has something to report.

**Never select by name at verify time.** `action_id`s are producer-chosen strings; they
live in the template, not in some ambient convention a verifier has to already know.
The diff is always computed against the *declared* template, never guessed from
whatever the run happened to contain.

`completeness.evaluate(template, observed)` reports the diff as findings — a missing
member or an out-of-order member is always a reported finding, never a silent gap.

## The template capsule, and `c_digest`

The template itself is sealed as a capsule (`register_lifecycle_template` in the demo).
Sealing the *declaration* — not just writing it to a file — gives the template its own
provenance: who declared it, when, tamper-evident and witnessed like any other capsule.

The resulting `capsule_id` is a **`c_digest`** — one of the two ways to name a
derivation in a [bundle-request](../../docs/a2a-request-shape.md)'s `derivation` field
(the other being a registry `token`). Citing a template by `c_digest` instead of by
name means the derivation itself is pinned to an exact, sealed, checkable declaration —
not to a string that could mean something different tomorrow.

## What this example does NOT do

It does not mint a capsule that *cites* the 7 member capsules by digest (the natural
next step, using the spec's cross-record `references[]` mechanism) — that mechanism is
landing separately. This example stops at sealing the template and computing the
intension/extension diff locally.

## Boundary

Mechanism-only: `template.json`'s 7 steps are illustrative placeholders for a generic
startup sequence, not any specific deployment's real data.
