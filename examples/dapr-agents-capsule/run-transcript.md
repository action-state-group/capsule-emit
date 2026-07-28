# Dapr Agents demo transcript

Generated: 2026-07-28  
Command: `python3 examples/dapr-agents-capsule/demo.py`

## Output

```
─── Capsule 1: fyi (execution record) ────────────────────────────
  capsule_id : e21cbf6fa49230dd82a556408b41b8c4855de068372169debadb8e77f78e04ca
  action_type: fyi
  verdict    : executed
  anchored   : True
  verified   : True

─── Capsule 2: decide (HITL decision) ────────────────────────────
  capsule_id   : 75719fad0a2229f5240e877bbbd4ce6c3859ebac4cc3efa10898eb8e9d381dbc
  action_type  : decide
  verdict      : executed
  human_disposed: True
  decision     : accept
  chained to   : e21cbf6fa49230dd82a556408b41b8c4855de068372169debadb8e77f78e04ca
  anchored     : True
  verified     : True

─── Side-by-side summary ─────────────────────────────────────────
  fyi    capsule_id: e21cbf6fa49230dd82a556408b41b8c4855de068372169debadb8e77f78e04ca
  decide capsule_id: 75719fad0a2229f5240e877bbbd4ce6c3859ebac4cc3efa10898eb8e9d381dbc
  Both verified    : True
  Both anchored    : True

Done.  Both capsule_ids are live on the anchor.
```

## Live capsule_ids on anchor.agentactioncapsule.org

| Type | capsule_id |
|---|---|
| fyi (execution record) | `e21cbf6fa49230dd82a556408b41b8c4855de068372169debadb8e77f78e04ca` |
| decide (HITL decision) | `75719fad0a2229f5240e877bbbd4ce6c3859ebac4cc3efa10898eb8e9d381dbc` |

The decide capsule chains to the fyi capsule via `chain.parent_capsule_id`.

## What this shows

- **Capsule 1 (fyi)**: produced by `@emitter.tool("check_invoice")` as the
  agent calls the tool.  Analogous to a Go-adapter execution record (post-hoc
  history) but captured live.  `action_type="fyi"` — the adapter observes what
  ran; the LLM decision happened upstream.

- **Capsule 2 (decide)**: produced by `emitter.record_hitl()` at the HITL
  gate.  Records Alice's real approval decision with `human_disposed=True`,
  `approver="human"`, `decision="accept"`.  Chained to capsule 1.

Both capsules verified offline via `agent_action_capsule.verify()` and are
anchored to the live SCITT Transparency Service.
