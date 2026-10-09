# capsule-emit

Language projects: [Python](python/) · [Go](go/) · [TypeScript](ts/) · [Rust](rust/).
See [consolidation and release boundaries](docs/consolidation.md).
For a local Python checkout, install with `pip install -e './python[dev]'`;
run Python tests from `python/`.

[![CI](https://github.com/action-state-group/capsule-emit/actions/workflows/python.yml/badge.svg)](https://github.com/action-state-group/capsule-emit/actions/workflows/python.yml)

> **New here? → [docs/start-here.md](docs/start-here.md)** — the one-page front door.
>
> **Vocabulary:** [TRANSLATION.md](TRANSLATION.md) — the dev / auditor / spec decoder.

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
print(capsule.capsule_id, capsule.signature)   # sealed and signed; witnessed once you name a witness (CAPSULE_WITNESS_URL); anchor is a legacy opt-in
```

```bash
pip install capsule-emit
```

`capsule-emit` is the producer layer for the **Agent Action Capsule** — a [SCITT](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/) statement profile. You add one line at the moment your agent does something consequential; you get back a digest-committed, content-addressed capsule — witnessed by the log(s) you choose — that a third party who trusts neither you nor your agent can independently verify.

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

**Start here.** Call `seal()` at each consequential action. You get a **verifiable ledger** of what your agent did — each capsule appended locally to `ledger.jsonl` — and, once you name a witness, checkpoints of it held outside your environment. That's the whole starting point. Everything below is optional depth you grow into — no rewrite.

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
- **Cite external records** — add a cross-record citation to a capsule outside this chain's scope (draft-04 §5.5.5): `seal(payload, references=(ReferenceEntry(type="agent-action-capsule", digest_alg="SHA-256", digest=other_id),))`. On the slot-form `seal(who(...), did(...), references=...)` the citation lands on the composition capsule only, not the individual members. References are committed to `capsule_id` before signing.
- **Declare now, enforce later** — a `manifest.md` declares your rules; a compatible gateway enforces the *same file*, with no change to your `seal()` calls.

The unit is the **capsule** (one action). What you keep and grow is the **ledger** (the witnessed trail). Chaining links specific capsules within it. Start with the ledger; add the rest when you need it. → walk it end-to-end in the **[tutorials](docs/tutorials/)**.

## What you get back

`seal()` returns an **`EmitResult`** — `cap.capsule_id`, `cap.signature`, `cap.key_id`, `cap.seq`, `cap.witness_outcome`, and `cap.capsule` (the capsule itself, plain JSON you can store or hand to anyone). It carries the `capsule_id` (a SHA-256 content address), a **self-attested `signature`** over that content by a persisted producer key (verify it straight from the capsule via `verify_capsule_signature` — see `capsule_emit.signing`), the accountable `operator` + `developer`, the **may/did verdict**, the **effect** (and its dispatched-vs-confirmed status), and **digests of your input and output** — your inputs and outputs are committed by hash; **you hold the raw values, the capsule does not** (only their digests). `cap.seq` is its position in your log — already a leaf, ambiently, before any checkpoint — and both `repr(cap)` and `capsule-emit ledger show` render it as `#logged @ leaf <seq>`. (`cap.anchored` / `cap.anchor_status` also exist, but only report the legacy, non-default per-capsule anchor channel below — kept for compatibility, not part of the default story.)

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

**0.5.0 pushes.** Anchoring (above) is per-**capsule**; checkpointing is per-**stream**. `seal()` also folds every capsule into a per-ledger [Merkle Mountain Range](docs/checkpoint.md) and — every ~100 records (`capsule_emit.witness.DEFAULT_CADENCE_ENTRIES`) — builds a signed **checkpoint** and registers it with the witness(es) you name: a summary of the whole stream so far (its size, a root hash, a timestamp), sent to an independent witness's `/checkpoints` route so it can verify the checkpoint's own signature before counter-signing. Async, same as the anchor; your payloads never leave.

- **Off is one flag, honored everywhere:** `seal(payload, witness=False)` for one call, `CAPSULE_WITNESS=off` for every call — no code change. With no witness named, nothing is sent at all.
- **Your log is still your file.** The witness only ever sees the checkpoint (never your capsule content, never a per-record digest); walking away from it loses no history — `ledger.jsonl` is complete on its own, the witness just lets someone else confirm you didn't rewrite it after the fact.
- **Force a checkpoint on demand.** `push()` builds and registers a checkpoint right now, without waiting for the cadence — useful before a process exits or at a natural audit boundary.
- **Any witness works, and more than one is stronger.** There is no default: name the one(s) you use with `CAPSULE_WITNESS_URL` / `seal(payload, witness_url=...)`. A free public one runs at `witness.agentactioncapsule.org` (a separate, live witness service, `POST /checkpoints`), and any conforming Transparency Service works the same way; and you can register with several at once (a list, or comma-separated) for a stronger, equivocation-resistant tier. The first checkpoint of a process prints one line to stderr — once — naming exactly what's sent, where, and how to turn it off.

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

| Adapter | What it wraps | [`ConnectorPort`](python/capsule_emit/connector.py) |
|---------|---------------|:---:|
| **MCP** | Model Context Protocol tool endpoints — any Python callable, decorator-based | ✅ `decorator` |
| **Google ADK** | Google Agent Development Kit tool calls, one capsule per completed tool invocation | — |
| **agentgateway** | Rust proxy for MCP/A2A/LLM traffic; seals all `tools/call` at the gateway chokepoint | — |
| **LangChain** | LangChain callback handler; fires on `on_tool_start`/`on_tool_end` automatically | ✅ `listener` |
| **CrewAI** | Wraps a CrewAI tool object; emits one capsule per call, input and output captured | — |
| **Goose** | Block's open-source AI agent; Goose tools are MCP tools, so the MCP adapter applies | ✅ `decorator` (via MCP) |
| **Hermes** | Custom agent loops; call `after_tool(...)` explicitly after any tool finishes | — |
| **Dapr Agents** | Dapr Agents decision points; one capsule per action | — |
| **Agno** | Agno tool hooks; planned / confirmed / failed capsules per tool call | — |
| **LiteLLM** | LiteLLM proxy callbacks; a request / outcome capsule pair per LLM call | — |
| **OpenAI Agents SDK** | an `openai-agents` run listener; planned / confirmed / failed capsules per tool call | — |
| **Strands Agents** | Strands hook events; planned / confirmed / failed capsules per tool call | — |
| **LlamaIndex** | a LlamaIndex agent listener; planned / confirmed / failed capsules per tool call | — |
| **Microsoft Agent Framework** | Agent Framework middleware; planned / confirmed capsules per tool call and per run | — |
| **NVIDIA NeMo Guardrails** | one capsule per rail decision, chained per turn | — |
| **Inspect** (`inspect_ai`) | an Inspect eval log, after the fact: one record per model call, per tool call, and per sample's end state | — |

Each adapter is one module in `capsule_emit/adapters/`, installed with its extra
(`pip install "capsule-emit[openai-agents]"`, `[strands]`, `[nemo-guardrails]`, …;
the extras pin the framework version each was tested against). LangChain and
CrewAI each have two shapes: the thin wrapper (`langchain.py`, `crewai.py`) and
an event listener that seals every tool call once registered
(`langchain_listener.py`, `crewai_listener.py`). `agentgateway_audit.py` reads
agentgateway's audit metadata, and `ext_mcp_pb2.py` is the generated protobuf
the gateway adapter speaks.

`ConnectorPort` (`capsule_emit.connector`) names the classify/capture contract every adapter
already implements informally — declared as a `typing.Protocol`, checkable with
`isinstance(adapter, ConnectorPort)`, opt-in per adapter (see `docs/whats-consequential.md`).
Two adapters conform today; the rest keep their existing, adapter-specific surface unchanged.

**Each adapter page has a paste-ready prompt for a coding agent** to wire emission into your
tools: **[docs/adapters/](docs/adapters/)**.
## Commands and servers

The package installs three commands:

| Command | What it does |
|---|---|
| `capsule-emit` | `ledger view` / `ledger show` (read the local ledger), `verify`, `status` (what is logged, which checkpoint covers what, the witnessing lag), `export` (a scoped evidence file for a third party: one exchange, peer or window), `evidence` (a verification comment built from a ledger, re-verifying every capsule first), `report` (one readable HTML page from an evidence file, offline), `disclose` (a bundle plus selected content, with its own sealed disclosure record), `permalink` (a demo verify-page link) |
| `capsule-emit-server` | a companion MCP server (`capsule_emit/server.py`), run as a Goose extension or by any MCP client, to record, verify and inspect capsules from a session |
| `capsule-emit-agentgateway` | the agentgateway adapter's process entry point |

`capsule-emit <command> --help` gives every flag.

## Beyond `seal()`: what else the package carries

Each of these is a module you can import; none is needed for `seal()`.

| Area | Modules | What they do |
|---|---|---|
| Two-party records | `bilateral.py`, `settlement.py`, `reconciliation.py`, `period.py` | the bilateral attestation protocol's reference implementation (`draft-mih-agent-bilateral-attestation-00`; see [docs/bilateral-reconciliation.md](docs/bilateral-reconciliation.md)); settlement records, where two parties each record the same payment; reconciling two independently sealed halves of one exchange; and `--period week|month` as sugar over a time window |
| Record patterns | `approval.py`, `adjudication.py` | a human approval sealed and chained to the capsule it unblocks; a verdict capsule for a twin comparison (which of two answers an independent recompute matched) |
| Evidence files | `evidence_file.py`, `evidence_report.py`, `evidence_request.py`, `evidence.py`, `scoped_export.py` | check an Evidence Bundle (`evidence-bundle/v2`) from any producer; render it as one page; answer an evidence request (artifact, signed refusal, or recorded absence); the verification comment built from a ledger; and the scoped export |
| Handing records over | `bundle.py`, `disclose.py`, `disclosure.py`, `chain_segment.py`, `permalink.py`, `viewer.py` | the bundle anyone can verify; the recorded act of disclosing content to an audience, and its Disclosure Envelope; a run of the chain as history; the demo permalink; and the capsule-native ledger viewer |
| Witnessing | `witness.py`, `witness_bindings.py`, `witness_directory.py`, `checkpoint/` | the default-on checkpoint and witness wiring behind `seal()`; how one checkpoint reaches more than one kind of transparency service, and the plurality policy applied to the receipts; [`witnesses.json`](witnesses.json), the public witness directory, with its validator; and `checkpoint/`, a compatibility re-export of the checkpointed-local-log library |
| OpenTelemetry | `otel/` | a digest-only span exporter for the `org.agentactioncapsule.otel` correlation block (`draft-palanisamy-scitt-aac-otel-00`), plus span classification. Only the exporter needs the `otel` extra |
| Accounts and holds | `account/`, `holds/` | a neutral fold core (a derivation as data, replayable and re-checkable); and reservation-as-capsule holds for a budget scope (a separate code path that still writes format `2`; see Status) |
| Producer plumbing | `core.py`, `surface.py`, `signing.py`, `gate.py`, `manifest.py`, `constraints/`, `connector.py`, `ledger.py`, `ledger_io.py`, `canonicalization.py`, `numbers.py`, `verify.py`, `verify_canonicalization.py`, `verification.py`, `relations.py`, `spec_version.py`, `status.py` | capsule construction, the developer surface, the `Signer` seam, a stateless check-then-seal gate, the declare-only manifest parser and illustrative constraints, the adapter contract, ledger I/O, canonicalization and number rules, the verifiers' canonicalization adapters, and the version and relation tokens it writes |

## Rust

Two crates live in [`rust/`](rust/), each with its own README and CHANGELOG, built and tested by one Rust CI workflow:

- **[`rust/capsule-emit`](rust/capsule-emit/)**: seal, sign, chain and
  checkpoint Agent Action Capsule records in Rust. JCS capsule ids, COSE_Sign1
  statements, a durable local ledger and signed checkpoints, checked against the
  Python reference and the conformance vectors.
- **[`rust/capsule-emit-evidence-request`](rust/capsule-emit-evidence-request/)**:
  the evidence request protocol (`draft-mih-agent-evidence-request-00`): parse
  and resolve requests, sign and check refusals, build and check artifact
  answers over a checkpointed local log, and classify outcomes.

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

## What else is in this repository

| Path | What it is |
|---|---|
| `skills/openclaw/` | an agent skill (`SKILL.md`) and its small HTTP sealing server (`seal_server.py`): `POST /seal` at dispatch and on outcome, `GET /verify` |
| `examples/` | one runnable example per adapter, plus worked examples (approval, bilateral, cross-record references, verified invoice, the gate, A2A, multi-anchor receipts and others); most have a README, the rest a single runnable script |
| `flows/` | a sample `manifest.md` for the declare-now, enforce-later pattern |
| `test-vectors/` | vector sets the tests pin: bilateral payloads, evidence files, permalink bundles, the producer envelope, settlement records, slot composition, and a Go oracle |
| `commitment-conformance-vectors/` | the interoperable encoding of a checkpoint's MMR accumulator, with a reference verifier, so a second implementation can produce byte-identical commitments |
| `witnesses.json` | the public witness directory `witness_directory.py` reads |
| `docs/` | the guides linked above, the adapter pages, schemas, extensions and A2A notes |
| `ADOPT.md`, `TRANSLATION.md` | the 30-minute adopter path, and the vocabulary decoder |

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

- **`seal()` / `received()`** (and the `who()`/`can()`/`did()`/`audit()` slot verbs nested in `seal()`) — the developer surface above — produce **format `4`**, canonicalized per RFC 8785 JCS (`canonicalization_id="jcs"`).
- **`holds/` (reserve/release/expire/reconcile lifecycle capsules)** — a separate, vintage code path — still produces **format `2`** (`canonicalization_id="jcs-n"`, the absent-field-normalized profile). It is a deliberate exception, not drift: hold-lifecycle capsules were minted under the older profile and stay there rather than silently reformatting existing records.

Every capsule this library produces stamps `spec_version` `draft-mih-scitt-agent-action-capsule-05`. Its verifiers accept records carrying -04 or -05 alike, and treat any other `spec_version` as informational, never as a reason to reject: `spec_version` selects no algorithm (`capsule_emit.SPEC_VERSION`, `capsule_emit.ACCEPTED_SPEC_VERSIONS`).

When the spec revises, the version bumps and older capsules keep verifying; that's how this implementation stays tracked to the standard.

## Provenance, neutrality & governance

Developed by **Action State Group, Inc.** and published as **open-source software (Apache-2.0)**, with a clean transfer path to a **neutral home** (foundation donation or community project) as the ecosystem matures. The content is product-free — the emission layer, adapters, ledger utilities, and a manifest parser; nothing tenant- or product-specific. No primacy is claimed; the value is an interoperable, independently-verifiable record format. Discussion venue: the IETF **SCITT** Working Group (`scitt@ietf.org`).

## License

Apache-2.0 — see [LICENSE](LICENSE).

**Patent posture:** All six provisional patent applications related to this specification were expressly abandoned on July 6, 2026. No license is required. See [agentactioncapsule.org/ip](https://agentactioncapsule.org/ip) for details.
