# Anatomy of a capsule — what gets sealed

What `emit()` hands you (`cap.capsule`) is a **content-addressed statement** — one JSON
object describing one agent action. Its tamper-evidence isn't a signature; it's the
**`capsule_id`** = the SHA-256 of the canonical capsule. Recompute it and it must match,
byte for byte. That hash *is* the seal.

```
cap.capsule  — a content-addressed JSON statement
  capsule_id ………… SHA-256 of everything below — the seal / content address
  what/who/when …… action_type, operator, developer, timestamp
  disposition …… the may/did verdict
  effect ………… what was committed (+ confirmed-effect binding)
  assurance …… how far to trust it
  model_attestation … the model + DIGESTS of input/output (raw values are NOT here)
  chain ………… links to other capsules (only when confirming/superseding)
```

**Where "signed" and "receipt" come in (and why they're not in `cap.capsule`).** The
Agent Action Capsule *spec* defines a richer wrapping — the capsule as a COSE_Sign1
**Signed Statement** (a signature) carrying an RFC 9162 transparency **receipt**.
`capsule-emit`'s default `emit()` produces the **statement above** and **anchors its
digest**; the **receipt comes back from the anchor** (you keep it next to the capsule —
it is *not* a field inside `cap.capsule`), and a producer **signature** is the SCITT
Signed-Statement tier (the verifier's `--transparent` path), a step up from the default.
So, concretely:

- **Tamper-evidence** is always there — `capsule_id` (recompute and compare).
- **Existence proof** comes from anchoring — the **receipt**, held *beside* the capsule.
- **A producer signature** binding to a key is the Signed-Statement tier — not part of
  the default `cap.capsule`.

The upshot: the capsule is **content-private by construction** — it carries *digests*
of your inputs/outputs, never the raw values (see the layers below). You can hand
someone `cap.capsule`, and they learn what happened and can verify the seal, without
seeing your prompts, vendors, or amounts.

---

## The fields

| Field | Meaning |
|---|---|
| `capsule_id` | SHA-256 of the canonical capsule — the seal / content address |
| `spec_version` / `format_version` | which profile + capsule format this is |
| `action_id` | the action name + a unique id (e.g. `write_order/39530d9c…`) — chain linkage |
| `action_type` | the capsule class (`decide` — a decision that produced an effect) |
| `operator` / `developer` | accountable tenant + agent identity@version |
| `timestamp` | when it happened |
| `disposition` | the **may/did** verdict (`decision`, `verdict_class`, `approver`, `human_disposed`) |
| `effect` | what was committed (`type`, `status`) + the **confirmed-effect binding** (`response_digest` appears on confirm) |
| `assurance` | how far to trust it: `attestation_mode`, `effect_mode`, `ledger_mode` |
| `model_attestation` | which model decided + the **layers** (evidence digests), each **digest-committed** (below) |
| `chain` | `parent_capsule_id` + `relation` — present only when this capsule confirms / supersedes another |

### The layers — the evidence inside

These are what people mean by "seal more into the capsule." Each is **hashed, and only
the digest is committed — the raw value is *not* stored in the capsule.** You hold the
inputs/outputs yourself; the capsule proves *what* they were (reveal a value later and
anyone can re-hash it and check it against the digest) without ever containing them.

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
    action="write_order",
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
    verdict="executed",                         # executed | confirmed | denied | blocked
    effect={"type": "write_order", "status": "dispatched"},
)
```

Everything above is digest-committed and sealed in one call. Add a layer by
passing the argument; omit it and it simply isn't part of the seal.

> **The four verdicts `executed | confirmed | denied | blocked` are load-bearing.** Any
> other string is accepted but classes the capsule as `fyi` instead of `decide` — use one
> of the four for a decision capsule.

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

A confirmation is **itself a capsule** that points at its parent by digest. `emit(..., confirms=…)` writes a chain with `relation="confirms"` by default;
pass `relation="supersedes"` or `relation="escalates"` to use the other spec relations.

```python
done = emit(action="write_order", operator="acme-co", developer="po-agent@v1",
            verdict="executed",
            effect={"type": "write_order", "status": "confirmed"},
            confirms=cap.capsule_id)          # ← chain.parent_capsule_id
```

That turns *approved → executed → confirmed* (or *deferred → escalated → resolved*)
into one verifiable trail — the basis for **human-in-the-loop confirmation**. It's also
the basis for **disclosure**: each capsule is already digest-only, so showing someone a
capsule (or a chain of them) proves *what happened and that it was authorized* without
exposing your inputs/outputs — and you reveal a raw value only when you choose, for them
to re-hash against the committed digest. (Revealing *individual fields* — the amount but
not the vendor — needs per-field salted commitments, which is the
[selective-disclosure companion spec](going-deeper.md), not the default producer.)

## Showing & sharing it

A capsule is plain JSON — `cap.capsule` — so you can store it, attach it, or hand
it over directly, and anyone can `agent-action-capsule verify` it from the bytes.
For a **human-readable view and controlled sharing** (rendering a chain, revealing
chosen capsules to another party), a [compatible open-source gateway](going-deeper.md)
reads these same capsules. The format is the contract; the viewer is optional.

---

*Field-level reference: the [spec](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/) §5.*
