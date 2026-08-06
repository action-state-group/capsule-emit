# Goose Capsule Demo — Run Transcript

**Run:** `python3 examples/goose-capsule/demo.py`
**Anchor:** `https://anchor.agentactioncapsule.org` (production, live)
**Verify surface:** `https://verify.agentactioncapsule.org`
**Branch:** `demo/goose-run`
**Run date:** 2026-08-04 (regenerated for `[goose-demo-merge-fixes]` — fixes 2 and 3 below
change capsule content, so `capsule_id` and every downstream leaf/permalink from the
2026-08-03 transcript are orphaned and replaced in full)

**What changed from the 2026-08-03 transcript:**
- Capsule 3 (`escalate_to_manager`) now carries `chain.relation="escalates"` instead of the
  default `"confirms"` — an escalation chained past a denial does not *confirm* that denial.
- Capsule 2 (`approve_large_order`, the denial) is **unchanged**: none of the three documented
  chain relations (`confirms` | `supersedes` | `escalates`, `capsule_emit/core.py:117`)
  genuinely describes a refusal chained to a prior, unrelated dispatch — flagged to the PM in
  `outbox.md` rather than minted as a new value at the demo level. It still reads
  `chain.relation="confirms"`.
- `action_type="act"` was attempted on `submit_order` per the original task instruction, then
  **reverted**: the reference verifier (`agent_action_capsule/verify.py:192-194`, §5.1)
  accepts only `action_type` `"fyi"` or `"decide"` — passing `"act"` makes `verify().ok` return
  `False`. `capsule_emit/core.py:126`'s docstring claiming `"act"`/`"retrieve"` as valid
  overrides is itself stale; this is flagged in outbox rather than shipping a demo with a
  capsule that fails its own verify step. `submit_order` keeps the auto-derived `"decide"`.
- `CAPSULE_ANCHOR` now defaults to `false` in both `capsule_emit/server.py` (the shipped
  extension) and `examples/goose-capsule/server.py` (the file this demo tells a Discord user to
  copy) — previously it defaulted `true` with no disclosure. This run's live anchor still fires
  because `demo.py` anchors by default independently (`--no-anchor` to opt out; see README).

---

## Live capsule IDs (leaf_index confirmed on live anchor, 2026-08-04)

| # | Capsule | capsule_id | leaf_index | tree_size | verdict | chain.relation |
|---|---------|-----------|-----------|-----------|---------|-----------------|
| — | fyi (get_price) — not anchored, informational only | `74740577180e7c13…` | — | — | executed | — |
| 1 | write_order (submit_order) | `eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48` | 259 | 260 | executed | — |
| 2 | decide (approve_large_order) **REJECTED** | `41a8e2589dc986ab77925efc4c53f7f44d5d5b6a5bcc05eff5648ae9160da4d3` | 260 | 261 | **blocked** | `confirms` (flagged — see above) |
| 3 | fyi (escalate_to_manager) | `109c6143967dc0fd97f7777ebe2866c62cda51be5d397e2436acb5861e7d4874` | 261 | 262 | executed | `escalates` |

Capsule 2 (the denial): `verdict_class=blocked`, `effect.status=planned` (the order was gated,
never dispatched), `human_disposed=true`, `approver=human` (identity `priya@acme-co.com`
carried via the `approver_id` compute-attestation extension field), reason: "order value
exceeds vendor's approved PO ceiling". Chained to capsule 1 via `chain.parent_capsule_id`.
Capsule 3 chains past the denial to capsule 2 with `chain.relation="escalates"`, proving the
chain continues after a blocked action (escalation to a human manager instead of retrying the
same order).

The `get_price` fyi capsule is sealed and offline-verified like every other capsule but is
intentionally **not** part of the live-anchored chain or the permalink set below — it mirrors
the Dapr demo's scope (anchor + permalink only the narrative chain: order → denial →
escalation).

All three chain capsules registered synchronously via `POST /v1/digest`, confirmed via
`GET /v1/inclusion/<capsule_id>` (HTTP 200, independently re-confirmed via curl — see below),
and inclusion-proven via `GET /anchor/inclusion-proof-ct`. Every capsule verified offline via
both `agent_action_capsule.verify()` and `scitt_cose.verify_receipt()` against the log's Ed25519
public key fetched from `/.well-known/did.json` — no trust in the anchor server required.

---

## Full output (live run, exit code 0)

