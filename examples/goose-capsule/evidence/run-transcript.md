# Goose Capsule Demo — Run Transcript

**Run:** `python3 examples/goose-capsule/demo.py`
**Anchor:** `https://anchor.agentactioncapsule.org` (production, live)
**Verify surface:** `https://verify.agentactioncapsule.org`
**Branch:** `demo/goose-run`
**Run date:** 2026-08-03 (regenerated — the previous 2026-06-30 transcript predated live
anchoring, permalinks, and the refusal chain; it is replaced in full below)

**What changed from the 2026-06-30 transcript:** that run exercised `capsule_emit.server`
(Pattern B, a companion MCP server) with a single unchained `submit_order`-style capsule and
no anchor evidence. This run exercises `examples/goose-capsule/demo.py` directly (Pattern A —
the same `MCPCapsuleEmitter` path Goose's own `po-agent` extension uses), producing a
**3-capsule chain with a genuine human denial**, live SCITT anchor inclusion for every capsule
in the chain, and verify permalinks (individual + bundle). This is the Dapr-parity run —
mirrors `examples/dapr-agents-capsule/demo.py`'s evidence shape.

**Editable-install note:** the global `capsule-emit` editable install was pointed at a
different, unrelated worktree (`_worktrees/capsule-emit/dapr-agents-demo-run`) before this run.
Re-pointed with `pip install -e .` from this worktree and confirmed
`import capsule_emit; capsule_emit.__file__` resolves here before trusting any output below.

---

## Live capsule IDs (leaf_index confirmed on live anchor, 2026-08-03)

| # | Capsule | capsule_id | leaf_index | tree_size | verdict |
|---|---------|-----------|-----------|-----------|---------|
| — | fyi (get_price) — not anchored, informational only | `874dbe8a0301052e…` | — | — | executed |
| 1 | write_order (submit_order) | `ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50` | 251 | 252 | executed |
| 2 | decide (approve_large_order) **REJECTED** | `41edf88007c592e930b5929725247539af87e9fa29a9cb0c8c95cef8b52942f7` | 252 | 253 | **blocked** |
| 3 | fyi (escalate_to_manager) | `bf3d94f6933e6a85f248da1dc05e6a3519f97f4ea8103b1e3b8a1ffecba2fe36` | 253 | 254 | executed |

Capsule 2 (the denial): `verdict_class=blocked`, `effect.status=planned` (the order was gated,
never dispatched), `human_disposed=true`, `approver=human` (identity `priya@acme-co.com`
carried via the `approver_id` compute-attestation extension field), reason: "order value
exceeds vendor's approved PO ceiling". Chained to capsule 1 via `chain.parent_capsule_id`.
Capsule 3 chains past the denial to capsule 2, proving the chain continues after a blocked
action (escalation to a human manager instead of retrying the same order).

The `get_price` fyi capsule (unchanged from the prior demo) is sealed and offline-verified
like every other capsule but is intentionally **not** part of the live-anchored chain or the
permalink set below — it mirrors the Dapr demo's scope (anchor + permalink only the narrative
chain: order → denial → escalation).

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
  capsule_id  : 41edf88007c592e930b5929725247539af87e9fa29a9cb0c8c95cef8b52942f7
  verdict     : blocked
  approver    : human (priya@acme-co.com)
  reason      : order value exceeds vendor's approved PO ceiling
  chained to  : ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50

─── Step 4 — escalate blocked order to manager ────────────────────────
  capsule_id  : bf3d94f6933e6a85f248da1dc05e6a3519f97f4ea8103b1e3b8a1ffecba2fe36
  chained to  : 41edf88007c592e930b5929725247539af87e9fa29a9cb0c8c95cef8b52942f7

[step 5] Ledger: 4 capsule(s) sealed
  874dbe8a0301052e… get_price [executed] runtime=mcp
  ba2ce5bf3f937009… submit_order [executed] runtime=mcp
  41edf88007c592e9… approve_large_order [blocked] runtime=mcp
  bf3d94f6933e6a85… escalate_to_manager [executed] runtime=mcp

[step 6] Verify all capsules (offline — no network needed)
  874dbe8a0301052e… ok=True  ✓
  ba2ce5bf3f937009… ok=True  ✓
  41edf88007c592e9… ok=True  ✓
  bf3d94f6933e6a85… ok=True  ✓

  All capsules verified ok=True.

