<!-- SPDX-License-Identifier: Apache-2.0 -->
# AAuth → Capsule bilateral interop

**Planner Agent (Org A) ↔ DJ Agent (Org B).** Two agents with distinct
organizational identities take one cross-org action; **both** seal a capsule
over the **same** action, and a third party who trusts *neither* org can verify
what happened end-to-end.

This is a neutral compose example. It does not modify AAuth and it does not
depend on any particular anchor operator.

## The story: AAuth is the "may", the capsule is the "did"

1. **AAuth = the "may."** A cross-org authorization grant lets Org A's planner
   agent ask Org B's DJ agent to act. The grant is captured only as an **opaque
   reference** on the record — `disposition.authority` carries the grant's `jti`
   (identifier), never the token body.
2. **Bilateral seal = the "did", both directions.** Both agents independently
   seal a capsule over the **same shared action digest**
   `subject_digest = SHA-256(JCS(action))`. Each capsule is bound to that one
   action and signed by its own org, so the two records are joined by the digest
   without either side being able to rewrite the other's.
3. **Anchor → verify.** Each capsule is anchored (digest-only) to a transparency
   log, then `agent-action-capsule verify` confirms inclusion. A relying party
   that trusts neither the planner nor the DJ can check the shared action
   end-to-end.

## Run

```bash
pip install "capsule-emit" "agent-action-capsule"

# Online — anchors digests to the default log (anchor.agentactioncapsule.org):
python examples/aauth-capsule-interop/demo.py

# Offline — no anchor submission:
AAC_ANCHOR_URL=off python examples/aauth-capsule-interop/demo.py
```

Then verify the sealed capsules from the ledger:

```bash
agent-action-capsule verify --store /tmp/aauth_capsule_interop_ledger.jsonl
```

Tests:

```bash
python -m pytest examples/aauth-capsule-interop/test_bilateral.py -q
```

## What runs live vs. stubbed

| Piece | Status |
|-------|--------|
| Bilateral seal over the shared `subject_digest` (both orgs) | **live** — real capsules produced by `capsule-emit` |
| Anchor (digest-only) + `agent-action-capsule verify` | **live** (online mode; skipped with `AAC_ANCHOR_URL=off`) |
| The AAuth authorization grant | **stubbed** — a clearly-labeled placeholder `jti` stands in at the exact seam where a real `aa-auth+jwt` grant id would flow |

**Where the real AAuth token id binds in:** in a full deployment the planner
agent receives an `aa-auth+jwt` grant from the Person Server (and, across a
multi-hop SCA→MAA exchange, an `act`-chained token). The grant's `jti` is what
`seal_planner()` records in `disposition.authority`. This demo substitutes a
placeholder `jti` at that one point; every other step is real. Nothing here
claims the AAuth handshake itself ran.

## Reputation leg

Omitted — not required for the compose story. This demo focuses on
authorize → both-seal → anchor → verify.

## Scope

Neutral mechanism only: an authorization grant reference, two capsules over a
shared action digest, and transparency-log verification with generic
roots/issuers. It does not nominate any particular identity root, anchor
operator, or scoring service.

## Out-of-grant enforcement

A grant without enforcement is a suggestion. This demo also shows the case
where the DJ agent's **wicket gate** runs a constraint derived directly from
the grant terms:

| Grant term | Action value | Result |
|---|---|---|
| `max_budget_eur: 500` | `total_eur: 1 200` | BLOCKED — capsule sealed |

When `total_eur` exceeds `max_budget_eur`, `run_gate` fails: the DJ agent seals
a **blocked** capsule (`verdict="blocked"`, `effect.status="planned"`) carrying
the gate check results and the same `disposition.authority` (grant JTI) as
the within-grant case. Both the executed record and the blocked record are
independently anchored and verifiable — the refusal is as provable as the
performance.
