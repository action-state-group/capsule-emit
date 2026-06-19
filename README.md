# capsule-emit

One-call `emit()` for [Agent Action Capsules](https://github.com/action-state-group/agent-action-capsule) — anchor on by default, ledger view CLI, thin framework adapters.

**capsule-emit** is the producer/emission layer for the Agent Action Capsule
specification: a SCITT statement profile for recording what an AI agent actually
did in a sealed, independently-verifiable capsule.

> **Status.** Alpha — API stable, not yet 1.0. The underlying specification
> ([draft-mih-scitt-agent-action-capsule](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/))
> is an individual IETF Internet-Draft, not an RFC. No RFC number is claimed.

## Install

```bash
pip install capsule-emit                 # emit + anchor-client + ledger CLI
pip install "capsule-emit[langchain]"    # + LangChain callback adapter
pip install "capsule-emit[crewai]"       # + CrewAI adapter
```

## Quickstart (~5 minutes)

```python
from capsule_emit import emit

cap = emit(
    action="write_po",
    operator="acme-co",
    developer="po-agent@v1",
    agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
    agent_output=result,
    model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    verdict="executed",
    effect={"type": "write_po", "status": "dispatched"},
)
print(cap.capsule_id, cap.anchored)  # sealed + anchored
```

Anchor is on by default — a digest-only submission to a SCITT Transparency
Service. No business content crosses the wire. Set `anchor=False` for offline
use.

## Ledger view

Every `emit()` appends to a local JSONL ledger. View it with:

```bash
capsule-emit ledger view ./ledger.jsonl
```

## Verify

```bash
python -m agent_action_capsule verify ./ledger.jsonl
```

Class-1 verification is reproducible from the capsule bytes alone — no keys,
no network, no clock.

## Framework adapters

Thin adapters over one shared base — one `emit()` call per tool invocation,
regardless of framework:

```python
# MCP (primary)
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

emitter = MCPCapsuleEmitter(operator="acme-co", developer="my-agent@v1")

@emitter.tool("write_po")
def write_po(vendor: str, total: float) -> dict: ...

# LangChain
from capsule_emit.adapters.langchain import LangChainCapsuleCallback

cb = LangChainCapsuleCallback(operator="acme-co", developer="my-agent@v1")
chain.invoke(inputs, config={"callbacks": [cb]})

# CrewAI
from capsule_emit.adapters.crewai import CrewAICapsuleListener
```

## Manifest declarations

Place a `flows/<action>/manifest.md` alongside your code to declare autonomy
level and constraints:

```markdown
---
wicket_id: write-po
autonomy: narrate
---
## Effect
`write_po` — autonomy `narrate`, reversibility `two_way`.
## Constraints
| id | what it checks | method | severity |
|----|----------------|--------|----------|
| `po_arithmetic` | Line items re-add to total. | arithmetic_balance | **block** |
```

`capsule-emit` reads the manifest to *declare* — no enforcement, no gate.
A compatible enforcement gateway reads the same manifest file and *enforces*
the declared constraints. This is the same-file upgrade path: no changes to
emit() calls or manifests are required to add enforcement on top.

## Repository layout

```
capsule_emit/          Python package (emit, ledger, manifest, adapters)
  adapters/            mcp, langchain, crewai, hermes — all over one base
tests/                 pytest quickstart acceptance suite
examples/              quickstart_demo.py — emit → verify → ledger view
flows/                 example manifest declarations (write-po)
LICENSE                Apache-2.0
NOTICE                 Attribution
```

## Relationship to agent-action-capsule

`capsule-emit` is the **producer** layer; [`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule)
is the specification + reference **verifier**. They are intentionally separate:
any tool can produce a capsule; any party can verify one independently.

```
capsule-emit  →  agent-action-capsule (emit + verify)
                        ↓
              scitt-cose (COSE_Sign1 + SCITT receipt verify)
```

## Provenance, neutrality & governance

This library was developed by **Action State Group, Inc.** and is published as
**open-source software (Apache-2.0), intended for contribution to an appropriate
neutral home when the ecosystem matures** — whether that is a foundation or a
community project.

The content here is product-free: the emission layer, framework adapters, ledger
utilities, and a manifest declaration parser — nothing tenant-specific or
product-specific. The change controller is Action State Group, Inc., with a
clean transfer path to a neutral home (foundation donation or community project)
whenever that moment arrives. No primacy is claimed; the value is an
interoperable, independently-verifiable record format.

The underlying specification is an IETF contribution; the intended venue for
discussion is the IETF **SCITT** Working Group (`scitt@ietf.org`).

## License

Apache-2.0 — see [LICENSE](LICENSE).
