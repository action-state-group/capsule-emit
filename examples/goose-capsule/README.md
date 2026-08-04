# Goose Capsule Demo

A runnable example: Goose calls purchase-order tools through a small MCP extension, and every
tool call is sealed into a verifiable Agent Action Capsule — including a genuine human denial
and a live anchor to a public transparency log.

```
examples/goose-capsule/
├── demo.py     # standalone run — no Goose required, anchors live by default
├── server.py   # the actual MCP extension — hand this file to Goose
├── evidence/   # a real, live-anchored run (transcript + one sealed capsule)
└── README.md   # this file
```

## Run it standalone (no Goose required)

```bash
pip install "capsule-emit[dev,mcp]"
python examples/goose-capsule/demo.py             # anchors live by default
python examples/goose-capsule/demo.py --no-anchor # offline only, no network
```

This simulates exactly what Goose does when it calls `submit_order` / `get_price`: each call is
sealed, chained, and (unless `--no-anchor`) submitted to the public SCITT anchor. See
`evidence/run-transcript.md` for a real run's full output, including live leaf indices and
verify permalinks.

## Connect it to real Goose

`server.py` is the file you hand to Goose as a custom MCP extension. Add this block to
`~/.config/goose/config.yaml`:

```yaml
extensions:
  po_agent:
    enabled: true
    type: stdio
    name: po_agent
    description: "Purchase-order tools with capsule audit trail"
    cmd: python3
    args: ["/path/to/examples/goose-capsule/server.py"]
    timeout: 30
    envs:
      CAPSULE_OPERATOR: "acme-co"
      CAPSULE_DEVELOPER: "goose-agent@v1"
      CAPSULE_ANCHOR: "true"
```

That's the whole install — a few lines of config, no changes to how you talk to Goose. Every
time Goose calls `submit_order` or `get_price`, the call is sealed into `ledger.jsonl` next to
`server.py` (override with `CAPSULE_LEDGER`).

## What gets recorded

Each tool call is sealed into an **Agent Action Capsule**: a small signed JSON record
committing to what the agent did, not what it said. It carries digests of the tool input and
output, the verdict (`executed` / `blocked`), and — when a call chains to a prior one — a
`chain.parent_capsule_id` link. The raw tool input/output never leaves the process; only their
SHA-256 digests are committed into the capsule.

Verify the local ledger any time:

```bash
agent-action-capsule verify --store ledger.jsonl
```

This re-derives every `capsule_id` from its contents and confirms it matches — a single flipped
byte anywhere in the record makes verification fail.

## What leaves the machine, and how to turn it off

By default (`CAPSULE_ANCHOR` unset, or the `server.py` example config above, which sets it
explicitly), each sealed capsule is anchored: **only its `capsule_id` — a 64-character SHA-256
digest — is POSTed to `anchor.agentactioncapsule.org`.** No tool input, no tool output, no
vendor name, no dollar amount, no business content of any kind crosses the wire. The anchor
receipt lets anyone later prove that exact capsule existed at that time, without the anchor
operator ever seeing what it was for.

**The library itself defaults `CAPSULE_ANCHOR` to `"false"`** — a fresh `pip install` of
`capsule-emit` does not talk to the network until you opt in. This example's config block above
sets `CAPSULE_ANCHOR: "true"` explicitly so the demo shows live anchoring; if you don't want
that, delete the line (or set it to `"false"`) and every capsule stays fully local:

```yaml
    envs:
      CAPSULE_ANCHOR: "false"   # or omit the line — same effect
```

`"0"`, `"false"`, and `"no"` (any case) all mean off. `demo.py` anchors live by default too
(independent of `CAPSULE_ANCHOR` — it reads its own `--no-anchor` flag); run it with
`--no-anchor` for a fully offline pass.

## Verify a capsule against the public log

Every anchored capsule can be checked by a third party with no access to your ledger:

```bash
curl -s https://anchor.agentactioncapsule.org/v1/inclusion/<capsule_id>
```

returns the leaf index, tree size, root hash, and a COSE receipt proving inclusion in the
append-only Merkle tree — independent of trusting the anchor operator. See
`evidence/run-transcript.md` for a worked example with real permalinks
(`https://verify.agentactioncapsule.org/v/<capsule_id>#...`) that render the anchor banner,
digest graph, and privilege log in a browser with zero pasting.

## The chain: order → human denial → escalation

`evidence/run-transcript.md` documents a real 3-capsule chain: an order is submitted
(`write_order`, executed), a larger order is denied by a human approver
(`decide`, `verdict_class=blocked`, `human_disposed=true`, with a named approver and reason),
and the agent escalates past the denial to a manager (`fyi`, chained with
`chain.relation="escalates"`) instead of silently retrying. The denial is the point — it is the
kind of event an ordinary log line cannot prove happened, or prove *why*.
