# Goose Capsule Demo — Run Transcript

**Run:** `python3 examples/goose-capsule/demo.py`
**Anchor:** `https://anchor.agentactioncapsule.org` (production, live)
**Verify surface:** `https://verify.agentactioncapsule.org`
**Branch:** `demo/goose-run`
**Run date:** 2026-08-10 (regenerated for `[goose-demo-pr42-close-and-merge-prep]` — the OMIT
ruling on capsule 2's chain relation changes capsule content, so `capsule_id` and every
downstream leaf/permalink from the 2026-08-04 transcript are orphaned and replaced in full)

**What changed from the 2026-08-04 transcript:**
- Capsule 2 (`approve_large_order`, the denial) now emits with `relation=None` — the caller no
  longer asserts `chain.relation="confirms"` on a link where a human denial is chained to the
  dispatch it refuses. This closes the gap flagged in the 2026-08-04 transcript (a genuine
  refusal chained to a prior, unrelated dispatch, where none of `confirms | supersedes |
  escalates` was accurate).
- **Finding during implementation:** `agent_action_capsule`'s `chain.relation` is a
  required, non-empty string (§5.4.4, enforced by `Chain.__post_init__` raising
  `InvariantError` on empty) — the reference library has no concept of an omitted relation on
  an existing chain link. Passing `chain_relation=None` through to `agent_action_capsule.emit()`
  does not leave the field empty; it falls back to the library's own generic
  no-explicit-relation default, `"sequence"` (the adapter-tier default, since `tool_name` is
  set on every capsule-emit call). So capsule 2's sealed `chain.relation` reads `"sequence"`,
  not an absent field — the closest achievable approximation to "no relation asserted" within
  capsule-emit's boundary, using an existing library-defined value rather than inventing one.
  True field-level omission would require a change to `agent_action_capsule` itself (out of
  scope for this repo). This is additional motivating evidence for the spec issue filed
  alongside this task (see outbox).
- Capsule 3 (`escalate_to_manager`) is unchanged: still `chain.relation="escalates"`.
- `action_type="act"` remains reverted (verified, not redone) — only `"fyi"`/`"decide"` appear
  anywhere in `demo.py`.
- `CAPSULE_ANCHOR` still defaults to `false` in both `capsule_emit/server.py` and
  `examples/goose-capsule/server.py` — this run's live anchor still fires because `demo.py`
  anchors by default independently (`--no-anchor` to opt out; see README).

---

## Live capsule IDs (leaf_index confirmed on live anchor, 2026-08-10)

| # | Capsule | capsule_id | leaf_index | tree_size | verdict | chain.relation |
|---|---------|-----------|-----------|-----------|---------|-----------------|
| — | fyi (get_price) — not anchored, informational only | `a102e4172fb4bafa…` | — | — | executed | — |
| 1 | write_order (submit_order) | `c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157` | 264 | 265 | executed | — |
| 2 | decide (approve_large_order) **REJECTED** | `90b7e43e16a7262ba26d973121d9e7496c1504186f64fd3d2694053a25b5e668` | 265 | 266 | **blocked** | `sequence` (OMIT ruling — see above) |
| 3 | fyi (escalate_to_manager) | `98e2fc030452188683509ccef07d773facddcf555bd4ec5243304df3f70c0ee3` | 266 | 267 | executed | `escalates` |

Capsule 2 (the denial): `verdict_class=blocked`, `effect.status=planned` (the order was gated,
never dispatched), `human_disposed=true`, `approver=human` (identity `priya@acme-co.com`
carried via the `approver_id` compute-attestation extension field), reason: "order value
exceeds vendor's approved PO ceiling". Chained to capsule 1 via `chain.parent_capsule_id`,
with no `"confirms"` assertion on the link.
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
  capsule_id  : 90b7e43e16a7262ba26d973121d9e7496c1504186f64fd3d2694053a25b5e668
  verdict     : blocked
  approver    : human (priya@acme-co.com)
  reason      : order value exceeds vendor's approved PO ceiling
  chained to  : c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157

─── Step 4 — escalate blocked order to manager ────────────────────────
  capsule_id  : 98e2fc030452188683509ccef07d773facddcf555bd4ec5243304df3f70c0ee3
  chained to  : 90b7e43e16a7262ba26d973121d9e7496c1504186f64fd3d2694053a25b5e668