```
============================================================
Goose capsule demo — tool call → sealed capsule → verify
============================================================

[step 1] Goose calls get_price (read-only, action_type=fyi)
  tool returned: {'vendor': 'Frobozz Supply', 'item': 'widget', 'unit_price_usd': '42.00', 'currency': 'USD'}

[step 2] Goose calls submit_order (consequential, write_order)
  tool returned: {'status': 'dispatched', 'po_number': 'PO-7777', 'vendor': 'Frobozz Supply', 'amount_usd': '1240.19', 'confirmation_ref': 'CONF-7777'}

─── Step 3 — human approval gate: large order REJECTED ────────────────
  capsule_id  : 41a8e2589dc986ab77925efc4c53f7f44d5d5b6a5bcc05eff5648ae9160da4d3
  verdict     : blocked
  approver    : human (priya@acme-co.com)
  reason      : order value exceeds vendor's approved PO ceiling
  chained to  : eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48

─── Step 4 — escalate blocked order to manager ────────────────────────
  capsule_id  : 109c6143967dc0fd97f7777ebe2866c62cda51be5d397e2436acb5861e7d4874
  chained to  : 41a8e2589dc986ab77925efc4c53f7f44d5d5b6a5bcc05eff5648ae9160da4d3

[step 5] Ledger: 4 capsule(s) sealed
  74740577180e7c13… get_price [executed] runtime=mcp
  eedf9efa25442337… submit_order [executed] runtime=mcp
  41a8e2589dc986ab… approve_large_order [blocked] runtime=mcp
  109c6143967dc0fd… escalate_to_manager [executed] runtime=mcp

[step 6] Verify all capsules (offline — no network needed)
  74740577180e7c13… ok=True  ✓
  eedf9efa25442337… ok=True  ✓
  41a8e2589dc986ab… ok=True  ✓
  109c6143967dc0fd… ok=True  ✓

  All capsules verified ok=True.

[step 7] Tamper test: flip one byte in output digest → verify fails
  original  digest:  …3ef16460
  tampered  digest:  …3ef16461
  verify result:     ok=False  findings: ['recomputed 1154d5ffc1f9718af0deaf42a41e1ace7a9cf153ca0082396c9ca621bccfeace != carried eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48']
  Tamper detected — ok=False as expected. ✓

─── Step 8 — live anchor the 3-capsule chain ──────────────────────────
  [1 write_order/submit_order] capsule_id  : eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48
  [1 write_order/submit_order] action_type : decide
  [1 write_order/submit_order] verdict     : executed
  [1 write_order/submit_order] verify().ok : True
  [1 write_order/submit_order] POST /v1/digest        HTTP 200  leaf=259 tree=260
  [1 write_order/submit_order] GET /v1/inclusion/<id> HTTP 200  root=dd2448037dffb550...
  [1 write_order/submit_order] GET /anchor/inclusion-proof-ct HTTP 200
  [1 write_order/submit_order] verify_receipt (offline) : ok=True
  [2 decide/approve_large_order(REJECTED)] capsule_id  : 41a8e2589dc986ab77925efc4c53f7f44d5d5b6a5bcc05eff5648ae9160da4d3
  [2 decide/approve_large_order(REJECTED)] action_type : decide
  [2 decide/approve_large_order(REJECTED)] verdict     : blocked
  [2 decide/approve_large_order(REJECTED)] verify().ok : True
  [2 decide/approve_large_order(REJECTED)] POST /v1/digest        HTTP 200  leaf=260 tree=261
  [2 decide/approve_large_order(REJECTED)] GET /v1/inclusion/<id> HTTP 200  root=f82259a36e598d87...
  [2 decide/approve_large_order(REJECTED)] GET /anchor/inclusion-proof-ct HTTP 200
  [2 decide/approve_large_order(REJECTED)] verify_receipt (offline) : ok=True
  [3 fyi/escalate_to_manager] capsule_id  : 109c6143967dc0fd97f7777ebe2866c62cda51be5d397e2436acb5861e7d4874
  [3 fyi/escalate_to_manager] action_type : fyi
  [3 fyi/escalate_to_manager] verdict     : executed
  [3 fyi/escalate_to_manager] verify().ok : True
  [3 fyi/escalate_to_manager] POST /v1/digest        HTTP 200  leaf=261 tree=262
  [3 fyi/escalate_to_manager] GET /v1/inclusion/<id> HTTP 200  root=d9a17ff9bc0953c5...
  [3 fyi/escalate_to_manager] GET /anchor/inclusion-proof-ct HTTP 200
  [3 fyi/escalate_to_manager] verify_receipt (offline) : ok=True

─── Step 9 — verify permalinks ────────────────────────────────────────
  [1 write_order/submit_order] leaf=259
    https://verify.agentactioncapsule.org/v/eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJlZWRmOWVmYTI1NDQyMzM3ZDI0NmMxMzk1OWM2NThmMmMzZmNlNjhmOTg1OTc5ZDQ4OGU0NTliMGFmODBhZDQ4IiwgImFjdGlvbl9pZCI6ICJzdWJtaXRfb3JkZXIvZDRlNmYwOGYtNDczYy00MTZmLWI5ZjYtMGZiYmFhN2E2MTk2IiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wNFQxOTowMzowMS45MzEzMDZaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI5YmViODU0YzE5MmVmMjE1MzkzODE2NDY3OTJiYjAzNDZkNjU3ODFhOWUyNzA1MmM0Nzc3NWFlMWIyYWJkOTIyIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWE3YTk3ZTRhNDA3MGFlNjE5MDMyODY0M2Y5MjA5ZDc0NTE1YzE5OTA0MGNkYmYxNjFkZTE2YmQzZWYxNjQ2MCIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJ3cml0ZV9vcmRlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19
  [2 decide/approve_large_order(REJECTED)] leaf=260
    https://verify.agentactioncapsule.org/v/41a8e2589dc986ab77925efc4c53f7f44d5d5b6a5bcc05eff5648ae9160da4d3#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWE4ZTI1ODlkYzk4NmFiNzc5MjVlZmM0YzUzZjdmNDRkNWQ1YjZhNWJjYzA1ZWZmNTY0OGFlOTE2MGRhNGQzIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzZlZWI4YjcxLTdjZjAtNDQ0MC1hM2E2LWFjNDM5OTRmMTE3YSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxNTE5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJlZWRmOWVmYTI1NDQyMzM3ZDI0NmMxMzk1OWM2NThmMmMzZmNlNjhmOTg1OTc5ZDQ4OGU0NTliMGFmODBhZDQ4IiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19
  [3 fyi/escalate_to_manager] leaf=261
    https://verify.agentactioncapsule.org/v/109c6143967dc0fd97f7777ebe2866c62cda51be5d397e2436acb5861e7d4874#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIxMDljNjE0Mzk2N2RjMGZkOTdmNzc3N2ViZTI4NjZjNjJjZGE1MWJlNWQzOTdlMjQzNmFjYjU4NjFlN2Q0ODc0IiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyLzQ4ZDI0ZTM1LTE3N2ItNDcwZS1hOTBhLWYzOWEwZTlmMzJiYyIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxNzA3WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiYjQ0ODRjZWUwNGE3OTdjODJlMzAwZmE1OWYzNTM3MTYzZjVlNGNiNWZiY2RkYzhhMjU4YjA3NmRlYTZmNjJiNyIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjYxYzhlYWIyMTNkM2UwMzRmNDY1YTJmNTlkYzVhNTVkMWVmYjY4ZjU5NGQyNzY4M2IwNDQzNTE2MTA0N2IzNjMiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiNDFhOGUyNTg5ZGM5ODZhYjc3OTI1ZWZjNGM1M2Y3ZjQ0ZDVkNWI2YTViY2MwNWVmZjU2NDhhZTkxNjBkYTRkMyIsICJyZWxhdGlvbiI6ICJlc2NhbGF0ZXMifX0=

  Bundle permalink (Chain Navigation table, VERDICT column executed → blocked → executed):
    https://verify.agentactioncapsule.org/v/eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiZWVkZjllZmEyNTQ0MjMzN2QyNDZjMTM5NTljNjU4ZjJjM2ZjZTY4Zjk4NTk3OWQ0ODhlNDU5YjBhZjgwYWQ0OCIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyL2Q0ZTZmMDhmLTQ3M2MtNDE2Zi1iOWY2LTBmYmJhYTdhNjE5NiIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxMzA2WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWE4ZTI1ODlkYzk4NmFiNzc5MjVlZmM0YzUzZjdmNDRkNWQ1YjZhNWJjYzA1ZWZmNTY0OGFlOTE2MGRhNGQzIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzZlZWI4YjcxLTdjZjAtNDQ0MC1hM2E2LWFjNDM5OTRmMTE3YSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxNTE5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJlZWRmOWVmYTI1NDQyMzM3ZDI0NmMxMzk1OWM2NThmMmMzZmNlNjhmOTg1OTc5ZDQ4OGU0NTliMGFmODBhZDQ4IiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjEwOWM2MTQzOTY3ZGMwZmQ5N2Y3Nzc3ZWJlMjg2NmM2MmNkYTUxYmU1ZDM5N2UyNDM2YWNiNTg2MWU3ZDQ4NzQiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvNDhkMjRlMzUtMTc3Yi00NzBlLWE5MGEtZjM5YTBlOWYzMmJjIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wNFQxOTowMzowMS45MzE3MDdaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICI0MWE4ZTI1ODlkYzk4NmFiNzc5MjVlZmM0YzUzZjdmNDRkNWQ1YjZhNWJjYzA1ZWZmNTY0OGFlOTE2MGRhNGQzIiwgInJlbGF0aW9uIjogImVzY2FsYXRlcyJ9fV0=

============================================================
Demo complete.
  ledger path: /var/folders/yg/cx7v1zqs26v1y0ys4wjhxh1m0000gn/T/tmpm517deox/goose-capsules.jsonl  (temp; deleted on exit)
  Chain: write_order → decide(BLOCKED) → fyi (escalation).
  To use with real Goose: see examples/goose-capsule/server.py
============================================================
```

