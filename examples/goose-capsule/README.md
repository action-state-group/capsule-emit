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

## Verify permalink (withheld/bundle)

Copy-paste this into a browser — it's the 3-capsule bundle from `evidence/run-transcript.md`
(order → human denial → escalation), browser-confirmed to render the Chain Navigation table with
`decide | executed` → `decide | blocked` → `fyi | executed`:

```
https://verify.agentactioncapsule.org/v/eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiZWVkZjllZmEyNTQ0MjMzN2QyNDZjMTM5NTljNjU4ZjJjM2ZjZTY4Zjk4NTk3OWQ0ODhlNDU5YjBhZjgwYWQ0OCIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyL2Q0ZTZmMDhmLTQ3M2MtNDE2Zi1iOWY2LTBmYmJhYTdhNjE5NiIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxMzA2WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWE4ZTI1ODlkYzk4NmFiNzc5MjVlZmM0YzUzZjdmNDRkNWQ1YjZhNWJjYzA1ZWZmNTY0OGFlOTE2MGRhNGQzIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzZlZWI4YjcxLTdjZjAtNDQ0MC1hM2E2LWFjNDM5OTRmMTE3YSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxNTE5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJlZWRmOWVmYTI1NDQyMzM3ZDI0NmMxMzk1OWM2NThmMmMzZmNlNjhmOTg1OTc5ZDQ4OGU0NTliMGFmODBhZDQ4IiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjEwOWM2MTQzOTY3ZGMwZmQ5N2Y3Nzc3ZWJlMjg2NmM2MmNkYTUxYmU1ZDM5N2UyNDM2YWNiNTg2MWU3ZDQ4NzQiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvNDhkMjRlMzUtMTc3Yi00NzBlLWE5MGEtZjM5YTBlOWYzMmJjIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wNFQxOTowMzowMS45MzE3MDdaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICI0MWE4ZTI1ODlkYzk4NmFiNzc5MjVlZmM0YzUzZjdmNDRkNWQ1YjZhNWJjYzA1ZWZmNTY0OGFlOTE2MGRhNGQzIiwgInJlbGF0aW9uIjogImVzY2FsYXRlcyJ9fV0=
```

This is the **withheld/bundle** permalink — the JSON-array fragment that always renders the
Chain Navigation table. It was independently reproduced byte-for-byte with the new
`capsule-emit permalink` subcommand (`capsule_emit.permalink.build_url(..., bundle=True)`) against
this same 3-capsule ledger, and re-verified with:

```bash
capsule-emit permalink --ledger <this run's ledger.jsonl> --check
# → permalink --check: 3/3 capsule(s) VALID
# → 3 capsules — chain: executed → blocked → executed (eedf9efa → 41a8e258 → 109c6143)
```

`--check` runs `agent_action_capsule.verify()` on every capsule locally (no network) and refuses
to emit a URL if any capsule fails verification — so a presenter can't hand out a bad demo link.

**Revealed links (`--reveal <artifact>`) are not available yet** — that flag depends on
[aac-disclosure-envelope], a separate, not-yet-built disclosure-envelope format change. Only
withheld links ship in this build. (`demo.py`'s own `_permalink`/`_bundle_permalink` helpers
predate the CLI and produce the identical byte-for-byte URL; migrating them to call
`capsule_emit.permalink` directly is a follow-up once this branch rebases onto a main that has
the module.)

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