[step 7] Tamper test: flip one byte in output digest → verify fails
  original  digest:  …3ef16460
  tampered  digest:  …3ef16461
  verify result:     ok=False  findings: ['recomputed 2cc7748b60ba0dcf527afe56bc68d2b63816d615362d6d6ce09fc0c69da2db44 != carried ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50']
  Tamper detected — ok=False as expected. ✓

─── Step 8 — live anchor the 3-capsule chain ──────────────────────────
  [1 write_order/submit_order] capsule_id  : ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50
  [1 write_order/submit_order] action_type : decide
  [1 write_order/submit_order] verdict     : executed
  [1 write_order/submit_order] verify().ok : True
  [1 write_order/submit_order] POST /v1/digest        HTTP 200  leaf=251 tree=252
  [1 write_order/submit_order] GET /v1/inclusion/<id> HTTP 200  root=3d7f715f47c45b54...
  [1 write_order/submit_order] GET /anchor/inclusion-proof-ct HTTP 200
  [1 write_order/submit_order] verify_receipt (offline) : ok=True
  [2 decide/approve_large_order(REJECTED)] capsule_id  : 41edf88007c592e930b5929725247539af87e9fa29a9cb0c8c95cef8b52942f7
  [2 decide/approve_large_order(REJECTED)] action_type : decide
  [2 decide/approve_large_order(REJECTED)] verdict     : blocked
  [2 decide/approve_large_order(REJECTED)] verify().ok : True
  [2 decide/approve_large_order(REJECTED)] POST /v1/digest        HTTP 200  leaf=252 tree=253
  [2 decide/approve_large_order(REJECTED)] GET /v1/inclusion/<id> HTTP 200  root=431bf68610c8629e...
  [2 decide/approve_large_order(REJECTED)] GET /anchor/inclusion-proof-ct HTTP 200
  [2 decide/approve_large_order(REJECTED)] verify_receipt (offline) : ok=True
  [3 fyi/escalate_to_manager] capsule_id  : bf3d94f6933e6a85f248da1dc05e6a3519f97f4ea8103b1e3b8a1ffecba2fe36
  [3 fyi/escalate_to_manager] action_type : fyi
  [3 fyi/escalate_to_manager] verdict     : executed
  [3 fyi/escalate_to_manager] verify().ok : True
  [3 fyi/escalate_to_manager] POST /v1/digest        HTTP 200  leaf=253 tree=254
  [3 fyi/escalate_to_manager] GET /v1/inclusion/<id> HTTP 200  root=ddd8a3d25506d37f...
  [3 fyi/escalate_to_manager] GET /anchor/inclusion-proof-ct HTTP 200
  [3 fyi/escalate_to_manager] verify_receipt (offline) : ok=True

