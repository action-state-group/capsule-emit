# capsule-emit

[![CI](https://github.com/action-state-group/capsule-emit/actions/workflows/python.yml/badge.svg)](https://github.com/action-state-group/capsule-emit/actions/workflows/python.yml)

> **New here? → [docs/start-here.md](docs/start-here.md)** — the one-page front door.

**Know what your AI agent did — and let anyone verify it.**

**capsule-emit records your agent's actions as verifiable capsules; `seal()` is the one call you make.**

One `seal()` call at each consequential action builds a **witnessed, verifiable ledger** of what your agent did — each entry sealed (content-addressed by hash) and checkable by anyone, *without trusting you*.

```python
from capsule_emit import seal

result = {"po_id": "PO-7781"}            # whatever your action returned

capsule = seal(
    {"vendor": "Frobozz Supply", "total": "1240.19"},   # payload: any JSON-serializable value (quantities are strings, not floats)
    action="write_order",
    operator="acme-co",                  # the accountable tenant
    developer="po-agent@v1",             # the agent identity + version
    agent_output=result,
    model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    verdict="executed",                  # executed | confirmed | denied | blocked
    effect={"type": "write_order", "status": "dispatched"},
)
print(capsule.capsule_id, capsule.signature)   # sealed, signed, witnessed by default; anchor is a legacy opt-in (seal(payload, anchor=True))
```

```bash
pip install capsule-emit
```

`capsule-emit` is the producer layer for the **Agent Action Capsule** — a [SCITT](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/) statement profile. You add one line at the moment your agent does something consequential; you get back a digest-committed, content-addressed capsule — witnessed by a public log — that a third party who trusts neither you nor your agent can independently verify.

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

**Start here.** Call `seal()` at each consequential action. You get a **witnessed, verifiable ledger** of what your agent did — each capsule appended locally to `ledger.jsonl`, its digest written to a public log. That's the whole starting point. Everything below is optional depth you grow into — no rewrite.

**The verb surface.** One authorship axis, one thing they all return (a `Capsule`, appended to the log) — which one you call just says who authored the content:

| Verb | Use it when | |
|---|---|---|
| **`seal(payload)`** | You authored this content — the common case | *mint* |
| **`received(bytes, type=...)`** | Someone else already signed it; you're bringing it into your log as-transmitted, under its own declared type | *carry* |
| **`seal(who(...), can(...), did(...), audit(...))`** | Bind several members into one capsule — the composition asserts nothing new, it references each slot member | *compose* |
| **`push()`** | Force a checkpoint now, instead of waiting for the cadence | *checkpoint* |

`seal(received(bytes, type="machine-mandate"))` and `received(bytes, type="machine-mandate")` produce the identical capsule — nest a carry inside `seal()`, or call it standalone; `seal()` never re-signs an already-carried capsule, and never accepts raw bytes directly (that ambiguity — yours or theirs? — is always refused, not guessed). Composition is nesting the slot verbs `who`/`can`/`did`/`audit` inside `seal()`; there is no separate `compose()`/`carry()` call.

**Then climb, one rung at a time:**

- **Capture more, write less** — a decorator [adapter](docs/adapters/) (MCP / LangChain / CrewAI / Hermes / Goose / ADK) seals each wrapped tool call automatically; the [agentgateway](docs/adapters/agentgateway.md) adapter seals all consequential traffic at the gateway chokepoint — no per-tool changes needed.
- **Link records into trails** — chain a confirmation capsule to its parent: *approved → executed → confirmed*, human-in-the-loop, and disclosure all ride this. This is where *may/did* becomes a verifiable sequence. → `seal(payload, confirms=parent_id)` · [within one stream, and across (under revision)](docs/chaining.md)
- **Declare now, enforce later** — a `manifest.md` declares your rules; a compatible gateway enforces the *same file*, with no change to your `seal()` calls.

The unit is the **capsule** (one action). What you keep and grow is the **ledger** (the witnessed trail). Chaining links specific capsules within it. Start with the ledger; add the rest when you need it. → walk it end-to-end in the **[tutorials](docs/tutorials/)**.

## What you get back

`seal()` returns an **`EmitResult`** — `cap.capsule_id`, `cap.anchored`, `cap.signature`, `cap.key_id`, `cap.seq`, and `cap.capsule` (the capsule itself, plain JSON you can store or hand to anyone). It carries the `capsule_id` (a SHA-256 content address), a **self-attested `signature`** over that content by a persisted producer key (verify it straight from the capsule via `verify_capsule_signature` — see `capsule_emit.signing`), the accountable `operator` + `developer`, the **may/did verdict**, the **effect** (and its dispatched-vs-confirmed status), and **digests of your input and output** — your inputs and outputs are committed by hash; **you hold the raw values, the capsule does not** (only their digests). `cap.seq` is its position in your log — already a leaf, ambiently, before any checkpoint — and both `repr(cap)` and `capsule-emit ledger show` render it as `#logged @ leaf <seq>`.

→ Field-by-field, the two-tier structure, and how each layer is captured: **[docs/anatomy.md](docs/anatomy.md)**.

## Anchoring — where the proof lives

**Anchor is a legacy, non-default channel as of 0.5.0.** The checkpoint/witness
stream below is the only default egress path; this per-capsule anchor exists
only as an explicit opt-in (`anchor=True` or
`CAPSULE_ANCHOR=legacy-on`), kept for one release as a rollback path. When
engaged, the capsule's **digest only** is submitted — async, non-blocking —
to an [RFC 9162](https://www.rfc-editor.org/rfc/rfc9162) SCITT transparency
log, so this exact capsule's existence is recorded at that time and checkable
against the log by a party who trusts neither you nor your runtime.
(`cap.anchored` reports the submission; surfacing the log's inclusion
**receipt** back onto the result is on the near-term roadmap — today the
digest is on the log and checkable there.) That's **self-attested**, not yet
**witnessed** — a single per-capsule inclusion receipt is a narrower claim
than the witnessed stream below; see [why anchoring makes it trustworthy](docs/why-anchoring.md)
for the honest ladder.

- **What's logged:** a SHA-256 digest — nothing else. Your payloads never leave your machine.
- **Where:** the free hosted log at `https://anchor.agentactioncapsule.org/v1/digest` (no signup, no key) — a single-operator log.
- **Self-host or repoint:** the log service ([`capsule-anchor`](https://github.com/action-state-group/capsule-anchor)) is open-source — `AAC_ANCHOR_URL=…` or `seal(payload, anchor_url=…)`.
- **Off by default; explicitly off:** leave `anchor` unset (the default), or pass `seal(payload, anchor=False)`.
- **First-run notice:** before this process's first anchor *or* witness network attempt, one line prints to stderr — naming the active endpoint(s) and how to turn each off — so an active network path is never silent on the very first call.

*Why bother:* a self-hosted log you control isn't proof to an outsider; a shared, append-only transparency log is checkable by someone who trusts neither you nor the log's contents (though not, without a witness, someone unwilling to trust the log's operator at all — that step is self-attested vs. witnessed, not shared vs. unshared).

## Checkpoint — your log now proves itself

Until 0.5.0, your capsule log was like a git repo you never pushed: internally consistent — every capsule content-addressed, every entry chained to the one before it — but nothing *outside* your machine vouches for it. An unpushed commit can be quietly rewritten; a pushed one can't.

**0.5.0 pushes.** Anchoring (above) is per-**capsule**; checkpointing is per-**stream**. `seal()` also, **by default**, folds every capsule into a per-ledger [Merkle Mountain Range](docs/checkpoint.md) and — every ~100 records (`capsule_emit.witness.DEFAULT_CADENCE_ENTRIES`) — builds and registers a signed **checkpoint**: a summary of the whole stream so far (its size, a root hash, a timestamp), sent to an independent witness's `/checkpoints` route so it can verify the checkpoint's own signature before counter-signing. Async, same as the anchor; your payloads never leave.

- **Off is one flag, honored everywhere:** `seal(payload, witness=False)` for one call, `CAPSULE_WITNESS=off` for every call — no code change, no opt-in required in the first place.
- **Your log is still your file.** The witness only ever sees the checkpoint (never your capsule content, never a per-record digest); walking away from it loses no history — `ledger.jsonl` is complete on its own, the witness just lets someone else confirm you didn't rewrite it after the fact.
- **Force a checkpoint on demand.** `push()` builds and registers a checkpoint right now, without waiting for the cadence — useful before a process exits or at a natural audit boundary.
- **Any witness works, and more than one is stronger.** The default is a free hosted tier at `witness.agentactioncapsule.org` (a separate, live witness service, `POST /checkpoints`) — but any conforming Transparency Service is substitutable (`CAPSULE_WITNESS_URL` / `seal(payload, witness_url=...)`), and you can register with several at once (a list, or comma-separated) for a stronger, equivocation-resistant tier. The first checkpoint of a process prints one line to stderr — once — naming exactly what's sent, where, and how to turn it off.

See **[`capsule_emit.checkpoint`](docs/checkpoint.md)** for the cadence, the multi-witness config, and precisely what trust tier a checkpoint does (and doesn't) reach — a single witness upgrades you from *self-attested*, but it isn't the *multi-witness, equivocation-resistant* tier, and a witness never vouches that your capsules' content is true, only that they exist, are ordered, and weren't deleted.

## Verify

The verifier ships in the spec package — check any capsule (or a whole ledger) from the bytes alone, no keys/network/clock:

```bash
pip install agent-action-capsule
agent-action-capsule verify --store ./ledger.jsonl
```

Tamper with one byte and verification fails. The verifier is independent of `capsule-emit` on purpose — *any* tool can produce a capsule; *any* party can verify one. (Every `seal()` also appends to a local JSONL ledger — view it with `capsule-emit ledger view ./ledger.jsonl`.)

## Framework adapters

One `seal()` per tool call, regardless of framework — thin adapters over one shared base:

```python
from capsule_emit.adapters.mcp import MCPCapsuleEmitter
emitter = MCPCapsuleEmitter(operator="acme-co", developer="my-agent@v1")

@emitter.tool("write_order")
def write_order(vendor: str, total: float) -> dict: ...
```

| Adapter | What it wraps |
|---------|---------------|
| **MCP** | Model Context Protocol tool endpoints — any Python callable, decorator-based |
| **Google ADK** | Google Agent Development Kit tool calls, one capsule per completed tool invocation |
| **agentgateway** | Rust proxy for MCP/A2A/LLM traffic; seals all `tools/call` at the gateway chokepoint |
| **LangChain** | LangChain callback handler; fires on `on_tool_start`/`on_tool_end` automatically |
| **CrewAI** | Wraps a CrewAI tool object; emits one capsule per call, input and output captured |
| **Goose** | Block's open-source AI agent; Goose tools are MCP tools, so the MCP adapter applies |
| **Hermes** | Custom agent loops; call `after_tool(...)` explicitly after any tool finishes |
| **Dapr** | Dapr actor and service invocation; wraps Dapr tool calls as capsule-emitting steps |

**Each adapter page has a paste-ready prompt for a coding agent** to wire emission into your
tools: **[docs/adapters/](docs/adapters/)**.
## Declare now, enforce later — same file

A `flows/<action>/manifest.md` *declares* autonomy + constraints; `capsule-emit` reads it to **declare** (no enforcement). A compatible gateway reads the **same file** and **enforces** — with **no change** to your `seal()` calls. → [docs/going-deeper.md](docs/going-deeper.md).

## Documentation

New here? Written to be read top-to-bottom, no standards background needed:

- **[Tutorials](docs/tutorials/)** — five-minute, copy-paste sessions: your first capsule → confirming & chaining → reading your ledger → declaring rules.
- **[Concepts in plain words](docs/concepts.md)** — the seven words (capsule, seal, may/did, chain, break, witness, ledger), each tied to a field or command.
- **[Anatomy of a capsule](docs/anatomy.md)** — exactly what gets sealed, the two-tier structure, how each layer is captured.
- **[Chaining — within one agent, and across agents](docs/chaining.md)** — capsules link by content address into verifiable trails, including **cross-organizational** chains; why the ledger is a DAG, not one line.
- **[Why anchoring makes it trustworthy](docs/why-anchoring.md)** — why a record *you* keep isn't proof to anyone else, and how a shared append-only log fixes it. The heart of it.
- **[The public log, explained](docs/the-public-log-explained.md)** — plain-English + FAQ: the transparency log, how Merkle proofs work, what's visible vs hidden, what you can progressively share. For when someone asks *"you're putting our data on a public log?"*
- **[Adapters](docs/adapters/)** — decorator adapters (MCP / LangChain / CrewAI / Hermes / [Goose](docs/adapters/goose.md) / [ADK](docs/adapters/adk.md)) seal each wrapped tool call; [agentgateway](docs/adapters/agentgateway.md) seals all `tools/call` traffic at the gateway layer. Paste-to-your-coding-agent prompt on each page.
- **[Going deeper — and popping out](docs/going-deeper.md)** — *down* into the spec + `scitt-cose` substrate to verify it yourself; *up* to a compatible enforcement gateway when you want capsules to **block**, not just record.
- **[`capsule_emit.checkpoint`](docs/checkpoint.md)** — the CLL (Checkpointed Local Log) core: an MMR index over your own ledger plus signed, TS-registrable peaks checkpoints. Wired in **by default** since 0.5.0 (lazy — zero cost until a ledger is actually checkpoint-worthy); the primitives are also directly usable for your own cadence/keys/TS.

## How it fits

```
capsule-emit  →  agent-action-capsule (spec + reference verifier)
                        ↓
                 scitt-cose (COSE_Sign1 + SCITT receipt verification)
```

`capsule-emit` produces; [`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule) is the specification + verifier; [`scitt-cose`](https://github.com/action-state-group/scitt-cose) verifies the transparency-log substrate. Separate on purpose.

## Status

Alpha — API stable, not yet 1.0. The underlying specification is an **individual IETF Internet-Draft**, not an RFC; no RFC number is claimed.

**Conformance & spec tracking.** Every capsule stamps its `spec_version` + `format_version`, and `capsule-emit` produces capsules conforming to the current draft (`draft-mih-scitt-agent-action-capsule`) — proven by the *independent* [`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule) verifier and its frozen conformance vectors, not by self-assertion. Two format versions exist and both keep verifying:

- **`seal()` / `carry()` / `received()` / `compose()`** — the developer surface above — produce **format `4`**, canonicalized per RFC 8785 JCS (`canonicalization_id="jcs"`).
- **`holds/` (reserve/release/expire/reconcile lifecycle capsules)** — a separate, vintage code path — still produces **format `2`** (`canonicalization_id="jcs-n"`, the absent-field-normalized profile). It is a deliberate exception, not drift: hold-lifecycle capsules were minted under the older profile and stay there rather than silently reformatting existing records.

When the spec revises, the version bumps and older capsules keep verifying; that's how this implementation stays tracked to the standard.

## Provenance, neutrality & governance

Developed by **Action State Group, Inc.** and published as **open-source software (Apache-2.0)**, with a clean transfer path to a **neutral home** (foundation donation or community project) as the ecosystem matures. The content is product-free — the emission layer, adapters, ledger utilities, and a manifest parser; nothing tenant- or product-specific. No primacy is claimed; the value is an interoperable, independently-verifiable record format. Discussion venue: the IETF **SCITT** Working Group (`scitt@ietf.org`).

## License

Apache-2.0 — see [LICENSE](LICENSE).

**Patent posture:** All six provisional patent applications related to this specification were expressly abandoned on July 6, 2026. No license is required. See [agentactioncapsule.org/ip](https://agentactioncapsule.org/ip) for details.