---

## Independent inclusion re-confirmation (curl, after the run)

```
$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48
HTTP 200 — leaf_index=259, entry_hash=a17b41e0fbd3f5bcffd7572ecb73b72b45bacd2cb82459d51ad14e3c0bb4db76, root_hash=dd2448037dffb550fd4795edb8547309f25f81a4b3b660a5c01e836b4eb31c09

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/41a8e2589dc986ab77925efc4c53f7f44d5d5b6a5bcc05eff5648ae9160da4d3
HTTP 200 — leaf_index=260, entry_hash=28461379d56b84ea6129c50fceac880f399409fca3eff2dc09622f65fac69981, root_hash=f82259a36e598d87f6d82c0918b4dc8ff8442844f3ba2f24d046c2234963e8ef

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/109c6143967dc0fd97f7777ebe2866c62cda51be5d397e2436acb5861e7d4874
HTTP 200 — leaf_index=261, entry_hash=9ab42f9943d6aab5749f4da7c1de9ff54b42a01f54f0d1f20252a9f7f78d9bc7, root_hash=d9a17ff9bc0953c5a719977c87b8fcf603eeb8d946ed0de584c9998d4f6e3c52
```

Capsule 3's `audit_path[0]` (`daca406d23ca2d2ff78ba6b2ffa824f21516b7aaf0c1452b914f035a8ad5b22e`)
equals capsule 2's `leaf_hash` — consistent with a genuine append-only tree (the same check the
PM ran independently against the 2026-08-03 leaves).

