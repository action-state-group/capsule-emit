# Reading your ledger

**Goal:** see everything your agent did, in one table.

Every `seal()` (and `received()`) appends one line to a local
file (`ledger.jsonl` by default) — your running, append-only trail. You don't have
to manage it; it just accumulates.

## View it

After running the first two tutorials, you have a couple of capsules. Look at them:

```console
$ capsule-emit ledger view ledger.jsonl

capsule ledger: ledger.jsonl  (2 record(s))

  capsule_id      actor                                       verdict       effect                chain    verify
-----------------------------------------------------------------------------------------------------------------
  4910e7fd1826cf  po-agent@v1                                 executed      write_order:applied               ✓
  68b063d202a18d  agent [↑ dispatched:4910e7fd] (confirmed)   confirmed     write_order:applied   confirms→4910e7fd…  ✓
```

Read it left to right: a short id, who's accountable (and, for a confirm, which
capsule it points back at), the verdict, the effect, the **chain** link, and whether
it verifies. You can see the confirm capsule's `actor` column names the dispatch it
confirms. That's your *attempted → confirmed* trail, at a glance. (`effect` here
reads `applied` for both rows — the table collapses `dispatched`/`confirmed` to one
"it went through" bucket; the raw `dispatched` vs. `confirmed` status is in the
`--json` output below, or in full for one capsule via `capsule-emit ledger show
ledger.jsonl <capsule_id>` — see below.)

## Get the raw data

Need it for a script, a dashboard, or to pipe somewhere? Ask for JSON:

```console
$ capsule-emit ledger view ledger.jsonl --json
[
  { "capsule_id": "4910e7fd…", "action_type": "decide", "effect": { "status": "dispatched", ... }, ... },
  { "capsule_id": "68b063d2…", "effect": { "status": "confirmed", ... }, "chain": { "relation": "confirms", ... }, ... }
]
```

## Verify the whole file at once

The same ledger file is what the verifier checks — one command covers every capsule
in it:

```console
$ agent-action-capsule verify --store ./ledger.jsonl
```

Want the full detail on one capsule instead of a table row? `capsule-emit ledger
show` takes the ledger path and a `capsule_id` (a prefix of ≥8 chars is enough):

```console
$ capsule-emit ledger show ledger.jsonl 4910e7fd
── capsule 4910e7fd1826cf3f14e7494e141e44b796793032a771399456e4297ec3fdd676 ──  #logged @ leaf 1

  format_version                   4
  operator                         acme-co
  developer                        po-agent@v1
  action                           write_order
  ...
```

> **Coming soon:** a chain tree view — group a whole *attempted → approved →
> confirmed* sequence under one heading, instead of reading the `chain` column by
> eye. For now, the table above, `ledger show`, and `--json` cover reading and
> scripting.

## You just

Turned a pile of capsules into a readable trail — and a JSON feed you can build on.

**Next:** [Declaring rules →](04-declaring-constraints.md)
