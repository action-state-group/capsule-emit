# capsule-emit

**One call to seal a tamper-evident, independently-verifiable record of what your AI agent actually did.**

```python
from capsule_emit import emit

cap = emit(action="write_po", operator="acme-co", developer="po-agent@v1",
           agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
           agent_output=result, verdict="executed",
           effect={"type": "write_po", "status": "dispatched"})

print(cap.capsule_id)   # sealed, anchored, verifiable by anyone
```

`capsule-emit` is the producer layer for the **Agent Action Capsule** — a [SCITT](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/) statement profile. You add one line at the moment your agent does something consequential; you get back a signed, digest-committed capsule that a third party who trusts neither you nor your agent can independently verify.

---

## Why you need this

Agents now move money, change records, and act across organizational boundaries. When something goes wrong — or someone asks "did your agent really do that, and was it authorized?" — what's your proof?

Your **logs** are your own word. They're mutable, they live in your database, and they mean nothing to an auditor, a counterparty, or a regulator who has no reason to trust your systems. There's no way for an outside party to confirm a log wasn't edited after the fact.

A **capsule** is different: it's signed at the moment of the action, its content is committed to a hash, and it's recorded in a public append-only log. Anyone can verify it offline, from the bytes alone — *without trusting you*.

## Why your existing stack can't do this

These layers answer **different questions** — a capsule fills the gap none of them cover:

| Layer | Examples | Answers | Doesn't answer |
|---|---|---|---|
| **Identity** | DIDs, SPIFFE, Agent Cards | *Who* is the agent? | What it did |
| **Authorization** | OPA, policy, permits | What is it *allowed* to do? | What it actually did, or the outcome |
| **Observability** | Datadog, audit logs, your DB | What *you say* happened | Nothing to a party who doesn't trust you — mutable, self-attested |
| **Agent Action Capsule** | `capsule-emit` | **What it *did*, provably** | (composes with the above by reference) |

A capsule records the action **and its outcome**, with a *confirmed-effect binding* so a **dispatched attempt can't be passed off as a completed effect** (the may/did distinction: approved ≠ executed ≠ confirmed). It records on **every verdict, including refusals** — a `blocked` capsule is auditor-grade evidence that a gate worked.

## Concepts

A small vocabulary — each concept maps to a field you can see or a command you can run:

- **Capsule** — the unit: one consequential action *and its outcome*, sealed, signed, and digest-committed. It's plain JSON — inspect it with `cap.capsule` (see *What's in a capsule* below).
- **may / did** — the honesty model: *approved ≠ executed ≠ confirmed*. A capsule carries the verdict (`disposition.verdict_class`) **and** a confirmed-effect binding (`effect.status` + request/response digests), so a *dispatched attempt* can never be presented as a *completed effect*.
- **Chain** — actions link by digest: a confirm / supersede / escalate capsule points at its parent (`chain.parent_capsule_id`), turning *approved → executed → confirmed* (or *deferred → escalated → resolved*) into one verifiable trail. → `emit(..., confirms=parent_id)`
- **Break** — tamper-evidence: change a single byte and the recomputed `capsule_id` stops matching, so `verify` returns **INVALID**. The break *is* the proof — it's what makes the record trustworthy to someone who didn't write it.
- **Anchor** — the public proof: the capsule's digest is written to an append-only transparency log; the receipt proves it existed at time T, checkable by anyone *without trusting you* (see *Anchoring* below).
- **Ledger** — your local append-only trail of capsules (the chain of chains). → `capsule-emit ledger view ./ledger.jsonl`
- **Verify** — anyone, offline, from the bytes alone — no keys, no network, no clock. Class-1 (structure + IDs), Class-2 (manifest-aware). Independent of the producer on purpose (see *Verify* below).
- **Declare → Enforce** — a `manifest.md` *declares* autonomy + constraints; a compatible gateway *enforces* the same file later, with **no change** to your `emit()` calls (see *Declare now, enforce later* below).

