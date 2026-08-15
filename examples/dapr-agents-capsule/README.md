# Dapr Agents Capsule Demo

A runnable example: a Dapr Agents tool call and its human-in-the-loop approval, each sealed into
a verifiable Agent Action Capsule and anchored to a public transparency log.

```
examples/dapr-agents-capsule/
├── demo.py             # standalone run — anchors live, verifies offline, prints a permalink
├── run-transcript.md   # a real, live-anchored run (full output + verify permalink)
└── README.md           # this file
```

## Run it

```bash
pip install "capsule-emit[dev,dapr-agents]"
python examples/dapr-agents-capsule/demo.py
```

Seals a `fyi` capsule (tool call) and a chained `decide` capsule (HITL approval), anchors both
synchronously to `https://anchor.agentactioncapsule.org`, verifies both offline
(`agent_action_capsule.verify()` + `scitt_cose.verify_receipt()`), and prints a verify permalink.

## Verify permalink (withheld/bundle)

Copy-paste this into a browser — it was produced by this run and browser-confirmed to render the
Chain Navigation table with both capsules (`fyi | executed` → `decide | executed`):

```
https://verify.agentactioncapsule.org/v/5179216505a0949e3195db1b9d180d43cea059babcfee12859628cb8a1dc8380#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiNTE3OTIxNjUwNWEwOTQ5ZTMxOTVkYjFiOWQxODBkNDNjZWEwNTliYWJjZmVlMTI4NTk2MjhjYjhhMWRjODM4MCIsICJhY3Rpb25faWQiOiAiY2hlY2tfaW52b2ljZS85ZTQ1NWZmYi01NGJiLTQ3OTEtYWIxYS0zNTJjNjJkMmU5NmMiLCAiYWN0aW9uX3R5cGUiOiAiZnlpIiwgIm9wZXJhdG9yIjogImFjbWUtY28iLCAiZGV2ZWxvcGVyIjogImludm9pY2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDZUMjA6MTE6MTUuMDY4NDcxWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsiY29tcHV0ZV9hdHRlc3RhdGlvbiI6IHsiYWdlbnRfaW5wdXRfZGlnZXN0IjogImE5ODFkNDUxMmIwNjE4NmMzYTA4MWUyZWQ5YjE4MzBhMjIxNmIxYjk4Y2FkMDcwNzJlMjAxZWIyMzU0ZGRjYzUiLCAiYWdlbnRfb3V0cHV0X2RpZ2VzdCI6ICJmZjg3MjE2NTc1Mjg1OTRjMGI3YTJiMzUxYjkwNzQ1NWNmNzkxMDdlMmUzMWE5NDViM2U2YjNkYTA2ZDRlNmZjIiwgInJ1bnRpbWUiOiAiZGFwcl9hZ2VudHMiLCAiZGFwcl9hZ2VudHMiOiB7ImFnZW50X25hbWUiOiAiaW52b2ljZS1jaGVja2VyIiwgInRvb2xfbmFtZSI6ICJjaGVja19pbnZvaWNlIiwgIndvcmtmbG93X2luc3RhbmNlX2lkIjogIndmLWRlbW8tMjAyNi0wNy0yOCIsICJhcHBfaWQiOiAiaW52b2ljZS1hcHAifX19LCAiZWZmZWN0IjogeyJzdGF0dXMiOiAiZGlzcGF0Y2hlZCIsICJ0eXBlIjogImNoZWNrX2ludm9pY2UiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI3MzViMWIwMWQ0MWEyOWU4NTVjYTc4YTFiMmU0NGQzMjk3NDU4MjcxNTA3NTFkZDY2ZWIxYmZkYWM1ZTdlOTVmIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX3BheW1lbnQvMzExZDZlZTEtYTZlMy00Mzc3LTk0ZmUtMjljMmQxODhkZmI4IiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJpbnZvaWNlLWFnZW50QHYxIiwgInRpbWVzdGFtcCI6ICIyMDI2LTA4LTA2VDIwOjExOjE1LjY1NTU0OVoiLCAibW9kZWxfYXR0ZXN0YXRpb24iOiB7ImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJmZGY4N2Y1NDk3OWY4NWExM2Q2YWRkZjkzOGZiZjE3NzE0YjQyMWNiZTdhMzkyZDkxNjFkYTNhNzM1ZjE1NWRkIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiOGE5NzJhZDBjMmM1NGY3MTQ1OGFjYmFhYTdjYjc3Zjg0NWJiY2NkZWUxMGE5YjRiMzgzMDBiOGViYjRmZjZlZiIsICJydW50aW1lIjogImRhcHJfYWdlbnRzIiwgImRhcHJfYWdlbnRzIjogeyJhZ2VudF9uYW1lIjogImludm9pY2UtY2hlY2tlciIsICJ0b29sX25hbWUiOiAiYXBwcm92ZV9wYXltZW50IiwgIndvcmtmbG93X2luc3RhbmNlX2lkIjogIndmLWRlbW8tMjAyNi0wNy0yOCIsICJhcHBfaWQiOiAiaW52b2ljZS1hcHAiLCAiYXBwcm92ZXJfaWQiOiAiYWxpY2VAYWNtZS5jb20ifX19LCAiZWZmZWN0IjogeyJzdGF0dXMiOiAiZGlzcGF0Y2hlZCIsICJ0eXBlIjogImFwcHJvdmVfcGF5bWVudCIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAiaHVtYW4iLCAiaHVtYW5fZGlzcG9zZWQiOiB0cnVlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9LCAiY2hhaW4iOiB7InBhcmVudF9jYXBzdWxlX2lkIjogIjUxNzkyMTY1MDVhMDk0OWUzMTk1ZGIxYjlkMTgwZDQzY2VhMDU5YmFiY2ZlZTEyODU5NjI4Y2I4YTFkYzgzODAiLCAicmVsYXRpb24iOiAiY29uZmlybXMifX1d
```

This is the **withheld/bundle** permalink — the JSON-array fragment that always renders the
Chain Navigation table, produced with `capsule_emit.permalink.build_url(..., bundle=True)` (the
same code behind `capsule-emit permalink`). Regenerate it yourself any time:

```bash
capsule-emit permalink --ledger <ledger.jsonl produced by this run> --check
```

`--check` runs `agent_action_capsule.verify()` on every capsule locally (no network) before
printing a URL, and refuses to emit one if any capsule fails verification.

**Revealed links (`--reveal <artifact>`) are not available yet** — that flag depends on
[aac-disclosure-envelope], a separate, not-yet-built disclosure-envelope format change. Only
withheld links ship in this build.

## What gets recorded

`fyi` (execution record, produced by `@emitter.tool()`) and `decide` (HITL decision record,
produced by `emitter.record_hitl()`) each carry digests of their inputs/outputs, a verdict
(`executed`), and — for `decide` — a `chain.parent_capsule_id` link back to the `fyi` capsule
it approves. Only the SHA-256 digests are committed; raw tool input/output never leaves the
process. See `run-transcript.md` for the full run, including live leaf indices and the
independently browser-confirmed permalink above.

## Verify a capsule against the public log

```bash
curl -s https://anchor.agentactioncapsule.org/v1/inclusion/<capsule_id>
```

returns the leaf index, tree size, root hash, and a COSE receipt proving inclusion in the
append-only Merkle tree, independent of trusting the anchor operator.
