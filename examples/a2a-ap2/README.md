# A2A callee-seals capsule — AP2 payment example

Demonstrates how an A2A callee seals a neutral capsule on every payment action using
[Agent Action Capsule](https://agentactioncapsule.org) +
[capsule-emit](https://pypi.org/project/capsule-emit/).

## Compose framing

AP2 (A2A Payment Profile) and AAC do not overlap — they compose:

| Layer | Protocol | What it proves |
|-------|----------|----------------|
| Authorization | AP2 CartMandate | Caller permitted this payment |
| Neutral record | AAC capsule | Callee sealed what actually happened |
| Independent proof | SCITT receipt | Third-party log the capsule was registered |

The capsule is the bridge: `agent_input_digest` binds the mandate ("may"),
`response_digest` binds the outcome ("did"). Neither carries raw content — only
digests leave the process.

## Two scenarios

**Scenario A — approved payment:**
1. Callee receives A2A Task with AP2 CartMandate (mandate within limit)
2. Seals a `dispatched` capsule immediately (before payment completes)
3. Executes the payment (Stripe or sandbox)
4. Seals a `confirmed` capsule chained to the dispatched one
5. Both capsules anchored to the live SCITT transparency log

**Scenario B — refusal:**
1. Callee receives A2A Task with AP2 CartMandate (mandate over agent spend limit)
2. Seals a `planned` capsule — `effect.status: "planned"` is the invariant that the
   payment was **never dispatched**
3. Payment never reaches Stripe
4. Refusal capsule anchored — the verifiable record that this was attempted and blocked

## Quick start

```bash
pip install "capsule-emit"
cd examples/a2a-ap2
python run_example.py
```

No API keys needed — runs in sandbox mode by default (`DRY_RUN=1`).

Real Stripe:
```bash
STRIPE_API_KEY=sk_test_... DRY_RUN=0 python run_example.py
```

## Capsule → AP2 field mapping

```
A2A Task + AP2 CartMandate
  └── task_id, session_id, mandate.*
        │
        ▼ digest
  capsule.agent_input_digest          ← "may" (the authorization)

Payment outcome (PaymentResult)
  └── payment_id, amount, payee, status
        │
        ▼ digest
  capsule.effect.response_digest      ← "did" (the outcome)
  capsule.effect.type = "send_payment"
  capsule.effect.status = "confirmed" | "dispatched" | "planned"

A2A Task state
  completed  →  verdict_class: "executed"
  failed     →  verdict_class: "blocked"
  (over limit) → verdict_class: "blocked", effect.status: "planned"
```

## Verify

```bash
# Verify the ledger from a run
agent-action-capsule verify --store /path/to/a2a_ap2_ledger.jsonl

# Verify a single capsule_id against the live anchor
curl https://anchor.agentactioncapsule.org/v1/inclusion/<capsule_id>
```

## Files

- `run_example.py` — runnable demo (3 capsules: dispatch + confirm + refusal)
- `a2a_sandbox.py` — minimal A2A Task + AP2 CartMandate data structures + payment sandbox

## Not included (by design)

- No standalone A2A adapter (agentgateway proxies A2A at the wire)
- No AP2 adapter (map to AP2, don't be AP2)
- No Authority/moat code (that lives in private repos)

## Boundary

This example is public-safe and content-private: only digests of the mandate and
payment outcome travel in the capsule. The raw AP2 mandate and payment details never
leave the process.
