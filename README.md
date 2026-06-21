# capsule-emit

**Know what your AI agent did — and let anyone verify it.**

One `emit()` call at each consequential action builds an **anchored, verifiable ledger** of what your agent did — each entry sealed (content-addressed by hash) and checkable by anyone, *without trusting you*.

```python
from capsule_emit import emit

result = {"po_id": "PO-7781"}            # whatever your action returned

cap = emit(
    action="write_order",
    operator="acme-co",                  # the accountable tenant
    developer="po-agent@v1",             # the agent identity + version
    agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
    agent_output=result,
    model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    verdict="executed",                  # executed | confirmed | denied | blocked
    effect={"type": "write_order", "status": "dispatched"},
)
print(cap.capsule_id, cap.anchored)      # sealed; anchor submitted async (offline? emit(..., anchor=False))
```

```bash
pip install capsule-emit
```

`capsule-emit` is the producer layer for the **Agent Action Capsule** — a [SCITT](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/) statement profile. You add one line at the moment your agent does something consequential; you get back a digest-committed, content-addressed capsule — anchored to a public log — that a third party who trusts neither you nor your agent can independently verify.

## Why you need this

Agents now move money, change records, and act across organizational boundaries. When something goes wrong — or someone asks *"did your agent really do that, and was it authorized?"* — what's your proof?

Your **logs** are your own word. They're mutable, they live in your database, and they mean nothing to an auditor, a counterparty, or a regulator who has no reason to trust your systems. There's no way for an outside party to confirm a log wasn't edited after the fact.

A **capsule** is different: its content is committed to a hash the moment the action happens, and that hash is recorded in a public append-only log.\* Anyone can verify it offline, from the bytes alone — *without trusting you*.

> **\* "Public log" ≠ public data.** Only a one-way fingerprint (a SHA-256 digest) and a timestamp are logged — your prompts, payloads, vendors, and amounts never leave your machine. [What's on the log, and what isn't →](docs/the-public-log-explained.md)

## Why your existing stack can't do this

These layers answer **different questions** — a capsule fills the gap of what an agent provably did:

| Layer | Examples | Answers | Doesn't answer |
|---|---|---|---|
| **Identity** | DIDs, SPIFFE, Agent Cards | *Who* is the agent? | What it did |
| **Authorization** | OPA, policy, permits | What is it *may* to do? | What it actually _did_, or the outcome |
| **Observability** | Datadog, audit logs, your DB | What *you say* happened | Nothing to a party who doesn't trust you — mutable, self-attested |
| **Agent Action Capsule** | `capsule-emit` | **What it *did*, provably** | (composes with layers above) |

A capsule records the action **and its outcome**, with a *confirmed-effect binding* so a **dispatched attempt can't be passed off as a completed effect** (the *may/did* distinction: approved ≠ executed ≠ confirmed). It records on **every verdict, including refusals** — a `blocked` capsule is auditor-grade evidence that a gate worked.

## Where you start, and where it goes

**Start here.** Call `emit()` at each consequential action. You get an **anchored, verifiable ledger** of what your agent did — each capsule appended locally to `ledger.jsonl`, its digest written to a public log. That's the whole starting point. Everything below is optional depth you grow into — no rewrite.

**Then climb, one rung at a time:**

- **Capture more, write less** — a framework [adapter](docs/adapters/) (MCP / LangChain / CrewAI / Hermes) emits for every tool call instead of hand-placing `emit()`.
- **Link records into trails** — chain a confirmation capsule to its parent: *approved → executed → confirmed*, human-in-the-loop, and disclosure all ride this. This is where *may/did* becomes a verifiable sequence. → `emit(..., confirms=parent_id)`
- **Declare now, enforce later** — a `manifest.md` declares your rules; a compatible gateway enforces the *same file*, with no change to your `emit()` calls.

The unit is the **capsule** (one action). What you keep and grow is the **ledger** (the anchored trail). Chaining links specific capsules within it. Start with the ledger; add the rest when you need it. → walk it end-to-end in the **[tutorials](docs/tutorials/)**.

## What you get back

`emit()` returns an **`EmitResult`** — `cap.capsule_id`, `cap.anchored`, and `cap.capsule` (the capsule itself, plain JSON you can store or hand to anyone). It carries the `capsule_id` (a SHA-256 content address), the accountable `operator` + `developer`, the **may/did verdict**, the **effect** (and its dispatched-vs-confirmed status), and **digests of your input and output** — your inputs and outputs are committed by hash; **you hold the raw values, the capsule does not** (only their digests).

→ Field-by-field, the two-tier structure, and how each layer is captured: **[docs/anatomy.md](docs/anatomy.md)**.

## Anchoring — where the proof lives

