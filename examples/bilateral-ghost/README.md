# Bilateral ghost demo

Three arcs in one runnable file showing the full bilateral attestation spectrum:
authorized, blocked, and ghost.

## Run

```bash
# Offline (no anchor submission):
AAC_ANCHOR_URL=off python examples/bilateral-ghost/demo.py

# Online (anchors to https://anchor.agentactioncapsule.org):
python examples/bilateral-ghost/demo.py
```

After running, verify any arc:

```bash
agent-action-capsule verify --store /tmp/.../arc3_ghost.jsonl
```

## The three arcs

### Arc 1 — Authorized (simple story)

AAuth CAN grant → both orgs seal over a shared action digest → bilateral
commit.

Planner (Org A) gets a cross-org authorization grant (the "may"). Both Planner
and DJ (Org B) independently seal a capsule over `subject_digest =
SHA-256(JCS(action))`. A third party trusting neither org can confirm:
- `disposition.authority` on the Planner's capsule holds the AAuth grant
  reference (opaque identifier only — never the token body)
- Both capsules carry the same `subject_digest` — neither party can swap in a
  different action after the fact

### Arc 2 — Out-of-grant BLOCKED

Budget cap exceeded → gate fires → DJ seals a `blocked` capsule.

The Planner's action total exceeds what the grant authorizes. The DJ's
constraint evaluator fires `budget_cap_eur`, records the gate check result, and
seals a `verdict_class="blocked"` capsule with `effect.status="planned"`.
The block is as verifiable as the execution.

### Arc 3 — GHOST (`countersign_refused`)

Planner seals request → DJ receives, then goes dark → Planner seals the
provable asymmetry.

**A ghost is NOT a both-assert.** The Planner ends up with two capsules:

| # | Who sealed | verdict_class | effect.status | chain |
|---|-----------|--------------|--------------|-------|
| 1 | planner-org | `executed` | `dispatched` | — |
| 2 | planner-org | `countersign_refused` | `planned` | supersedes #1 |

The DJ has **zero** capsules. A verifier sees:
- Planner committed: request was sealed with an AAuth grant reference
- Counterparty countersignature: absent
- The `countersign_refused` capsule chains to the request via
  `chain.relation="supersedes"` — the honest party's two-capsule record is
  self-consistent and independently anchored
- The *missing* countersignature and the *present* ghost are equally provable

**The refusal and the ghost are as provable as the performance.**

## Spec reference

GHOST / `countersign_refused` is the bilateral asymmetry mechanism defined in
`draft-mih-agent-bilateral-attestation-01`. The -01 revision posts on the
IETF datatracker during the Jul 18–24 window.

The companion demo [`../aauth-capsule-interop/`](../aauth-capsule-interop/)
covers the AAuth grant exchange in more detail (real HTTP seam documented).

## What runs live vs. stubbed

| Component | This demo | Live deployment |
|-----------|-----------|----------------|
| AAuth grant | Stub UUID | `jti` from `aa-auth+jwt` at PS token endpoint |
| Action | In-memory dict | Real cross-org request payload |
| Anchor | Optional (default on) | `anchor.agentactioncapsule.org` |
| DJ "going dark" | Simulated (no seal) | Retry window elapsed, no response |

The sealing and verification paths — `bilateral.seal_ghost()`,
`agent_action_capsule.verify()` — run live in all cases.