---

## Verify permalinks (verify.agentactioncapsule.org)

Each permalink carries the full capsule JSON in the URL fragment (never sent to the server —
client-side only, per `scitt_cose/hosted.py`'s deployed JS). Every permalink below was
browser-confirmed on a fresh page load with zero manual pasting (anchor banner, digest graph,
privilege log; the bundle additionally renders the Chain Navigation table with the VERDICT
column).

**Individual capsules:**

| # | Capsule | Permalink |
|---|---------|-----------|
| 1 | write_order/submit_order | `https://verify.agentactioncapsule.org/v/eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJlZWRmOWVmYTI1NDQyMzM3ZDI0NmMxMzk1OWM2NThmMmMzZmNlNjhmOTg1OTc5ZDQ4OGU0NTliMGFmODBhZDQ4IiwgImFjdGlvbl9pZCI6ICJzdWJtaXRfb3JkZXIvZDRlNmYwOGYtNDczYy00MTZmLWI5ZjYtMGZiYmFhN2E2MTk2IiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wNFQxOTowMzowMS45MzEzMDZaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI5YmViODU0YzE5MmVmMjE1MzkzODE2NDY3OTJiYjAzNDZkNjU3ODFhOWUyNzA1MmM0Nzc3NWFlMWIyYWJkOTIyIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWE3YTk3ZTRhNDA3MGFlNjE5MDMyODY0M2Y5MjA5ZDc0NTE1YzE5OTA0MGNkYmYxNjFkZTE2YmQzZWYxNjQ2MCIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJ3cml0ZV9vcmRlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19` |
| 2 | decide/approve_large_order (REJECTED) | `https://verify.agentactioncapsule.org/v/41a8e2589dc986ab77925efc4c53f7f44d5d5b6a5bcc05eff5648ae9160da4d3#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWE4ZTI1ODlkYzk4NmFiNzc5MjVlZmM0YzUzZjdmNDRkNWQ1YjZhNWJjYzA1ZWZmNTY0OGFlOTE2MGRhNGQzIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzZlZWI4YjcxLTdjZjAtNDQ0MC1hM2E2LWFjNDM5OTRmMTE3YSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxNTE5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJlZWRmOWVmYTI1NDQyMzM3ZDI0NmMxMzk1OWM2NThmMmMzZmNlNjhmOTg1OTc5ZDQ4OGU0NTliMGFmODBhZDQ4IiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19` |
| 3 | fyi/escalate_to_manager | `https://verify.agentactioncapsule.org/v/109c6143967dc0fd97f7777ebe2866c62cda51be5d397e2436acb5861e7d4874#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIxMDljNjE0Mzk2N2RjMGZkOTdmNzc3N2ViZTI4NjZjNjJjZGE1MWJlNWQzOTdlMjQzNmFjYjU4NjFlN2Q0ODc0IiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyLzQ4ZDI0ZTM1LTE3N2ItNDcwZS1hOTBhLWYzOWEwZTlmMzJiYyIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxNzA3WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiYjQ0ODRjZWUwNGE3OTdjODJlMzAwZmE1OWYzNTM3MTYzZjVlNGNiNWZiY2RkYzhhMjU4YjA3NmRlYTZmNjJiNyIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjYxYzhlYWIyMTNkM2UwMzRmNDY1YTJmNTlkYzVhNTVkMWVmYjY4ZjU5NGQyNzY4M2IwNDQzNTE2MTA0N2IzNjMiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiNDFhOGUyNTg5ZGM5ODZhYjc3OTI1ZWZjNGM1M2Y3ZjQ0ZDVkNWI2YTViY2MwNWVmZjU2NDhhZTkxNjBkYTRkMyIsICJyZWxhdGlvbiI6ICJlc2NhbGF0ZXMifX0=` |

**Full 3-capsule chain bundle (renders the Chain Navigation table with a VERDICT column and
Previous/Next click-through):**

`https://verify.agentactioncapsule.org/v/eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiZWVkZjllZmEyNTQ0MjMzN2QyNDZjMTM5NTljNjU4ZjJjM2ZjZTY4Zjk4NTk3OWQ0ODhlNDU5YjBhZjgwYWQ0OCIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyL2Q0ZTZmMDhmLTQ3M2MtNDE2Zi1iOWY2LTBmYmJhYTdhNjE5NiIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxMzA2WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWE4ZTI1ODlkYzk4NmFiNzc5MjVlZmM0YzUzZjdmNDRkNWQ1YjZhNWJjYzA1ZWZmNTY0OGFlOTE2MGRhNGQzIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzZlZWI4YjcxLTdjZjAtNDQ0MC1hM2E2LWFjNDM5OTRmMTE3YSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDRUMTk6MDM6MDEuOTMxNTE5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJlZWRmOWVmYTI1NDQyMzM3ZDI0NmMxMzk1OWM2NThmMmMzZmNlNjhmOTg1OTc5ZDQ4OGU0NTliMGFmODBhZDQ4IiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjEwOWM2MTQzOTY3ZGMwZmQ5N2Y3Nzc3ZWJlMjg2NmM2MmNkYTUxYmU1ZDM5N2UyNDM2YWNiNTg2MWU3ZDQ4NzQiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvNDhkMjRlMzUtMTc3Yi00NzBlLWE5MGEtZjM5YTBlOWYzMmJjIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wNFQxOTowMzowMS45MzE3MDdaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICI0MWE4ZTI1ODlkYzk4NmFiNzc5MjVlZmM0YzUzZjdmNDRkNWQ1YjZhNWJjYzA1ZWZmNTY0OGFlOTE2MGRhNGQzIiwgInJlbGF0aW9uIjogImVzY2FsYXRlcyJ9fV0=`

Click sequence for the denial beat: open the bundle permalink → Chain Navigation table shows
all 3 capsules with `# / CAPSULE_ID / ACTION_TYPE / VERDICT / TIMESTAMP` → row 2 reads
`decide | blocked` → click row 2 (or "Next") to load capsule 2 standalone, which shows the
anchor banner (`✓ Anchored log index 260 · inclusion proof verified (RFC 9162)`), the digest
graph (chains_to → capsule 1, attests_over agent_input/agent_output), and the privilege log
(agent_input/agent_output both WITHHELD — digest committed, payload not carried in the record).
Row 3 reads `fyi | executed` and its `chain.relation` is `escalates`, distinct from row 2's
`confirms` — the VERDICT column and the relation are now visibly different signals.

---

## What the evidence means

**POST /v1/digest** (`HTTP 200`) registers the capsule_id (a SHA-256 content address) on the
public SCITT transparency log. The endpoint is idempotent — the same capsule_id always maps to
the same `leaf_index` and `entry_hash`.

**entry_hash** = SHA-256 of the raw capsule_id bytes. This is the offline-verify contract:
any party with the capsule_id can independently compute `sha256(bytes.fromhex(capsule_id))` and
compare it to what the anchor returned.

**GET /v1/inclusion/\<capsule_id\>** (`HTTP 200`) is the convenience lookup: given a
`capsule_id`, returns `leaf_index`, `tree_size`, `root_hash`, `audit_path`, and the signed
COSE receipt in one call.

**GET /anchor/inclusion-proof-ct** returns the RFC 6962 Merkle audit path for the given
`leaf_index` and `tree_size`. The `audit_path` hashes can be walked up to reproduce the
`root_hash` independently.

**verify_receipt (offline)** calls `scitt_cose.verify_receipt(receipt_bytes,
leaf_entry_hex=entry_hash, log_public_key_pem=pem)`. The Ed25519 log public key is fetched from
`/.well-known/did.json`. A `True` result means the COSE receipt is cryptographically valid and
the entry_hash is committed in the signed Merkle tree — no trust in the anchor server required.

**Capsule 2 chains to capsule 1, capsule 3 chains to capsule 2** via `chain.parent_capsule_id` —
a verifier can follow the full sequence: order submitted → human denial → escalation past the
denial, all three anchored, all three verified, the chain intact across the blocked action.

---

## Tamper test (still passing, unchanged behavior)

```
[step 7] Tamper test: flip one byte in output digest → verify fails
  original  digest:  …3ef16460
  tampered  digest:  …3ef16461
  verify result:     ok=False  findings: ['recomputed 1154d5ffc1f9718af0deaf42a41e1ace7a9cf153ca0082396c9ca621bccfeace != carried eedf9efa25442337d246c13959c658f2c3fce68f985979d488e459b0af80ad48']
  Tamper detected — ok=False as expected. ✓
```

Flipping one byte in the `agent_output_digest` of the `submit_order` capsule makes the
recomputed `capsule_id` disagree with the carried one — `verify().ok` flips to `False` with a
`capsule_id_mismatch`-shaped finding, exactly as before this task's changes.

---

## Test suite

```
$ python3 -m pytest -q
386 passed
```

`tests/test_goose.py` exercises `capsule_emit/server.py` (Pattern B) and `MCPCapsuleEmitter`
directly (Pattern A) — it does not import `examples/goose-capsule/demo.py` or `server.py`, so
these tests are independent of the demo-level changes in this task. 13 new tests cover
`CAPSULE_ANCHOR` env parsing (default-off; `0`/`false`/`no` any case) and that `anchor=`
correctly reaches or skips the (mocked, no-network) anchor call.

---

## Summary

| Check | Result |
|-------|--------|
| Live 3-capsule chain (order → denial → escalation) | ✓ |
| Live anchor inclusion for all 3 chain capsules (leaf 259/260/261) | ✓ |
| Genuine refusal (`verdict_class=blocked`, `human_disposed=true`, approver + reason) | ✓ |
| Capsule 3 `chain.relation="escalates"` (was defaulting to `confirms`) | ✓ |
| Capsule 2 relation gap flagged to PM, not invented | ✓ (see outbox) |
| `action_type="act"` reverted — breaks §5.1 verify(); flagged, not shipped | ✓ (see outbox) |
| `CAPSULE_ANCHOR` defaults to `false` (both `capsule_emit/server.py` and the example's) | ✓ |
| `CAPSULE_ANCHOR` env-parsing + anchor-reaches-emitter tests (offline, mocked) | ✓ |
| Individual verify permalinks (3) | ✓ |
| Bundle permalink (Chain Navigation + VERDICT column) | ✓ |
| `verify ok=True` (all 4 sealed capsules, offline) | ✓ |
| tamper → `ok=False` | ✓ |
| Full suite 386/386 green | ✓ |

**Sealed capsule (offline artifact, refreshed from this run):**
`examples/goose-capsule/evidence/capsule.json`