**Anchor is on by default.** On `emit()`, the capsule's **digest only** is submitted — async, non-blocking — to an [RFC 9162](https://www.rfc-editor.org/rfc/rfc9162) SCITT transparency log, so this exact capsule's existence is recorded at that time and independently checkable against the log. (`cap.anchored` reports the submission; surfacing the log's inclusion **receipt** back onto the result is on the near-term roadmap — today the digest is on the log and checkable there.)

- **What's logged:** a SHA-256 digest — nothing else. Your payloads never leave your machine.
- **Where:** the free hosted log at `https://anchor.agentactioncapsule.org/v1/digest` (no signup, no key).
- **Self-host or repoint:** the log service ([`capsule-anchor`](https://github.com/action-state-group/capsule-anchor)) is open-source — `AAC_ANCHOR_URL=…` or `emit(..., anchor_url=…)`.
- **Offline:** `emit(..., anchor=False)` seals locally, skips the network.

*Why bother:* a self-hosted log you control isn't proof to an outsider; a shared, append-only transparency log is. That's what makes the capsule checkable by someone who trusts neither party.

## Verify

The verifier ships in the spec package — check any capsule (or a whole ledger) from the bytes alone, no keys/network/clock:

```bash
pip install agent-action-capsule
agent-action-capsule verify --store ./ledger.jsonl
```

Tamper with one byte and verification fails. The verifier is independent of `capsule-emit` on purpose — *any* tool can produce a capsule; *any* party can verify one. (Every `emit()` also appends to a local JSONL ledger — view it with `capsule-emit ledger view ./ledger.jsonl`.)

## Framework adapters

One `emit()` per tool call, regardless of framework — thin adapters over one shared base:

```python
from capsule_emit.adapters.mcp import MCPCapsuleEmitter
emitter = MCPCapsuleEmitter(operator="acme-co", developer="my-agent@v1")

@emitter.tool("write_order")
def write_order(vendor: str, total: float) -> dict: ...
```

MCP, LangChain, CrewAI, and Hermes are all supported. **Each adapter page has a paste-ready prompt for your coding agent** — drop it into Claude Code (or similar) and it wires emission into your tools for you: **[docs/adapters/](docs/adapters/)**.

## Declare now, enforce later — same file

A `flows/<action>/manifest.md` *declares* autonomy + constraints; `capsule-emit` reads it to **declare** (no enforcement). A compatible gateway reads the **same file** and **enforces** — with **no change** to your `emit()` calls. → [docs/going-deeper.md](docs/going-deeper.md).

## Documentation

New here? Written to be read top-to-bottom, no standards background needed:

- **[Tutorials](docs/tutorials/)** — five-minute, copy-paste sessions: your first capsule → confirming & chaining → reading your ledger → declaring rules.
- **[Concepts in plain words](docs/concepts.md)** — the seven words (capsule, seal, may/did, chain, break, anchor, ledger), each tied to a field or command.
- **[Anatomy of a capsule](docs/anatomy.md)** — exactly what gets sealed, the two-tier structure, how each layer is captured.
- **[Why anchoring makes it trustworthy](docs/why-anchoring.md)** — why a record *you* keep isn't proof to anyone else, and how a shared append-only log fixes it. The heart of it.
- **[The public log, explained](docs/the-public-log-explained.md)** — plain-English + FAQ: the transparency log, how Merkle proofs work, what's visible vs hidden, what you can progressively share. For when someone asks *"you're putting our data on a public log?"*
- **[Adapters](docs/adapters/)** — let MCP / LangChain / CrewAI / Hermes emit capsules for you (paste-to-your-coding-agent prompt on each page).
- **[Going deeper — and popping out](docs/going-deeper.md)** — *down* into the spec + `scitt-cose` substrate to verify it yourself; *up* to a compatible enforcement gateway when you want capsules to **block**, not just record.

## How it fits

```
capsule-emit  →  agent-action-capsule (spec + reference verifier)
                        ↓
                 scitt-cose (COSE_Sign1 + SCITT receipt verification)
```

`capsule-emit` produces; [`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule) is the specification + verifier; [`scitt-cose`](https://github.com/action-state-group/scitt-cose) verifies the transparency-log substrate. Separate on purpose.

## Status

Alpha — API stable, not yet 1.0. The underlying specification is an **individual IETF Internet-Draft**, not an RFC; no RFC number is claimed.

## Provenance, neutrality & governance

Developed by **Action State Group, Inc.** and published as **open-source software (Apache-2.0)**, with a clean transfer path to a **neutral home** (foundation donation or community project) as the ecosystem matures. The content is product-free — the emission layer, adapters, ledger utilities, and a manifest parser; nothing tenant- or product-specific. No primacy is claimed; the value is an interoperable, independently-verifiable record format. Discussion venue: the IETF **SCITT** Working Group (`scitt@ietf.org`).

## License

Apache-2.0 — see [LICENSE](LICENSE).
