# Verified Invoice Example

A runnable worked example: an invoice action runs deterministic checks, emits a capsule, anchors it to a public transparency log, and verifies — and the capsule records **what was verified** and **how rigorously**.

```
examples/verified-invoice/
├── run_example.py      # the full end-to-end flow
├── invoice_checks.py   # illustrative check functions (three tiers)
├── manifest.md         # declared ruleset, pinned by digest in the capsule
└── README.md           # this file
```

## Run it

```bash
cd examples/verified-invoice
python run_example.py
```

No network required — anchoring is attempted if `AAC_ANCHOR_URL` is set or the public anchor is reachable; it is skipped gracefully otherwise.  All verifications run fully offline.

## What the capsule records

The core question this example answers: **when an agent acts, how do you know not just *what it did* but *what it was checked against, and how rigorously*?**

The sealed capsule carries:

| Field | What it records |
|---|---|
| `constraints[].id` | Which check ran (e.g. `invoice_reconciles`) |
| `constraints[].result` | Pass or fail |
| `constraints[].check_type` | **Assurance tier**: `standard`, `policy`, or `formal` |
| `constraints[].evidence_digest` | SHA-256 of the evidence the check ran on |
| `constraints[].method` | How the check works (`arithmetic_sum`, `threshold`, …) |
| `constraints[].blocking` | Whether this check gates execution |
| `disposition.verdict_class` | Gate outcome: `executed` or `blocked` |
| `compute_attestation.manifest_ref` | `sha256:<hex>` of `manifest.md` — the declared ruleset, pinned |

An auditor can retrieve the manifest by its digest, re-read exactly which constraints were declared, and confirm the capsule records the results for each one.  **The record is the product.**

## Three assurance tiers

The example ships exactly one check per tier to illustrate the shape:

### Standard — existence + math

```python
invoice_reconciles(invoice)         # line items sum = declared total
value_grounded(invoice, source_doc) # quoted unit-price matches source document
```

Deterministic, universal, cheap.  These run everywhere with no external dependencies.

### Policy — thresholds + governance

```python
amount_under_policy_cap(invoice)    # total < $10 000 (policy-v1.0)
```

The **policy version is committed in the evidence_digest** — the capsule records not just that a threshold was checked, but which version of the policy applied.  The declaration (in `manifest.md`) is separate from the enforcement (this check) and from the record (the capsule).  This is the same pattern as OPA / Cedar / AARM-style governance: *declare → enforce → record*.

### Formal — formal-methods result

```python
formal_arithmetic_verified(invoice) # stub: records a pre-existing proof result
```

The capsule can **record a formal-verification result at the highest assurance tier** — even when the prover runs separately.  The `check_type: "formal"` field grades it.  Replace the stub with a real prover call (Z3, Lean, Isabelle) and the proof reference.

The teaching point: *verification ranges from a math check to a formal proof.  Whatever rigor you apply, the capsule records the tier and the result — so an auditor knows not just that it was checked, but how rigorously.*

## The determinism theme

This example uses only deterministic checks: **same inputs always produce the same result**.

The design choice is deliberate.  Deterministic checks are:
- **Reproducible** — any party with the evidence can re-run the check.
- **Trustworthy precisely because they are not an LLM grading itself** — they do not drift with model temperature or prompt wording.
- **Cheap** — no inference cost, no latency, no API key.

This is the methodological counterpart to disinterest in transparency: just as a neutral third-party anchor is more credible than an operator's own log, a deterministic arithmetic check is more credible than asking a model whether the numbers look right.

The capsule makes that deterministic verification **portable and verifiable**: the evidence digest commits to exactly what was checked, and the manifest digest commits to exactly which rules applied.  An auditor can reconstruct the full picture without trusting the operator.

## The in-toto / SLSA pattern

This flow applies the supply-chain integrity pattern to agent actions:

1. **Declare** the checks in `manifest.md` (the policy / ruleset).
2. **Attest** the action: run the checks, seal the capsule with results.
3. **Verify** the evidence against the declaration: re-read the manifest by its pinned digest, confirm the checks listed there appear in `constraints[]`, confirm each passed.

This is the same shape SLSA uses for build provenance — *declare what should run → attest that it ran → verify the evidence*.  The capsule is the attestation; `manifest.md` is the policy.

## Content-private

Only **digests** leave the process:
- `agent_input_digest` / `agent_output_digest` — SHA-256 of the invoice data.
- `evidence_digest` per check — SHA-256 of what each check examined.
- `manifest_ref` — SHA-256 of the ruleset file.
- `capsule_id` — SHA-256 of the capsule envelope.

The raw invoice, line items, vendor name, and amounts stay local.  The anchor receives only the `capsule_id` digest.

## Compose, don't compete

The deterministic checks here are not a replacement for identity, authorization, or a gateway layer.  They run **before** the gate decision and **emit through capsule-emit** — so the full stack is:

```
identity layer (authn)
    ↓
authorization layer (policy / gateway)
    ↓
deterministic checks (this example)
    ↓
capsule-emit (seal + anchor)
    ↓
public transparency log
```

Any gateway (agentgateway, AARM, OPA) can read `manifest.md` and enforce the same constraints at the gateway layer — no code changes required, because the manifest is the shared declaration.

## Schema representation (implementation note)

The per-check results use the existing `ConstraintRecord` (§8.1 of the AAC -02 spec) with its `check_type` field carrying the assurance tier.  No new spec field was needed.

The manifest reference lives in `compute_attestation.manifest_ref` (producer-side context, not a spec-defined binding).  If a future AAC revision adds a dedicated `manifest_ref` envelope field, this example would migrate cleanly — the semantic is already clearly defined here.