The sections below are the depth behind each concept.

## How easy it is

```bash
pip install capsule-emit          # emit + anchor client + ledger CLI
```

```python
from capsule_emit import emit

cap = emit(
    action="write_po",
    operator="acme-co",                 # the accountable tenant
    developer="po-agent@v1",            # the agent identity + version
    agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
    agent_output=result,
    model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    verdict="executed",                 # executed | blocked | denied | errored | timed_out
    effect={"type": "write_po", "status": "dispatched"},
)
print(cap.capsule_id, cap.anchored)
```

That's it. The capsule is sealed and anchored. One call per consequential action.

## What's inside a capsule

`emit()` returns `cap.capsule` — a JSON object you can inspect, store, or hand to anyone:

```jsonc
{
  "spec_version":   "draft-mih-scitt-agent-action-capsule-01",
  "format_version": "2",
  "capsule_id":     "9fddfcec…32eb26",      // SHA-256 of the canonical payload (its content address)
  "action_id":      "write_po/39530d9c…",   // the action name + a unique id (chain linkage)
  "action_type":    "decide",               // the capsule class (a decision that produced an effect)
  "operator":       "acme-co",              // accountable tenant
  "developer":      "po-agent@v1",          // agent identity + version
  "timestamp":      "2026-06-20T04:45:11Z",
  "model_attestation": {                    // which model + the evidence it produced
    "provider": "anthropic",
    "model_id": "claude-sonnet-4-6",
    "compute_attestation": {
      "agent_input_digest":  "3c2c9123…",   // the prompt/input, sealed by hash
      "agent_output_digest": "c574d16d…"    // the inference/output, sealed by hash
    }
  },
  "effect": {                               // what was committed
    "type":   "write_po",
    "status": "dispatched",                 // dispatched (attempted) vs confirmed (observed)
    "effect_attestation": "runtime_claimed"
  },
  "disposition": {                          // the may/did verdict
    "decision":       "accept",
    "verdict_class":  "executed",           // executed | blocked | denied | errored | confirmed
    "approver":       "policy",
    "human_disposed": false                 // honest in-the-loop flag
  },
  "assurance": {                            // how far to trust each part
    "attestation_mode": "self_attested",
    "effect_mode":      "dispatched_unconfirmed",
    "ledger_mode":      "standalone"        // standalone | chained
  }
  // When a capsule confirms/supersedes another, a "chain" block appears and
  // the effect gains a "response_digest" (the confirmed-effect binding):
  //   "chain":  { "parent_capsule_id": "1008e6fc…", "relation": "confirms" }
}
```

Confirming or superseding an action is itself a capsule that **chains** to the first — that's how "approved → executed → confirmed" becomes a verifiable trail. Full field reference: the [spec](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/) §5.

## Anchoring — where the proof lives

**Anchor is on by default.** When you `emit()`, the capsule's **digest only** is submitted to a SCITT transparency log, and you get back an [RFC 9162](https://www.rfc-editor.org/rfc/rfc9162) inclusion-proof **receipt** — durable, tamper-evident evidence that this exact capsule existed at that time.