[step 5] Ledger: 4 capsule(s) sealed
  a102e4172fb4bafa… get_price [executed] runtime=mcp
  c523eafd0c0b5f8e… submit_order [executed] runtime=mcp
  90b7e43e16a7262b… approve_large_order [blocked] runtime=mcp
  98e2fc0304521886… escalate_to_manager [executed] runtime=mcp

[step 6] Verify all capsules (offline — no network needed)
  a102e4172fb4bafa… ok=True  ✓
  c523eafd0c0b5f8e… ok=True  ✓
  90b7e43e16a7262b… ok=True  ✓
  98e2fc0304521886… ok=True  ✓

  All capsules verified ok=True.

[step 7] Tamper test: flip one byte in output digest → verify fails
  original  digest:  …3ef16460
  tampered  digest:  …3ef16461
  verify result:     ok=False  findings: ['recomputed 27a62da8a535e3498c8562813e6a4cacd975ef339b5e644027701d83396cd8f5 != carried c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157']
  Tamper detected — ok=False as expected. ✓

─── Step 8 — live anchor the 3-capsule chain ──────────────────────────
  [1 write_order/submit_order] capsule_id  : c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157
  [1 write_order/submit_order] action_type : decide
  [1 write_order/submit_order] verdict     : executed
  [1 write_order/submit_order] verify().ok : True
  [1 write_order/submit_order] POST /v1/digest        HTTP 200  leaf=264 tree=265
  [1 write_order/submit_order] GET /v1/inclusion/<id> HTTP 200  root=6591584882dd82a8...
  [1 write_order/submit_order] GET /anchor/inclusion-proof-ct HTTP 200
  [1 write_order/submit_order] verify_receipt (offline) : ok=True
  [2 decide/approve_large_order(REJECTED)] capsule_id  : 90b7e43e16a7262ba26d973121d9e7496c1504186f64fd3d2694053a25b5e668
  [2 decide/approve_large_order(REJECTED)] action_type : decide
  [2 decide/approve_large_order(REJECTED)] verdict     : blocked
  [2 decide/approve_large_order(REJECTED)] verify().ok : True
  [2 decide/approve_large_order(REJECTED)] POST /v1/digest        HTTP 200  leaf=265 tree=266
  [2 decide/approve_large_order(REJECTED)] GET /v1/inclusion/<id> HTTP 200  root=83ba27252bef1e3c...
  [2 decide/approve_large_order(REJECTED)] GET /anchor/inclusion-proof-ct HTTP 200
  [2 decide/approve_large_order(REJECTED)] verify_receipt (offline) : ok=True
  [3 fyi/escalate_to_manager] capsule_id  : 98e2fc030452188683509ccef07d773facddcf555bd4ec5243304df3f70c0ee3
  [3 fyi/escalate_to_manager] action_type : fyi
  [3 fyi/escalate_to_manager] verdict     : executed
  [3 fyi/escalate_to_manager] verify().ok : True
  [3 fyi/escalate_to_manager] POST /v1/digest        HTTP 200  leaf=266 tree=267
  [3 fyi/escalate_to_manager] GET /v1/inclusion/<id> HTTP 200  root=9a5aea7d835b2bb4...
  [3 fyi/escalate_to_manager] GET /anchor/inclusion-proof-ct HTTP 200
  [3 fyi/escalate_to_manager] verify_receipt (offline) : ok=True

