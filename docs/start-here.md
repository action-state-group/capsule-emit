# Start here

**capsule-emit records your agent's actions as verifiable capsules; `seal()` is the one call you make.**

## 1. What it is

`capsule-emit` turns each consequential thing your AI agent does into a **capsule** — a hashed, signed, content-addressed record that anyone can verify offline, without trusting you.

## 2. The one call

**You already log what your agent does. Move that log to the effect boundary — the moment an action takes effect — and call `seal()` instead of `log()`.** Same habit you already have, one extra benefit: every call appends one entry to a local, append-only ledger and its digest is witnessed — together a **witnessed, verifiable ledger** of what your agent did, checkable by anyone. If you know git: `seal()` is `git commit` for your agent's actions.

```python
from capsule_emit import seal

capsule = seal(
    {"vendor": "Frobozz Supply", "total": "1240.19"},   # payload: any JSON-serializable value
    action="write_order",
    operator="acme-co",                  # the accountable tenant
    developer="po-agent@v1",             # the agent identity + version
    verdict="executed",                  # executed | confirmed | denied | blocked
    effect={"type": "write_order", "status": "dispatched"},
)

print(capsule.capsule_id, capsule.signature)   # sealed, signed, witnessed by default
```

```bash
pip install capsule-emit
```

## 3. Or zero code changes

Don't want to write `seal()` at each call site? Add **one adapter listener per framework** and every tool call is sealed automatically:

| Your stack | One-liner |
|---|---|
| **LangChain / LangGraph** | `agent.invoke(..., config={"callbacks": [LangChainCapsuleListener(operator="acme-co", developer="my-agent@v1")]})` — see [adapters/langchain.md](adapters/langchain.md) |
| **CrewAI** | wrap your tool object with `CrewAICapsuleEmitter(...)` — see [adapters/crewai.md](adapters/crewai.md) |
| **MCP / any callable** | decorate the tool with `MCPCapsuleEmitter(...).tool("write_order")` — see [adapters/mcp.md](adapters/mcp.md) |
| **Others** | Hermes, Google ADK, Dapr, Goose, agentgateway — see [adapters/](adapters/) |

All adapters are thin shells over one shared base and seal the same capsule you'd get from calling `seal()` yourself.

## 4. When to call it

At **each consequential action** — the moments where "did your agent really do that, and was it authorized?" has a real answer at stake. → [what counts as consequential](whats-consequential.md).

## 5. What's underneath: the Checkpointed Local Log (CLL)

You never touch this to *use* `seal()` — but it's what makes the ledger verifiable, and it's worth 30 seconds. Under every `seal()` is a **Checkpointed Local Log (CLL)**: an append-only Merkle log on your own disk. `seal()` appends a leaf; on a cadence the library folds the whole history into one ~200-byte **checkpoint** and sends *only that* to a **witness** — an independent service that co-signs it so your history can't be quietly rewritten later. Your payloads never leave; only the checkpoint does.

If you know git, you already know the shape:

| git | capsule-emit |
|---|---|
| `git commit` | `seal(payload)` — record one action |
| your commit history | your **CLL** — the append-only log under every `seal()` |
| a signed tag | a **checkpoint** — one signed value over the whole history so far |
| `git push` | **witnessing** — an independent party vouches your history existed |

Unpushed git history can be quietly rewritten; pushed history can't. Same here: an unwitnessed log is yours alone, a witnessed one is checkable by strangers.

## 6. Grow into depth, only when needed

You never rewrite anything to add these — they're the same `seal()` surface, reached for when you need them:

- **Chain records into trails** — link a confirmation to its parent (*approved → executed → confirmed*):
  ```python
  confirmation = seal(payload, confirms=parent_capsule_id)
  ```
- **Compose from slots** — bind several members into one capsule that references them by slot and asserts nothing new:
  ```python
  from capsule_emit import seal, who, can, did, audit
  capsule = seal(who(agent_id), can(mandate), did(action), audit(check))
  ```
  Composition *is* nesting the slot verbs `who`/`can`/`did`/`audit` inside `seal()` — there is no separate `compose()` call.
- **Ingest a foreign signed artifact** — bring in something someone else already signed, as-transmitted, under its own declared type:
  ```python
  from capsule_emit import received
  effect = received(mandate_bytes, type="machine-mandate")
  ```
  `received(...)` is also legal nested inside `seal()` — `seal(received(bytes, type=...))` and the standalone call produce the identical capsule.
- **Force a checkpoint** — push a signed checkpoint of your log to the witness now, instead of waiting for the cadence:
  ```python
  from capsule_emit import push
  push()
  ```
- **Witness / anchor knobs** — witnessing to `witness.agentactioncapsule.org` (`POST /checkpoints`) is on by default; turn it off with one flag (`seal(payload, witness=False)`, or `CAPSULE_WITNESS=off` everywhere). The legacy per-capsule anchor channel is off by default (`anchor=True` to opt back in).

## 7. Read & verify

View your ledger:

```bash
capsule-emit ledger view ./ledger.jsonl
```

Anyone can verify a capsule (or a whole ledger) independently, from the bytes alone — no keys, no network, no clock — with the separate spec package:

```bash
pip install agent-action-capsule
agent-action-capsule verify --store ./ledger.jsonl
```

The verifier is independent of `capsule-emit` on purpose: *any* tool can produce a capsule; *any* party can verify one.

---

## Coming from an older version?

- **`emit()` → `seal()`.** The top-level producer verb was renamed. `emit()` remains importable for one release as a raising stub that points you at `seal()`.
- **`compose()` / `carry()` are retired as public verbs.** There is no `compose()`/`carry()` call. "Composition" is now nesting the slot verbs `who`/`can`/`did`/`audit` inside `seal(...)`; bringing in a foreign signed artifact is `received(bytes, type=...)` (nested in `seal()` or standalone).
- **Anchor is now legacy and off by default.** The per-capsule anchor channel is an explicit, non-default opt-in (`anchor=True` / `CAPSULE_ANCHOR=legacy-on`), kept for one release as a rollback path.
- **Checkpoint/witness is the default push.** Every sealed ledger is folded into a per-ledger Merkle Mountain Range and, at the cadence, a signed checkpoint is registered with the witness — the separate, live `witness.agentactioncapsule.org` service at its `POST /checkpoints` route. This is the only default egress channel as of 0.5.0.
