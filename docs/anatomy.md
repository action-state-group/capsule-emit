# Anatomy of a capsule — what gets sealed

A capsule has **two tiers**. Keeping them separate is the whole trick: the
**outer envelope** is what a stranger verifies *without seeing your business*, and
the **inner payload** is what *you* sealed inside it. Outsiders check the seal;
only the holder (or whoever you disclose to) reads the contents.

```
┌─ OUTER ENVELOPE ─────────────────────────────────────────────┐
│  SCITT signed statement (COSE_Sign1)                          │
│   • signature over the payload          ← proves who sealed it │
│   • content_type, capsule_id (SHA-256)  ← the content address  │
│   • RFC 9162 transparency receipt       ← proves it existed @ T │
│                                                               │
│  ┌─ INNER PAYLOAD (the agent action) ───────────────────────┐ │
│  │  what / who / when / disposition / effect / constraints   │ │
│  │  chain  ……………………………………… links to other capsules         │ │
│  │  layers (each digest-committed):                          │ │
│  │     agent_input  ·  agent_output  ·  model  ·  compute     │ │
│  └──────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
```

**Why two tiers.** A verifier checks the outer envelope from the bytes alone — no
keys, no network — and learns *that* a specific payload was sealed and anchored at
time T, **without the payload being exposed**. The inner payload travels with the
capsule but is committed by digest, so you can hand someone the proof and reveal
the contents *only when you choose to* (see [chaining & disclosure](#chaining--disclosure)).

---

## Tier 1 — the outer envelope (what strangers verify)

| Field | Meaning | Who reads it |
|---|---|---|
| `signature` | COSE_Sign1 signature over the inner payload | anyone — proves the sealer |
| `content_type` | declares the payload profile | verifier |
| `capsule_id` | SHA-256 of the canonical inner payload (its content address) | anyone — the break-point |
| `receipt` | RFC 9162 inclusion proof from the transparency log | anyone — proves existence at time T |

The envelope is **content-free**: it commits to the payload by hash, so it can be
published, anchored, and checked by a party who never sees the business data.

## Tier 2 — the inner payload (the agent action)

| Field | Meaning |
|---|---|
| `action_id` | the action name + a unique id (e.g. `write_po/39530d9c…`) — chain linkage |
| `action_type` | the capsule class (`decide` — a decision that produced an effect) |
| `operator` / `developer` | accountable tenant + agent identity@version |
| `timestamp` | when it happened |
| `disposition` | the **may/did** verdict (`decision`, `verdict_class`, `approver`, `human_disposed`) |
| `effect` | what was committed (`type`, `status`) + the **confirmed-effect binding** (`response_digest` appears on confirm) |
| `assurance` | how far to trust it: `attestation_mode`, `effect_mode`, `ledger_mode` |
| `model_attestation` | which model decided + the **layers** (evidence digests), each **digest-committed** (below) |
| `chain` | `parent_capsule_id` + `relation` — present only when this capsule confirms / supersedes another |

### The layers — the evidence inside

These are what people mean by "seal more into the capsule." Each is hashed and the
digest is committed; the raw value rides inside the payload (and can be withheld
on disclosure).

| Layer | `emit()` argument | What it captures | Committed as |
|---|---|---|---|
| **Prompt / input** | `agent_input=` | what went *into* the agent for this action | `model_attestation.compute_attestation.agent_input_digest` |
| **Inference / output** | `agent_output=` | what the agent *produced* | `model_attestation.compute_attestation.agent_output_digest` |
| **Model** | `model={"provider","model_id",…}` | which model decided | `model_attestation.{provider, model_id}` |
| **Compute / hardware** | `model={…,"endpoint","chip"}` extra keys | where inference ran (best-effort) | `model_attestation.compute_attestation.*` |

(The digests live *inside* `model_attestation.compute_attestation` — one place holds
the model and the evidence it produced.)

---

## What gets sealed — a fully-loaded `emit()`

```python
from capsule_emit import emit

cap = emit(
    action="write_po",
    operator="acme-co",                       # accountable tenant
    developer="po-agent@v1",                   # agent identity + version

    # ── layers: the evidence ──────────────────────────────
    agent_input={"vendor": "Frobozz Supply",   # the prompt/input  → agent_input_digest
                 "total": 1240.19,
                 "po_lines": [...]},
    agent_output=result,                        # the inference     → agent_output_digest
    model={"provider": "anthropic",             # the model         → ModelAttestation
           "model_id": "claude-sonnet-4-6",
           "endpoint": "https://…/v1",          # the compute       → compute_attestation
           "chip": "NVIDIA-NIM-routed"},        #   (best-effort)

    # ── disposition + effect: the may/did ─────────────────
    verdict="executed",                         # executed|blocked|denied|errored|timed_out
    effect={"type": "write_po", "status": "dispatched"},
)
```

Everything above is digest-committed and sealed in one call. Add a layer by
passing the argument; omit it and it simply isn't part of the seal.

## How layers are captured — automatic vs. explicit

The **hashing is always automatic** — whatever reaches `emit()` is canonicalized
and digest-committed. What varies is *how the value gets to `emit()`*:

| Layer | Bare `emit()` | Via an adapter |
|---|---|---|
| `agent_input` | you pass it | **auto** — MCP `@emitter.tool` digests the call args; LangChain/CrewAI capture tool input |
| `agent_output` | you pass it | **auto** — the adapter digests the return value |
| `model` | you pass it | **auto where the framework exposes it** (e.g. LangChain `on_llm_start` → `model_id`); explicit otherwise |
| `compute` (`endpoint`/`chip`) | you pass it | **best-effort / explicit** — never GPU-detected; pass what your inference route reports |

So with the adapters, input/output capture themselves; **model auto-fills when the
framework hands it to us**, and falls back to explicit when it doesn't; hardware is
always whatever you can honestly report from your inference route (which is why the
Hermes demo records `"NVIDIA-NIM-routed"` rather than pretending to detect silicon).

> **Honesty rule:** never seal a layer you can't stand behind. An absent
> `compute_attestation` is fine; a *fabricated* one defeats the point — the
> capsule's value is that every sealed field is true.

---

## Chaining & disclosure

A confirm / supersede / escalate is **itself a capsule** that points at its parent
by digest:

```python
done = emit(action="write_po", operator="acme-co", developer="po-agent@v1",
            verdict="executed",
            effect={"type": "write_po", "status": "confirmed"},
            confirms=cap.capsule_id)          # ← chain.parent_capsule_id
```

That turns *approved → executed → confirmed* (or *deferred → escalated → resolved*)
into one verifiable trail — the basis for both **human-in-the-loop confirmation**
and **selective disclosure**: because each tier commits by digest, you can reveal
the envelope + chain (the *proof it happened and was authorized*) while withholding
the inner payload layers, or disclose specific layers to specific parties.

## Showing & sharing it

A capsule is plain JSON — `cap.capsule` — so you can store it, attach it, or hand
it over directly, and anyone can `agent-action-capsule verify` it from the bytes.
For a **human-readable view and controlled sharing** (rendering a chain, revealing
selected layers to another party), a compatible open-source gateway — e.g.
[`gopher-ai`](going-deeper.md) — reads these same capsules for a human-readable view
and controlled sharing. The format is the contract; the viewer is optional.

---

*Field-level reference: the [spec](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/) §5.*