─── Step 9 — verify permalinks ────────────────────────────────────────
  [1 write_order/submit_order] leaf=264
    https://verify.agentactioncapsule.org/v/c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJjNTIzZWFmZDBjMGI1ZjhlOWU5NDE4ZjI0NGM1MWVjZWZiZjUzZjMzOWY2ZTc5NmZjYWY3ZWM3NjNkM2FmMTU3IiwgImFjdGlvbl9pZCI6ICJzdWJtaXRfb3JkZXIvNjQ4NTNmYTItOWQxNC00NGZkLWFlZmQtY2VkMjUxYWJlYWUwIiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMDozOTozMy4zNzk2MzVaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI5YmViODU0YzE5MmVmMjE1MzkzODE2NDY3OTJiYjAzNDZkNjU3ODFhOWUyNzA1MmM0Nzc3NWFlMWIyYWJkOTIyIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWE3YTk3ZTRhNDA3MGFlNjE5MDMyODY0M2Y5MjA5ZDc0NTE1YzE5OTA0MGNkYmYxNjFkZTE2YmQzZWYxNjQ2MCIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJ3cml0ZV9vcmRlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19
  [2 decide/approve_large_order(REJECTED)] leaf=265
    https://verify.agentactioncapsule.org/v/90b7e43e16a7262ba26d973121d9e7496c1504186f64fd3d2694053a25b5e668#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI5MGI3ZTQzZTE2YTcyNjJiYTI2ZDk3MzEyMWQ5ZTc0OTZjMTUwNDE4NmY2NGZkM2QyNjk0MDUzYTI1YjVlNjY4IiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2UxM2RiODQyLTczMDAtNGQ4NS05Yjk4LTMzMjcxMDM2OWZmYSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjA6Mzk6MzMuMzc5OTIzWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJjNTIzZWFmZDBjMGI1ZjhlOWU5NDE4ZjI0NGM1MWVjZWZiZjUzZjMzOWY2ZTc5NmZjYWY3ZWM3NjNkM2FmMTU3IiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19
  [3 fyi/escalate_to_manager] leaf=266
    https://verify.agentactioncapsule.org/v/98e2fc030452188683509ccef07d773facddcf555bd4ec5243304df3f70c0ee3#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI5OGUyZmMwMzA0NTIxODg2ODM1MDljY2VmMDdkNzczZmFjZGRjZjU1NWJkNGVjNTI0MzMwNGRmM2Y3MGMwZWUzIiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyL2NjMjZjODg2LTdhYzUtNGM3Ni1hZDQzLTk3NWFjZGUwZDQ1MyIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjA6Mzk6MzMuMzgwMTc0WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiYjQ0ODRjZWUwNGE3OTdjODJlMzAwZmE1OWYzNTM3MTYzZjVlNGNiNWZiY2RkYzhhMjU4YjA3NmRlYTZmNjJiNyIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjYxYzhlYWIyMTNkM2UwMzRmNDY1YTJmNTlkYzVhNTVkMWVmYjY4ZjU5NGQyNzY4M2IwNDQzNTE2MTA0N2IzNjMiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiOTBiN2U0M2UxNmE3MjYyYmEyNmQ5NzMxMjFkOWU3NDk2YzE1MDQxODZmNjRmZDNkMjY5NDA1M2EyNWI1ZTY2OCIsICJyZWxhdGlvbiI6ICJlc2NhbGF0ZXMifX0=

  Bundle permalink (Chain Navigation table, VERDICT column executed → blocked → executed):
    https://verify.agentactioncapsule.org/v/c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiYzUyM2VhZmQwYzBiNWY4ZTllOTQxOGYyNDRjNTFlY2VmYmY1M2YzMzlmNmU3OTZmY2FmN2VjNzYzZDNhZjE1NyIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyLzY0ODUzZmEyLTlkMTQtNDRmZC1hZWZkLWNlZDI1MWFiZWFlMCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjA6Mzk6MzMuMzc5NjM1WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI5MGI3ZTQzZTE2YTcyNjJiYTI2ZDk3MzEyMWQ5ZTc0OTZjMTUwNDE4NmY2NGZkM2QyNjk0MDUzYTI1YjVlNjY4IiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2UxM2RiODQyLTczMDAtNGQ4NS05Yjk4LTMzMjcxMDM2OWZmYSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjA6Mzk6MzMuMzc5OTIzWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJjNTIzZWFmZDBjMGI1ZjhlOWU5NDE4ZjI0NGM1MWVjZWZiZjUzZjMzOWY2ZTc5NmZjYWY3ZWM3NjNkM2FmMTU3IiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjk4ZTJmYzAzMDQ1MjE4ODY4MzUwOWNjZWYwN2Q3NzNmYWNkZGNmNTU1YmQ0ZWM1MjQzMzA0ZGYzZjcwYzBlZTMiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvY2MyNmM4ODYtN2FjNS00Yzc2LWFkNDMtOTc1YWNkZTBkNDUzIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMDozOTozMy4zODAxNzRaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICI5MGI3ZTQzZTE2YTcyNjJiYTI2ZDk3MzEyMWQ5ZTc0OTZjMTUwNDE4NmY2NGZkM2QyNjk0MDUzYTI1YjVlNjY4IiwgInJlbGF0aW9uIjogImVzY2FsYXRlcyJ9fV0=

============================================================
Demo complete.
  ledger path: /var/folders/yg/cx7v1zqs26v1y0ys4wjhxh1m0000gn/T/tmpo0s9sci6/goose-capsules.jsonl  (temp; deleted on exit)
  Chain: write_order → decide(BLOCKED) → fyi (escalation).
  To use with real Goose: see examples/goose-capsule/server.py