─── Step 9 — verify permalinks ────────────────────────────────────────
  [1 write_order/submit_order] leaf=251
    https://verify.agentactioncapsule.org/v/ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJiYTJjZTViZjNmOTM3MDA5ZDViZjFhODc5YzVjMGNiOTgzZjAyMjcyYjA5Y2E2NzA5NmUwODQ4MmU1MTZjYzUwIiwgImFjdGlvbl9pZCI6ICJzdWJtaXRfb3JkZXIvMjczNjM1MWUtY2VjNi00Y2Q1LWI3MDMtMmIyMWQwMmI1MDQwIiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wM1QyMTowNjo1NC42ODU4NjdaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI5YmViODU0YzE5MmVmMjE1MzkzODE2NDY3OTJiYjAzNDZkNjU3ODFhOWUyNzA1MmM0Nzc3NWFlMWIyYWJkOTIyIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWE3YTk3ZTRhNDA3MGFlNjE5MDMyODY0M2Y5MjA5ZDc0NTE1YzE5OTA0MGNkYmYxNjFkZTE2YmQzZWYxNjQ2MCIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJ3cml0ZV9vcmRlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19
  [2 decide/approve_large_order(REJECTED)] leaf=252
    https://verify.agentactioncapsule.org/v/41edf88007c592e930b5929725247539af87e9fa29a9cb0c8c95cef8b52942f7#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWVkZjg4MDA3YzU5MmU5MzBiNTkyOTcyNTI0NzUzOWFmODdlOWZhMjlhOWNiMGM4Yzk1Y2VmOGI1Mjk0MmY3IiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzUxN2QzMDI5LWI1M2UtNGY4NC05Y2ZhLTNmY2EyOGZhYWI4MiIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMjE6MDY6NTQuNjg2MTU5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJiYTJjZTViZjNmOTM3MDA5ZDViZjFhODc5YzVjMGNiOTgzZjAyMjcyYjA5Y2E2NzA5NmUwODQ4MmU1MTZjYzUwIiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19
  [3 fyi/escalate_to_manager] leaf=253
    https://verify.agentactioncapsule.org/v/bf3d94f6933e6a85f248da1dc05e6a3519f97f4ea8103b1e3b8a1ffecba2fe36#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJiZjNkOTRmNjkzM2U2YTg1ZjI0OGRhMWRjMDVlNmEzNTE5Zjk3ZjRlYTgxMDNiMWUzYjhhMWZmZWNiYTJmZTM2IiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyLzA5MGY1NGRiLTFiOTktNGJkZC1hYWJiLTBhOTU5Mjg2NjUwYiIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMjE6MDY6NTQuNjg2NDE1WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiYjQ0ODRjZWUwNGE3OTdjODJlMzAwZmE1OWYzNTM3MTYzZjVlNGNiNWZiY2RkYzhhMjU4YjA3NmRlYTZmNjJiNyIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjYxYzhlYWIyMTNkM2UwMzRmNDY1YTJmNTlkYzVhNTVkMWVmYjY4ZjU5NGQyNzY4M2IwNDQzNTE2MTA0N2IzNjMiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiNDFlZGY4ODAwN2M1OTJlOTMwYjU5Mjk3MjUyNDc1MzlhZjg3ZTlmYTI5YTljYjBjOGM5NWNlZjhiNTI5NDJmNyIsICJyZWxhdGlvbiI6ICJjb25maXJtcyJ9fQ==

  Bundle permalink (Chain Navigation table, VERDICT column executed → blocked → executed):
    https://verify.agentactioncapsule.org/v/ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiYmEyY2U1YmYzZjkzNzAwOWQ1YmYxYTg3OWM1YzBjYjk4M2YwMjI3MmIwOWNhNjcwOTZlMDg0ODJlNTE2Y2M1MCIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyLzI3MzYzNTFlLWNlYzYtNGNkNS1iNzAzLTJiMjFkMDJiNTA0MCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMjE6MDY6NTQuNjg1ODY3WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWVkZjg4MDA3YzU5MmU5MzBiNTkyOTcyNTI0NzUzOWFmODdlOWZhMjlhOWNiMGM4Yzk1Y2VmOGI1Mjk0MmY3IiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzUxN2QzMDI5LWI1M2UtNGY4NC05Y2ZhLTNmY2EyOGZhYWI4MiIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMjE6MDY6NTQuNjg2MTU5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJiYTJjZTViZjNmOTM3MDA5ZDViZjFhODc5YzVjMGNiOTgzZjAyMjcyYjA5Y2E2NzA5NmUwODQ4MmU1MTZjYzUwIiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogImJmM2Q5NGY2OTMzZTZhODVmMjQ4ZGExZGMwNWU2YTM1MTlmOTdmNGVhODEwM2IxZTNiOGExZmZlY2JhMmZlMzYiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvMDkwZjU0ZGItMWI5OS00YmRkLWFhYmItMGE5NTkyODY2NTBiIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wM1QyMTowNjo1NC42ODY0MTVaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICI0MWVkZjg4MDA3YzU5MmU5MzBiNTkyOTcyNTI0NzUzOWFmODdlOWZhMjlhOWNiMGM4Yzk1Y2VmOGI1Mjk0MmY3IiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19XQ==

============================================================
Demo complete.
  ledger path: /var/folders/yg/cx7v1zqs26v1y0ys4wjhxh1m0000gn/T/tmp7z9yjg_r/goose-capsules.jsonl  (temp; deleted on exit)
  Chain: write_order → decide(BLOCKED) → fyi (escalation).
  To use with real Goose: see examples/goose-capsule/server.py
