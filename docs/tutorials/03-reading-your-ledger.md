# Reading your ledger

**Goal:** see everything your agent did, in one table.

Every `emit()` appends one line to a local file (`ledger.jsonl` by default) — your
running, append-only trail. You don't have to manage it; it just accumulates.

## View it

After running the first two tutorials, you have a couple of capsules. Look at them:

```console
$ capsule-emit ledger view ledger.jsonl

capsule-emit ledger: ledger.jsonl  (2 record(s))

capsule_id      action                  operator        effect/status           verdict       chain
---------------------------------------------------------------------------------------------------
96d457260535f3  write_order                acme-co         write_order:dispatched     executed
7430c9d886ebcf  write_order                acme-co         write_order:confirmed      confirmed     confirms→96d45726…
```

Read it left to right: a short id, what happened, who's accountable, the effect and
its status, the verdict, and — in the last column — the **chain** link. You can see
the confirm capsule points back at the attempt. That's your *attempted → confirmed*
trail, at a glance.

## Get the raw data

Need it for a script, a dashboard, or to pipe somewhere? Ask for JSON:

```console
$ capsule-emit ledger view ledger.jsonl --json
[
  { "capsule_id": "96d45726…", "action_type": "decide", "effect": { ... }, ... },
  { "capsule_id": "7430c9d8…", "chain": { "relation": "confirms", ... }, ... }
]
```

## Verify the whole file at once

The same ledger file is what the verifier checks — one command covers every capsule
in it:

```console
$ agent-action-capsule verify --store ./ledger.jsonl
```

> **Coming soon:** richer views — a chain tree (group a whole *attempted → approved
> → confirmed* sequence under one heading) and a single-capsule detail view. For
> now, the table above plus `--json` covers reading and scripting.

## You just

Turned a pile of capsules into a readable trail — and a JSON feed you can build on.

**Next:** [Declaring rules →](04-declaring-constraints.md)