- **Where:** the free hosted log at `https://anchor.agentactioncapsule.org/v1/digest` (no signup, no key).
- **What's logged:** a **SHA-256 digest** (the `capsule_id`) — nothing else. Your vendors, amounts, operator, and payloads **never leave your machine**.
- **What you get:** the receipt (an inclusion proof) — keep it with the capsule; anyone can later check the capsule against the log offline.
- **Self-host or repoint:** the log service ([`capsule-anchor`](https://github.com/action-state-group/capsule-anchor)) is open-source. Point anywhere with `AAC_ANCHOR_URL=https://your-log/...` or `emit(..., anchor_url=...)`.
- **Offline:** `emit(..., anchor=False)` seals locally and skips the network.
- **Why bother:** a self-hosted log you control isn't proof to an outsider; a shared, append-only transparency log is. That's what makes the capsule checkable by someone who trusts neither party.

## Verify

The verifier ships in the spec package — install it and check any capsule (or a whole ledger) from the bytes alone, no keys/network/clock:

```bash
pip install agent-action-capsule
agent-action-capsule verify ./ledger.jsonl
```

Tamper with one byte and verification fails. The verifier is independent of `capsule-emit` on purpose — *any* tool can produce a capsule; *any* party can verify one.

## Ledger view

Every `emit()` appends to a local JSONL ledger:

```bash
capsule-emit ledger view ./ledger.jsonl
```

## Framework adapters

One `emit()` per tool call, regardless of framework — thin adapters over one shared base:

```python
from capsule_emit.adapters.mcp import MCPCapsuleEmitter          # primary (MCP)
emitter = MCPCapsuleEmitter(operator="acme-co", developer="my-agent@v1")

@emitter.tool("write_po")
def write_po(vendor: str, total: float) -> dict: ...
```

LangChain (`LangChainCapsuleEmitter`, a callback handler), CrewAI (`CrewAICapsuleEmitter`, `.wrap(tool)`), and Hermes (`HermesCapsuleEmitter`, `.after_tool(...)`) work the same way. **Per-adapter guides — where to put the call, the one-line add, and a ready-made prompt for your coding agent — are in [`docs/adapters/`](docs/adapters/).**

## Declare now, enforce later — same file

Drop a `flows/<action>/manifest.md` next to your code to declare autonomy + constraints:

```markdown
---
wicket_id: write-po
autonomy: narrate
---
## Constraints
| id | what it checks | method | severity |
|----|----------------|--------|----------|
| po_arithmetic | Line items re-add to total. | arithmetic_balance | block |
```

`capsule-emit` reads the manifest to **declare** (no enforcement). A compatible gateway reads the **same file** and **enforces** — adding enforcement requires **no changes** to your `emit()` calls or manifests.

## How it fits

```
capsule-emit  →  agent-action-capsule (spec + reference verifier)
                        ↓
                 scitt-cose (COSE_Sign1 + SCITT receipt verification)
```

`capsule-emit` produces; [`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule) is the specification + verifier; [`scitt-cose`](https://github.com/action-state-group/scitt-cose) verifies the transparency-log substrate. Separate on purpose.

## Documentation

New here? These are written to be read top-to-bottom, no standards background needed:

- **[Tutorials](docs/tutorials/)** — five-minute, copy-paste sessions: your first capsule → confirming & chaining → reading your ledger → declaring rules.
- **[Concepts in plain words](docs/concepts.md)** — the seven words (capsule, seal, may/did, chain, break, anchor, ledger), each tied to a field or command.
- **[Anatomy of a capsule](docs/anatomy.md)** — exactly what gets sealed, the two-tier structure, and how each layer is captured.
- **[Adapters](docs/adapters/)** — let MCP / LangChain / CrewAI / Hermes emit capsules for you (with a paste-to-your-coding-agent prompt on each page).
- **[Going deeper — and popping out](docs/going-deeper.md)** — *down* into the spec + `scitt-cose` substrate if you want to verify it yourself; *up* to a compatible enforcement gateway (e.g. `gopher-ai`, OSS) when you want capsules to **block**, not just record.

## Status

Alpha — API stable, not yet 1.0. The underlying specification is an **individual IETF Internet-Draft**, not an RFC; no RFC number is claimed.

## Provenance, neutrality & governance

Developed by **Action State Group, Inc.** and published as **open-source software (Apache-2.0)**, with a clean transfer path to a **neutral home** (foundation donation or community project) as the ecosystem matures. The content is product-free — the emission layer, adapters, ledger utilities, and a manifest parser; nothing tenant- or product-specific. No primacy is claimed; the value is an interoperable, independently-verifiable record format. Discussion venue: the IETF **SCITT** Working Group (`scitt@ietf.org`).

## License

Apache-2.0 — see [LICENSE](LICENSE).