============================================================
```

---

## Independent inclusion re-confirmation (curl, after the run)

```
$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50
HTTP 200 — leaf_index=251, entry_hash=b968474d474dfe1361ff31ce856b5ce12b87fda124b8ad10cdb6e6eb172c7014, root_hash=3d7f715f47c45b54e23acd717688ab9ace5c2041a0b5b2ac8b4e51a2f59ea62a

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/41edf88007c592e930b5929725247539af87e9fa29a9cb0c8c95cef8b52942f7
HTTP 200 — leaf_index=252, entry_hash=3bb90db3489a27404d5294fe67847b60b4cc206a7179db49f2be8bae2a27329e, root_hash=431bf68610c8629ec0da0dad51c67e563436b39090c3807842ebb1febfa73594

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/bf3d94f6933e6a85f248da1dc05e6a3519f97f4ea8103b1e3b8a1ffecba2fe36
HTTP 200 — leaf_index=253, entry_hash=123d2e14c727b8a813675876005576c4151bd2dcf2b15d1e42599a20f6647ce6, root_hash=ddd8a3d25506d37fa7b21380b06de71f89fe499ae272e76959d022f4fa2d6878
```

---

## Verify permalinks (verify.agentactioncapsule.org)

Each permalink carries the full capsule JSON in the URL fragment (never sent to the server —
client-side only, per `scitt_cose/hosted.py`'s deployed JS). Fragment auto-load is confirmed
wired and working (see the Dapr-parity task's prior finding, re-confirmed live below): every
permalink below renders the anchor banner, digest graph, and privilege log on a fresh page
load with zero manual pasting.

**Individual capsules:**

| # | Capsule | Permalink |
|---|---------|-----------|
| 1 | write_order/submit_order | `https://verify.agentactioncapsule.org/v/ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJiYTJjZTViZjNmOTM3MDA5ZDViZjFhODc5YzVjMGNiOTgzZjAyMjcyYjA5Y2E2NzA5NmUwODQ4MmU1MTZjYzUwIiwgImFjdGlvbl9pZCI6ICJzdWJtaXRfb3JkZXIvMjczNjM1MWUtY2VjNi00Y2Q1LWI3MDMtMmIyMWQwMmI1MDQwIiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wM1QyMTowNjo1NC42ODU4NjdaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI5YmViODU0YzE5MmVmMjE1MzkzODE2NDY3OTJiYjAzNDZkNjU3ODFhOWUyNzA1MmM0Nzc3NWFlMWIyYWJkOTIyIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWE3YTk3ZTRhNDA3MGFlNjE5MDMyODY0M2Y5MjA5ZDc0NTE1YzE5OTA0MGNkYmYxNjFkZTE2YmQzZWYxNjQ2MCIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJ3cml0ZV9vcmRlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19` |
| 2 | decide/approve_large_order (REJECTED) | `https://verify.agentactioncapsule.org/v/41edf88007c592e930b5929725247539af87e9fa29a9cb0c8c95cef8b52942f7#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWVkZjg4MDA3YzU5MmU5MzBiNTkyOTcyNTI0NzUzOWFmODdlOWZhMjlhOWNiMGM4Yzk1Y2VmOGI1Mjk0MmY3IiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzUxN2QzMDI5LWI1M2UtNGY4NC05Y2ZhLTNmY2EyOGZhYWI4MiIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMjE6MDY6NTQuNjg2MTU5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJiYTJjZTViZjNmOTM3MDA5ZDViZjFhODc5YzVjMGNiOTgzZjAyMjcyYjA5Y2E2NzA5NmUwODQ4MmU1MTZjYzUwIiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19` |
| 3 | fyi/escalate_to_manager | `https://verify.agentactioncapsule.org/v/bf3d94f6933e6a85f248da1dc05e6a3519f97f4ea8103b1e3b8a1ffecba2fe36#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJiZjNkOTRmNjkzM2U2YTg1ZjI0OGRhMWRjMDVlNmEzNTE5Zjk3ZjRlYTgxMDNiMWUzYjhhMWZmZWNiYTJmZTM2IiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyLzA5MGY1NGRiLTFiOTktNGJkZC1hYWJiLTBhOTU5Mjg2NjUwYiIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMjE6MDY6NTQuNjg2NDE1WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiYjQ0ODRjZWUwNGE3OTdjODJlMzAwZmE1OWYzNTM3MTYzZjVlNGNiNWZiY2RkYzhhMjU4YjA3NmRlYTZmNjJiNyIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjYxYzhlYWIyMTNkM2UwMzRmNDY1YTJmNTlkYzVhNTVkMWVmYjY4ZjU5NGQyNzY4M2IwNDQzNTE2MTA0N2IzNjMiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiNDFlZGY4ODAwN2M1OTJlOTMwYjU5Mjk3MjUyNDc1MzlhZjg3ZTlmYTI5YTljYjBjOGM5NWNlZjhiNTI5NDJmNyIsICJyZWxhdGlvbiI6ICJjb25maXJtcyJ9fQ==` |