============================================================
```

---

## Independent inclusion re-confirmation (curl, after the run)

```
$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157
HTTP 200 — leaf_index=264, tree_size=265, entry_hash=17cca0c1cd3d762679093b76cfa23b82d2a593c1e9efb7ae51e9af431ce04922, leaf_hash=7d277240bd766810892c245ad9e2b4e813e4345aac17157b30b1d6cbc65bde0c, root_hash=6591584882dd82a8fa6a15ef7279ebb826b88fe873397f6f220f295a80fbdf92

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/90b7e43e16a7262ba26d973121d9e7496c1504186f64fd3d2694053a25b5e668
HTTP 200 — leaf_index=265, tree_size=266, entry_hash=896ba3d699440e538bffdcf6b2f3365893f092364395a375647345cd8d3eb3b3, leaf_hash=404a52eb463e0f48b3c109847e6280735c428db9af389b91a833deeb3145fd50, root_hash=83ba27252bef1e3cfc8b366564a9ec6defecc71b6ff646e78120e6a0588321a4

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/98e2fc030452188683509ccef07d773facddcf555bd4ec5243304df3f70c0ee3
HTTP 200 — leaf_index=266, tree_size=267, entry_hash=85fa759facddae4678dae5cc0055fbc8bfaf1e2e07094f0e83b12df9e4469cf8, leaf_hash=9458292729bf49dea7f7919c15c610541a05f866ed22ca32302a9e0cc8bbc50b, root_hash=9a5aea7d835b2bb49687404e64029b574d392a3520c4027e90958976d11da389
```

`leaf_index` progresses 264 → 265 → 266 and `tree_size` 265 → 266 → 267, matching the
sequential order of the three `POST /v1/digest` calls above — an independent confirmation the
capsules landed in the shared public log in chain order.

Capsule 2's `audit_path[0]` (`7d277240bd766810892c245ad9e2b4e813e4345aac17157b30b1d6cbc65bde0c`)
equals capsule 1's `leaf_hash` exactly — the direct sibling-leaf check, valid here because leaf
264 (even) and leaf 265 (odd) are RFC 9162 tree siblings at the base level.

Capsule 3's `audit_path[0]` (`43c546c584b4183ac05456d39161da641ada00433d7a88fb681b1bf2ff14d7f7`)
does **not** equal capsule 2's `leaf_hash` directly — leaf 266 (even) is not a base-level
sibling of leaf 265 (odd is its own left half); RFC 9162 folds leaves 264+265 into one interior
node first. Recomputing that interior node independently confirms this is exactly the expected
value, not a discrepancy:

```
$ python3 -c "
import hashlib
lh1 = bytes.fromhex('7d277240bd766810892c245ad9e2b4e813e4345aac17157b30b1d6cbc65bde0c')  # capsule 1 leaf_hash
lh2 = bytes.fromhex('404a52eb463e0f48b3c109847e6280735c428db9af389b91a833deeb3145fd50')  # capsule 2 leaf_hash
print(hashlib.sha256(b'\x01' + lh1 + lh2).hexdigest())
"
43c546c584b4183ac05456d39161da641ada00433d7a88fb681b1bf2ff14d7f7   # == capsule 3's audit_path[0]
```

This is a stronger, from-first-principles independent check than the direct-sibling comparison
alone (SHA-256 over the RFC 9162 interior-node prefix `0x01`, combining capsule 1's and
capsule 2's leaf hashes) — it confirms the shared public log's Merkle tree shape is internally
consistent for this chain, using nothing but `curl` and `hashlib`, no capsule-emit code.

---

## Verify permalinks (verify.agentactioncapsule.org)

Each permalink carries the full capsule JSON in the URL fragment (never sent to the server —
client-side only, per `scitt_cose/hosted.py`'s deployed JS). Every permalink below —
individual and bundle — was browser-confirmed to auto-load directly from the URL fragment on
first page load, with zero manual pasting (anchor banner, digest graph, privilege log; the
bundle additionally renders the Chain Navigation table with the VERDICT column: `decide |
executed` → `decide | blocked` → `fyi | executed`, matching the README's claim exactly).

**Caveat found during this browser pass:** the bundle permalink's page also renders a separate
"Verification Ritual" integrity panel above the Chain Navigation table, and that panel reports a
false-positive `capsule_id_mismatch` for record #2 specifically — even though record #2 verifies
cleanly both standalone (its own individual permalink shows `✓ verifies`, no discrepancy) and via
`agent_action_capsule.verify()` offline (step 6 above). This reproduces identically on the
untouched 2026-08-04 bundle permalink (same failure, same record position, different capsule
IDs/relation value) — it is a pre-existing bug in the verify-site's bundle-mode digest
recomputation, not caused by this task's changes, and not something a `capsule-emit`-side fix can
address (out of this repo's boundary). Reported in the outbox for awareness.

**Individual capsules:**

| # | Capsule | Permalink |
|---|---------|-----------|
| 1 | write_order/submit_order | `https://verify.agentactioncapsule.org/v/c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJjNTIzZWFmZDBjMGI1ZjhlOWU5NDE4ZjI0NGM1MWVjZWZiZjUzZjMzOWY2ZTc5NmZjYWY3ZWM3NjNkM2FmMTU3IiwgImFjdGlvbl9pZCI6ICJzdWJtaXRfb3JkZXIvNjQ4NTNmYTItOWQxNC00NGZkLWFlZmQtY2VkMjUxYWJlYWUwIiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMDozOTozMy4zNzk2MzVaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI5YmViODU0YzE5MmVmMjE1MzkzODE2NDY3OTJiYjAzNDZkNjU3ODFhOWUyNzA1MmM0Nzc3NWFlMWIyYWJkOTIyIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWE3YTk3ZTRhNDA3MGFlNjE5MDMyODY0M2Y5MjA5ZDc0NTE1YzE5OTA0MGNkYmYxNjFkZTE2YmQzZWYxNjQ2MCIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJ3cml0ZV9vcmRlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19` |
| 2 | decide/approve_large_order (REJECTED) | `https://verify.agentactioncapsule.org/v/90b7e43e16a7262ba26d973121d9e7496c1504186f64fd3d2694053a25b5e668#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI5MGI3ZTQzZTE2YTcyNjJiYTI2ZDk3MzEyMWQ5ZTc0OTZjMTUwNDE4NmY2NGZkM2QyNjk0MDUzYTI1YjVlNjY4IiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2UxM2RiODQyLTczMDAtNGQ4NS05Yjk4LTMzMjcxMDM2OWZmYSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjA6Mzk6MzMuMzc5OTIzWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJjNTIzZWFmZDBjMGI1ZjhlOWU5NDE4ZjI0NGM1MWVjZWZiZjUzZjMzOWY2ZTc5NmZjYWY3ZWM3NjNkM2FmMTU3IiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19` |
| 3 | fyi/escalate_to_manager | `https://verify.agentactioncapsule.org/v/98e2fc030452188683509ccef07d773facddcf555bd4ec5243304df3f70c0ee3#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI5OGUyZmMwMzA0NTIxODg2ODM1MDljY2VmMDdkNzczZmFjZGRjZjU1NWJkNGVjNTI0MzMwNGRmM2Y3MGMwZWUzIiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyL2NjMjZjODg2LTdhYzUtNGM3Ni1hZDQzLTk3NWFjZGUwZDQ1MyIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjA6Mzk6MzMuMzgwMTc0WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiYjQ0ODRjZWUwNGE3OTdjODJlMzAwZmE1OWYzNTM3MTYzZjVlNGNiNWZiY2RkYzhhMjU4YjA3NmRlYTZmNjJiNyIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjYxYzhlYWIyMTNkM2UwMzRmNDY1YTJmNTlkYzVhNTVkMWVmYjY4ZjU5NGQyNzY4M2IwNDQzNTE2MTA0N2IzNjMiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiOTBiN2U0M2UxNmE3MjYyYmEyNmQ5NzMxMjFkOWU3NDk2YzE1MDQxODZmNjRmZDNkMjY5NDA1M2EyNWI1ZTY2OCIsICJyZWxhdGlvbiI6ICJlc2NhbGF0ZXMifX0=` |

