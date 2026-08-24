# Goose Capsule Demo

A runnable example: Goose calls purchase-order tools through a small MCP extension, and every
tool call is sealed into a verifiable Agent Action Capsule — including a genuine human denial
and a live anchor to a public transparency log.

```
examples/goose-capsule/
├── demo.py     # standalone run — no Goose required, anchors live by default
├── server.py   # the actual MCP extension — hand this file to Goose
├── contribution_demo.py  # issues-first lifecycle demo → Verification evidence comment
├── evidence/   # a real, live-anchored run (transcript + one sealed capsule
│               #  + verification-comment.md from contribution_demo.py)
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
https://verify.agentactioncapsule.org/v/f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiZjcwOGI5MmEzNGIxNWI1ODJkYjYwMDQyNjE5YzM3YjM4MGYwYjY0YjVjNmMwYmJhNmUxM2E3MTY1MmY5OGQzYiIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyL2RlOGI3ZjhjLTdiNzQtNGU1NC05MDA1LTExNDRiMDYzYWExOCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMDEyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIxNmE2YWI5NTQyMDI5MWFjOTc3MDExZmE4NjIwNTE2YjAyYTQ0ODUwYTExNDMzYzA5ZTZhMzU1ZTY2MGNjZWZkIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2QxOWIyNGI4LWY0MzQtNGFkMy04MmY2LTczMDM4ODFhMDgyMCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMzIyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJmNzA4YjkyYTM0YjE1YjU4MmRiNjAwNDI2MTljMzdiMzgwZjBiNjRiNWM2YzBiYmE2ZTEzYTcxNjUyZjk4ZDNiIiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjA2MWU2YmRlZDg3ZDNjNDZkNjQyNTAxYWExMDg1YmQ4N2FlMTAyYzhiNDQ1OWI1Yzk1MmRiZmFlNjM0YjNhM2IiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvYjBiMGNjMGMtOWU4NS00ZTA4LWE4ZTgtOGIxYzJiMmRmMTZjIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMTowMzo0OS4xODI1OTFaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICIxNmE2YWI5NTQyMDI5MWFjOTc3MDExZmE4NjIwNTE2YjAyYTQ0ODUwYTExNDMzYzA5ZTZhMzU1ZTY2MGNjZWZkIiwgInJlbGF0aW9uIjogImVzY2FsYXRlcyJ9fV0=
```

This is the **withheld/bundle** permalink — the JSON-array fragment that always renders the
Chain Navigation table. It was independently reproduced byte-for-byte with the
`capsule-emit permalink` subcommand (`capsule_emit.permalink.build_url(..., bundle=True)`)
against this same 3-capsule ledger, and re-verified with:

```bash
capsule-emit permalink --ledger <this run's ledger.jsonl> --check
# → permalink --check: 3/3 capsule(s) VALID
# → 3 capsules — chain: executed → blocked → executed (f708b92a → 16a6ab95 → 061e6bde)
```

`--check` runs `agent_action_capsule.verify()` on every capsule locally (no network) and refuses
to emit a URL if any capsule fails verification — so a presenter can't hand out a bad demo link.
`demo.py`'s own `_permalink`/`_bundle_permalink` helpers (step 9 of the run — see
`evidence/run-transcript.md`) produce the identical byte-for-byte URL; they predate the CLI and
haven't been migrated to call `capsule_emit.permalink` directly yet.

## Verify permalink (disclosed)