**Full 3-capsule chain bundle (renders the Chain Navigation table with a VERDICT column and
Previous/Next click-through):**

`https://verify.agentactioncapsule.org/v/ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiYmEyY2U1YmYzZjkzNzAwOWQ1YmYxYTg3OWM1YzBjYjk4M2YwMjI3MmIwOWNhNjcwOTZlMDg0ODJlNTE2Y2M1MCIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyLzI3MzYzNTFlLWNlYzYtNGNkNS1iNzAzLTJiMjFkMDJiNTA0MCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMjE6MDY6NTQuNjg1ODY3WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI0MWVkZjg4MDA3YzU5MmU5MzBiNTkyOTcyNTI0NzUzOWFmODdlOWZhMjlhOWNiMGM4Yzk1Y2VmOGI1Mjk0MmY3IiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyLzUxN2QzMDI5LWI1M2UtNGY4NC05Y2ZhLTNmY2EyOGZhYWI4MiIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMjE6MDY6NTQuNjg2MTU5WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJiYTJjZTViZjNmOTM3MDA5ZDViZjFhODc5YzVjMGNiOTgzZjAyMjcyYjA5Y2E2NzA5NmUwODQ4MmU1MTZjYzUwIiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogImJmM2Q5NGY2OTMzZTZhODVmMjQ4ZGExZGMwNWU2YTM1MTlmOTdmNGVhODEwM2IxZTNiOGExZmZlY2JhMmZlMzYiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvMDkwZjU0ZGItMWI5OS00YmRkLWFhYmItMGE5NTkyODY2NTBiIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wM1QyMTowNjo1NC42ODY0MTVaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICI0MWVkZjg4MDA3YzU5MmU5MzBiNTkyOTcyNTI0NzUzOWFmODdlOWZhMjlhOWNiMGM4Yzk1Y2VmOGI1Mjk0MmY3IiwgInJlbGF0aW9uIjogImNvbmZpcm1zIn19XQ==`

Click sequence for the denial beat: open the bundle permalink → Chain Navigation table shows
all 3 capsules with `# / CAPSULE_ID / ACTION_TYPE / VERDICT / TIMESTAMP` → row 2 reads
`decide | blocked` → click row 2 (or "Next") to load capsule 2 standalone, which shows the
anchor banner (`✓ Anchored log index 252 · inclusion proof verified (RFC 9162)`), the digest
graph (chains_to → capsule 1, attests_over agent_input/agent_output), and the privilege log
(agent_input/agent_output both WITHHELD — digest committed, payload not carried in the record).

**Live-verified (browser, fresh page loads, zero pasting) — see task report for the
per-permalink confirmation table.**

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
  verify result:     ok=False  findings: ['recomputed 2cc7748b60ba0dcf527afe56bc68d2b63816d615362d6d6ce09fc0c69da2db44 != carried ba2ce5bf3f937009d5bf1a879c5c0cb983f02272b09ca67096e08482e516cc50']
  Tamper detected — ok=False as expected. ✓
```

Flipping one byte in the `agent_output_digest` of the `submit_order` capsule makes the
recomputed `capsule_id` disagree with the carried one — `verify().ok` flips to `False` with a
`capsule_id_mismatch`-shaped finding, exactly as before this task's changes.

---

## Test suite

```
$ python3 -m pytest tests/test_goose.py -v
...
21 passed in 0.31s
```

No tests skipped (mcp installed). `tests/test_goose.py` exercises `capsule_emit/server.py`
(Pattern B) and `MCPCapsuleEmitter` directly (Pattern A) — it does not import
`examples/goose-capsule/demo.py` or `server.py`, so this suite is independent of the changes
in this task and stayed green throughout.

---

## Summary

| Check | Result |
|-------|--------|
| Live 3-capsule chain (order → denial → escalation) | ✓ |
| Live anchor inclusion for all 3 chain capsules (leaf 251/252/253) | ✓ |
| Genuine refusal (`verdict_class=blocked`, `human_disposed=true`, approver + reason) | ✓ |
| Individual verify permalinks (3) | ✓ |
| Bundle permalink (Chain Navigation + VERDICT column) | ✓ |
| `verify ok=True` (all 4 sealed capsules, offline) | ✓ |
| tamper → `ok=False` | ✓ |
| `test_goose.py` 21/21 green | ✓ |

**Sealed capsule (offline artifact, unchanged from prior transcript):**
`examples/goose-capsule/evidence/capsule.json`