**Full 3-capsule chain bundle (renders the Chain Navigation table with a VERDICT column and
Previous/Next click-through):**

`https://verify.agentactioncapsule.org/v/c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiYzUyM2VhZmQwYzBiNWY4ZTllOTQxOGYyNDRjNTFlY2VmYmY1M2YzMzlmNmU3OTZmY2FmN2VjNzYzZDNhZjE1NyIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyLzY0ODUzZmEyLTlkMTQtNDRmZC1hZWZkLWNlZDI1MWFiZWFlMCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjA6Mzk6MzMuMzc5NjM1WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI5MGI3ZTQzZTE2YTcyNjJiYTI2ZDk3MzEyMWQ5ZTc0OTZjMTUwNDE4NmY2NGZkM2QyNjk0MDUzYTI1YjVlNjY4IiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2UxM2RiODQyLTczMDAtNGQ4NS05Yjk4LTMzMjcxMDM2OWZmYSIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjA6Mzk6MzMuMzc5OTIzWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJjNTIzZWFmZDBjMGI1ZjhlOWU5NDE4ZjI0NGM1MWVjZWZiZjUzZjMzOWY2ZTc5NmZjYWY3ZWM3NjNkM2FmMTU3IiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjk4ZTJmYzAzMDQ1MjE4ODY4MzUwOWNjZWYwN2Q3NzNmYWNkZGNmNTU1YmQ0ZWM1MjQzMzA0ZGYzZjcwYzBlZTMiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvY2MyNmM4ODYtN2FjNS00Yzc2LWFkNDMtOTc1YWNkZTBkNDUzIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMDozOTozMy4zODAxNzRaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICI5MGI3ZTQzZTE2YTcyNjJiYTI2ZDk3MzEyMWQ5ZTc0OTZjMTUwNDE4NmY2NGZkM2QyNjk0MDUzYTI1YjVlNjY4IiwgInJlbGF0aW9uIjogImVzY2FsYXRlcyJ9fV0=`