The order/vendor/amount data in this demo is entirely synthetic (acme-co, Frobozz Supply,
Globex Corp — nobody real), so there's no reason to keep it withheld once you actually want to
*see* what a capsule committed to, not just its digest. `--reveal FIELD=payload.json` wraps a
single capsule in the [Disclosure Envelope](https://github.com/action-state-group/agent-action-capsule/issues/66)
(`{"capsule": <unmodified, anchored bytes>, "disclosures": {...}}`) — the capsule itself never
changes; the disclosed payload rides alongside it, and the verify page recomputes its digest
against the field the capsule already committed to and shows the match:

```bash
capsule-emit permalink capsule2.json \
  --reveal agent_input=capsule2_input.json --reveal agent_output=capsule2_output.json
# → permalink --reveal: 2/2 disclosed field(s) digest-match VALID
```

Capsule 2's disclosed permalink (the human denial — chained order → **REJECTED** → escalation):

```
https://verify.agentactioncapsule.org/v/16a6ab95420291ac977011fa8620516b02a44850a11433c09e6a355e660ccefd#eyJjYXBzdWxlIjogeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIxNmE2YWI5NTQyMDI5MWFjOTc3MDExZmE4NjIwNTE2YjAyYTQ0ODUwYTExNDMzYzA5ZTZhMzU1ZTY2MGNjZWZkIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2QxOWIyNGI4LWY0MzQtNGFkMy04MmY2LTczMDM4ODFhMDgyMCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMzIyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJmNzA4YjkyYTM0YjE1YjU4MmRiNjAwNDI2MTljMzdiMzgwZjBiNjRiNWM2YzBiYmE2ZTEzYTcxNjUyZjk4ZDNiIiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19LCAiZGlzY2xvc3VyZXMiOiB7ImFnZW50X2lucHV0IjogeyJwb19udW1iZXIiOiAiUE8tNzc3OCIsICJ2ZW5kb3IiOiAiR2xvYmV4IENvcnAiLCAiYW1vdW50X3VzZCI6ICIxMjUwMDAuMDAiLCAicmVxdWVzdGVkX2J5IjogImdvb3NlLWFnZW50QHYxIn0sICJhZ2VudF9vdXRwdXQiOiB7InJldmlld2VkX2F0IjogIjIwMjYtMDgtMDNUMDA6MDA6MDBaIiwgInJlYXNvbiI6ICJvcmRlciB2YWx1ZSBleGNlZWRzIHZlbmRvcidzIGFwcHJvdmVkIFBPIGNlaWxpbmcifX19
```

Open it: the Privilege Log now reads `REVEALED · ✓ match` for both `agent_input` and
`agent_output` (was `WITHHELD` before), Integrity/Sequence still both `✓`, and the anchor banner
still reads `Anchored log index 268` — unchanged, because the capsule's own bytes never moved.
The escrowed denial reason ("order value exceeds vendor's approved PO ceiling") and the human
approver's identity are right there instead of a digest. See `evidence/run-transcript.md` for
the other two capsules' disclosed permalinks and the full before/after fragment-size table.

Withholding any field still leaves the record verifying — that's the point being demonstrated,
not just claimed: the capsule doesn't need the payload to prove it exists and hasn't changed;
disclosure is a separate, later, revocable choice about the same sealed bytes.

## Verify permalink (disclosed bundle)

The array-fragment *bundle* permalink also supports per-item disclosure now — each capsule in
the bundle can independently be disclosed or left withheld. Use `SELECTOR:FIELD=payload.json`,
where `SELECTOR` is the 1-based record number shown in the chain summary (or an `>=8`-char
`capsule_id` prefix, same convention as `ledger show`):

```bash
capsule-emit permalink --ledger <this run's ledger.jsonl> --check \
  --reveal 1:agent_input=capsule1_input.json --reveal 1:agent_output=capsule1_output.json \
  --reveal 2:agent_input=capsule2_input.json --reveal 2:agent_output=capsule2_output.json \
  --reveal 3:agent_input=capsule3_input.json --reveal 3:agent_output=capsule3_output.json
# → permalink --reveal: 6/6 disclosed field(s) digest-match VALID (3/3 capsule(s) disclosed)
```

The fully disclosed bundle for this run — all three capsules, all six payload fields:

```
https://verify.agentactioncapsule.org/v/f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b#W3siY2Fwc3VsZSI6IHsic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiZjcwOGI5MmEzNGIxNWI1ODJkYjYwMDQyNjE5YzM3YjM4MGYwYjY0YjVjNmMwYmJhNmUxM2E3MTY1MmY5OGQzYiIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyL2RlOGI3ZjhjLTdiNzQtNGU1NC05MDA1LTExNDRiMDYzYWExOCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMDEyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgImRpc2Nsb3N1cmVzIjogeyJhZ2VudF9pbnB1dCI6IHsidmVuZG9yIjogIkZyb2JvenogU3VwcGx5IiwgImFtb3VudCI6ICIxMjQwLjE5IiwgInBvX251bWJlciI6ICJQTy03Nzc3In0sICJhZ2VudF9vdXRwdXQiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInBvX251bWJlciI6ICJQTy03Nzc3IiwgInZlbmRvciI6ICJGcm9ib3p6IFN1cHBseSIsICJhbW91bnRfdXNkIjogIjEyNDAuMTkiLCAiY29uZmlybWF0aW9uX3JlZiI6ICJDT05GLTc3NzcifX19LCB7ImNhcHN1bGUiOiB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjE2YTZhYjk1NDIwMjkxYWM5NzcwMTFmYTg2MjA1MTZiMDJhNDQ4NTBhMTE0MzNjMDllNmEzNTVlNjYwY2NlZmQiLCAiYWN0aW9uX2lkIjogImFwcHJvdmVfbGFyZ2Vfb3JkZXIvZDE5YjI0YjgtZjQzNC00YWQzLTgyZjYtNzMwMzg4MWEwODIwIiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMTowMzo0OS4xODIzMjJaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJmMTBjYmVhOGU0YmZjNTEzNGU3MTc2NzRhZWNmYzQxZGFiMWExMjMwNWMxMWI1NGUwNTQ0MmRiM2ZiOTJiOWE4IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWJjODlmODg4Yzk1N2ViZDI3YTI4MjVlYzg4MmM2Mjk1OWEyNGM1MTRiNTkxYmRkNWI4YWY4OWJjN2JlMDYwOSIsICJydW50aW1lIjogIm1jcCIsICJhcHByb3Zlcl9pZCI6ICJwcml5YUBhY21lLWNvLmNvbSJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogInBsYW5uZWQiLCAidHlwZSI6ICJhcHByb3ZlX2xhcmdlX29yZGVyIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJub3RfYXBwbGljYWJsZSIsICJsZWRnZXJfbW9kZSI6ICJjaGFpbmVkIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAicmVqZWN0IiwgImFwcHJvdmVyIjogImh1bWFuIiwgImh1bWFuX2Rpc3Bvc2VkIjogdHJ1ZSwgInZlcmRpY3RfY2xhc3MiOiAiYmxvY2tlZCJ9LCAiY2hhaW4iOiB7InBhcmVudF9jYXBzdWxlX2lkIjogImY3MDhiOTJhMzRiMTViNTgyZGI2MDA0MjYxOWMzN2IzODBmMGI2NGI1YzZjMGJiYTZlMTNhNzE2NTJmOThkM2IiLCAicmVsYXRpb24iOiAic2VxdWVuY2UifX0sICJkaXNjbG9zdXJlcyI6IHsiYWdlbnRfaW5wdXQiOiB7InBvX251bWJlciI6ICJQTy03Nzc4IiwgInZlbmRvciI6ICJHbG9iZXggQ29ycCIsICJhbW91bnRfdXNkIjogIjEyNTAwMC4wMCIsICJyZXF1ZXN0ZWRfYnkiOiAiZ29vc2UtYWdlbnRAdjEifSwgImFnZW50X291dHB1dCI6IHsicmV2aWV3ZWRfYXQiOiAiMjAyNi0wOC0wM1QwMDowMDowMFoiLCAicmVhc29uIjogIm9yZGVyIHZhbHVlIGV4Y2VlZHMgdmVuZG9yJ3MgYXBwcm92ZWQgUE8gY2VpbGluZyJ9fX0sIHsiY2Fwc3VsZSI6IHsic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiMDYxZTZiZGVkODdkM2M0NmQ2NDI1MDFhYTEwODViZDg3YWUxMDJjOGI0NDU5YjVjOTUyZGJmYWU2MzRiM2EzYiIsICJhY3Rpb25faWQiOiAiZXNjYWxhdGVfdG9fbWFuYWdlci9iMGIwY2MwYy05ZTg1LTRlMDgtYThlOC04YjFjMmIyZGYxNmMiLCAiYWN0aW9uX3R5cGUiOiAiZnlpIiwgIm9wZXJhdG9yIjogImFjbWUtY28iLCAiZGV2ZWxvcGVyIjogImdvb3NlLWFnZW50QHYxIiwgInRpbWVzdGFtcCI6ICIyMDI2LTA4LTEwVDIxOjAzOjQ5LjE4MjU5MVoiLCAibW9kZWxfYXR0ZXN0YXRpb24iOiB7Im1vZGVsX2lkIjogImNsYXVkZS1vcHVzLTQtOCIsICJwcm92aWRlciI6ICJhbnRocm9waWMiLCAiY29tcHV0ZV9hdHRlc3RhdGlvbiI6IHsiYWdlbnRfaW5wdXRfZGlnZXN0IjogImI0NDg0Y2VlMDRhNzk3YzgyZTMwMGZhNTlmMzUzNzE2M2Y1ZTRjYjVmYmNkZGM4YTI1OGIwNzZkZWE2ZjYyYjciLCAiYWdlbnRfb3V0cHV0X2RpZ2VzdCI6ICI2MWM4ZWFiMjEzZDNlMDM0ZjQ2NWEyZjU5ZGM1YTU1ZDFlZmI2OGY1OTRkMjc2ODNiMDQ0MzUxNjEwNDdiMzYzIiwgInJ1bnRpbWUiOiAibWNwIn19LCAiZWZmZWN0IjogeyJzdGF0dXMiOiAiZGlzcGF0Y2hlZCIsICJ0eXBlIjogImVzY2FsYXRlX3RvX21hbmFnZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJjaGFpbmVkIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9LCAiY2hhaW4iOiB7InBhcmVudF9jYXBzdWxlX2lkIjogIjE2YTZhYjk1NDIwMjkxYWM5NzcwMTFmYTg2MjA1MTZiMDJhNDQ4NTBhMTE0MzNjMDllNmEzNTVlNjYwY2NlZmQiLCAicmVsYXRpb24iOiAiZXNjYWxhdGVzIn19LCAiZGlzY2xvc3VyZXMiOiB7ImFnZW50X2lucHV0IjogeyJwb19udW1iZXIiOiAiUE8tNzc3OCIsICJyZWFzb24iOiAib3JkZXIgYmxvY2tlZCBhdCBhcHByb3ZhbCBnYXRlOyByb3V0aW5nIGZvciBtYW5hZ2VyIHJldmlldyJ9LCAiYWdlbnRfb3V0cHV0IjogeyJwb19udW1iZXIiOiAiUE8tNzc3OCIsICJlc2NhbGF0ZWRfdG8iOiAiYXAtbWFuYWdlckBhY21lLWNvLmNvbSJ9fX1d
```

Open it: every row in the Verification Ritual reads Integrity ✓ / Sequence ✓ — including the
enveloped items — because the deployed verify-surface viewer unwraps a per-item Disclosure
Envelope before checking (`scitt-cose#30`). The Chain Navigation table's ACTION_TYPE/VERDICT/
APPROVER columns populate correctly through the envelope too, and each record's Regulatory
context panel and Privilege Log detect the disclosure exactly like the single-capsule permalinks
above. See `evidence/run-transcript.md` for the full browser-confirmation transcript and the
before/after fragment-size table.

Withholding any field still leaves the record verifying — that's the point being demonstrated,
not just claimed: the capsule doesn't need the payload to prove it exists and hasn't changed;
disclosure is a separate, later, revocable choice about the same sealed bytes. (Earlier revisions
of this demo noted that the bundle permalink didn't support per-item disclosure — a viewer gap
in `scitt-cose`, not `capsule-emit`. That gap is fixed as of `scitt-cose#30`; the producer-side
refusal in `capsule_emit.permalink.build_url()` is lifted accordingly.)

## Goose's issues-first policy: evidence under the Verification stage

Goose moved contribution to an issues-first lifecycle ("Moving to issues as the
new PRs", 2026-07-30): Inbox → Accepted/design → Ready → In progress →
**Verification** → Done. PRs must implement a Ready issue and "explain how the
issue's verification plan was carried out" — and Verification is a human
confirming the work. That stage runs on prose; capsules give it records.

`contribution_demo.py` maps the lifecycle onto sealed evidence:

| Lifecycle stage   | What the demo does |
|-------------------|--------------------|
| Accepted/design   | the verification plan is agreed (the demo's `PLAN`) |
| Ready             | implementation begins — fresh ledger, issue-linked payloads |
| In progress       | every tool call sealed + chained: `run_repro` → `apply_patch` → `run_tests` |
| Verification      | `capsule-emit evidence --ledger … --issue <url>` renders the evidence comment |
| Done              | the issue closes with the evidence bundle in its record |

```bash
python examples/goose-capsule/contribution_demo.py            # offline
capsule-emit evidence --ledger ledger.jsonl --issue <ready-issue-url>
```

The builder is fail-closed: every capsule is re-verified locally at generation
time, and a tampered ledger refuses to render at all — a contributor
structurally cannot hand a reviewer an evidence comment whose records don't
verify. See `evidence/verification-comment.md` for a committed real run.

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

Each tool call is sealed into an **Agent Action Capsule**: a small content-addressed JSON
record committing to what the agent did, not what it said. It carries digests of the tool input and
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