Click sequence for the denial beat: open the bundle permalink → Chain Navigation table shows
all 3 capsules with `# / CAPSULE_ID / ACTION_TYPE / VERDICT / TIMESTAMP` → row 2 reads
`decide | blocked` → click row 2 (or "Next") to load capsule 2 standalone, which shows the
anchor banner (`✓ Anchored log index 265 · inclusion proof verified (RFC 9162)`), the digest
graph (chains_to → capsule 1, attests_over agent_input/agent_output), and the privilege log
(agent_input/agent_output both WITHHELD — digest committed, payload not carried in the record).
Row 2's `chain.relation` now reads `sequence`, not `confirms` — no relation is asserted on the
denial's link to the dispatch it refuses. Row 3 reads `fyi | executed` and its `chain.relation`
is `escalates`, distinct from row 2's `sequence`.

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
  verify result:     ok=False  findings: ['recomputed 27a62da8a535e3498c8562813e6a4cacd975ef339b5e644027701d83396cd8f5 != carried c523eafd0c0b5f8e9e9418f244c51ecefbf53f339f6e796fcaf7ec763d3af157']
  Tamper detected — ok=False as expected. ✓
```

Flipping one byte in the `agent_output_digest` of the `submit_order` capsule makes the
recomputed `capsule_id` disagree with the carried one — `verify().ok` flips to `False` with a
`capsule_id_mismatch`-shaped finding, exactly as before this task's changes.

---

## Test suite

```
$ python3 -m pytest -q --ignore=tests/test_agentgateway.py
386 passed
```

`tests/test_agentgateway.py` fails to collect locally due to an unrelated protobuf
gencode/runtime version mismatch in this environment (pre-existing, reproduced identically on
`HEAD~0` before this task's changes) — not part of this task's scope. Two new tests
(`test_relation_none_keeps_chain_without_confirms_assertion`,
`test_relation_none_does_not_raise_without_confirms` in
`tests/test_producer_hardening.py`) cover the `relation=None` behavior added by this task.

---

## Summary

| Check | Result |
|-------|--------|
| Live 3-capsule chain (order → denial → escalation) | ✓ |
| Live anchor inclusion for all 3 chain capsules (leaf 264/265/266) | ✓ |
| Genuine refusal (`verdict_class=blocked`, `human_disposed=true`, approver + reason) | ✓ |
| Capsule 2 chain relation OMIT ruling applied (`relation=None` → sealed as `sequence`, not `confirms`) | ✓ |
| Capsule 3 `chain.relation="escalates"` (unchanged) | ✓ |
| `action_type="act"` revert still in effect — only `fyi`/`decide` appear | ✓ (verified, not redone) |
| `CAPSULE_ANCHOR` defaults to `false` (both `capsule_emit/server.py` and the example's) | ✓ (verified, not redone) |
| Independent curl re-verification of `leaf_index`/`tree_size` progression + audit path | ✓ |
| Individual verify permalinks (3) | ✓ |
| Bundle permalink (Chain Navigation + VERDICT column) | ✓ |
| `verify ok=True` (all 4 sealed capsules, offline) | ✓ |
| tamper → `ok=False` | ✓ |
| Full suite 386/386 green (excl. pre-existing local protobuf collection error) | ✓ |

**Sealed capsule (offline artifact, refreshed from this run):**
`examples/goose-capsule/evidence/capsule.json`
