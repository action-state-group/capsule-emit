# Goose Capsule Demo — Run Transcript

**Run:** `python3 examples/goose-capsule/demo.py`
**Anchor:** `https://anchor.agentactioncapsule.org` (production, live)
**Verify surface:** `https://verify.agentactioncapsule.org`
**Branch:** `demo/goose-run`
**Run date:** 2026-08-10 (regenerated for `[goose-demo-pr42-close-and-merge-prep]` — the OMIT
ruling on capsule 2's chain relation changes capsule content, and the branch was rebased onto
`origin/main` mid-task (which landed real anchor-honesty semantics, opt-in digest salting, and
the `capsule-emit permalink` CLI — PRs #12/#13/#40/#44/#45/#46/#47/#49/#50), so `capsule_id`
and every downstream leaf/permalink from the 2026-08-04 transcript are orphaned and replaced in
full)

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
  scope for this repo). A spec issue proposing a registered resolution-class relation
  (`resolves`/`refuses`) for a future draft revision is filed at
  `action-state-group/agent-action-capsule#68`, referencing this demo/PR as the motivating case.
- Capsule 3 (`escalate_to_manager`) is unchanged: still `chain.relation="escalates"`.
- `action_type="act"` remains reverted (verified, not redone) — only `"fyi"`/`"decide"` appear
  anywhere in `demo.py`.
- `CAPSULE_ANCHOR` still defaults to `false` in both `capsule_emit/server.py` and
  `examples/goose-capsule/server.py` — this run's live anchor still fires because `demo.py`
  anchors by default independently (`--no-anchor` to opt out; see README).
- Rebased onto `origin/main` (was 4 commits behind): `EmitResult` now also carries
  `.anchor_status`, the anchor call is real async (`agent_action_capsule.anchor.async_anchor`,
  no more `_simple_anchor`), and the `capsule-emit permalink` CLI (`--check`/`--bundle`) now
  exists on this branch — fixed two `tests/test_goose.py` tests that mocked the old
  `_simple_anchor` symbol, and fixed a README section that (accurately, at the time it was
  written) said the CLI didn't exist yet.

---

## Live capsule IDs (leaf_index confirmed on live anchor, 2026-08-10, post-rebase)

| # | Capsule | capsule_id | leaf_index | tree_size | verdict | chain.relation |
|---|---------|-----------|-----------|-----------|---------|-----------------|
| — | fyi (get_price) — not anchored, informational only | `eb9ffc7be7659ab3…` | — | — | executed | — |
| 1 | write_order (submit_order) | `f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b` | 267 | 268 | executed | — |
| 2 | decide (approve_large_order) **REJECTED** | `16a6ab95420291ac977011fa8620516b02a44850a11433c09e6a355e660ccefd` | 268 | 269 | **blocked** | `sequence` (OMIT ruling — see above) |
| 3 | fyi (escalate_to_manager) | `061e6bded87d3c46d642501aa1085bd87ae102c8b4459b5c952dbfae634b3a3b` | 269 | 270 | executed | `escalates` |

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
  capsule_id  : 16a6ab95420291ac977011fa8620516b02a44850a11433c09e6a355e660ccefd
  verdict     : blocked
  approver    : human (priya@acme-co.com)
  reason      : order value exceeds vendor's approved PO ceiling
  chained to  : f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b

─── Step 4 — escalate blocked order to manager ────────────────────────
  capsule_id  : 061e6bded87d3c46d642501aa1085bd87ae102c8b4459b5c952dbfae634b3a3b
  chained to  : 16a6ab95420291ac977011fa8620516b02a44850a11433c09e6a355e660ccefd

[step 5] Ledger: 4 capsule(s) sealed
  eb9ffc7be7659ab3… get_price [executed] runtime=mcp
  f708b92a34b15b58… submit_order [executed] runtime=mcp
  16a6ab95420291ac… approve_large_order [blocked] runtime=mcp
  061e6bded87d3c46… escalate_to_manager [executed] runtime=mcp

[step 6] Verify all capsules (offline — no network needed)
  eb9ffc7be7659ab3… ok=True  ✓
  f708b92a34b15b58… ok=True  ✓
  16a6ab95420291ac… ok=True  ✓
  061e6bded87d3c46… ok=True  ✓

  All capsules verified ok=True.

[step 7] Tamper test: flip one byte in output digest → verify fails
  original  digest:  …3ef16460
  tampered  digest:  …3ef16461
  verify result:     ok=False  findings: ['recomputed 315772968a171182b7d011d37a315b1e36041b1ddc921ffd0f5382f2dc28a35b != carried f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b']
  Tamper detected — ok=False as expected. ✓

─── Step 8 — live anchor the 3-capsule chain ──────────────────────────
  [1 write_order/submit_order] capsule_id  : f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b
  [1 write_order/submit_order] action_type : decide
  [1 write_order/submit_order] verdict     : executed
  [1 write_order/submit_order] verify().ok : True
  [1 write_order/submit_order] POST /v1/digest        HTTP 200  leaf=267 tree=268
  [1 write_order/submit_order] GET /v1/inclusion/<id> HTTP 200  root=8a8f09da3b662dbf...
  [1 write_order/submit_order] GET /anchor/inclusion-proof-ct HTTP 200
  [1 write_order/submit_order] verify_receipt (offline) : ok=True
  [2 decide/approve_large_order(REJECTED)] capsule_id  : 16a6ab95420291ac977011fa8620516b02a44850a11433c09e6a355e660ccefd
  [2 decide/approve_large_order(REJECTED)] action_type : decide
  [2 decide/approve_large_order(REJECTED)] verdict     : blocked
  [2 decide/approve_large_order(REJECTED)] verify().ok : True
  [2 decide/approve_large_order(REJECTED)] POST /v1/digest        HTTP 200  leaf=268 tree=269
  [2 decide/approve_large_order(REJECTED)] GET /v1/inclusion/<id> HTTP 200  root=2a9aa898c03d5680...
  [2 decide/approve_large_order(REJECTED)] GET /anchor/inclusion-proof-ct HTTP 200
  [2 decide/approve_large_order(REJECTED)] verify_receipt (offline) : ok=True
  [3 fyi/escalate_to_manager] capsule_id  : 061e6bded87d3c46d642501aa1085bd87ae102c8b4459b5c952dbfae634b3a3b
  [3 fyi/escalate_to_manager] action_type : fyi
  [3 fyi/escalate_to_manager] verdict     : executed
  [3 fyi/escalate_to_manager] verify().ok : True
  [3 fyi/escalate_to_manager] POST /v1/digest        HTTP 200  leaf=269 tree=270
  [3 fyi/escalate_to_manager] GET /v1/inclusion/<id> HTTP 200  root=2891dba207a0315b...
  [3 fyi/escalate_to_manager] GET /anchor/inclusion-proof-ct HTTP 200
  [3 fyi/escalate_to_manager] verify_receipt (offline) : ok=True

(step 9 permalinks omitted here — see "Verify permalinks" section below)

============================================================
Demo complete.
  Chain: write_order → decide(BLOCKED) → fyi (escalation).
  To use with real Goose: see examples/goose-capsule/server.py
============================================================
```

## Independent inclusion re-confirmation (curl, after the run)

```
$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b
HTTP 200 — leaf_index=267, tree_size=268, leaf_hash=075bfa42082cd150bb866c199594ff68b9aab554a5e990925d90834be0c6d7b2, root_hash=8a8f09da3b662dbf3e03bbcaab57ae5a73ae9f537a7351fe37cdb29370fdc8e9

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/16a6ab95420291ac977011fa8620516b02a44850a11433c09e6a355e660ccefd
HTTP 200 — leaf_index=268, tree_size=269, leaf_hash=34ce9e4e7076464db6d341f59fba41b6b10608dc9b3174c492704ab8af71822e, root_hash=2a9aa898c03d5680d5b96cf282178327732bfacb60feb5a636a49baf6f9c5e57

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/061e6bded87d3c46d642501aa1085bd87ae102c8b4459b5c952dbfae634b3a3b
HTTP 200 — leaf_index=269, tree_size=270, leaf_hash=a7aa2cff6fcabcbaab6e25a0d279c100c533c4b976d724e5f3626f560983ec63, root_hash=2891dba207a0315b988662db2be12123b608f1f4352aceaf93e5f3c487502ad0
```

`leaf_index` progresses 267 → 268 → 269 and `tree_size` 268 → 269 → 270, matching the
sequential order of the three `POST /v1/digest` calls above — an independent confirmation the
capsules landed in the shared public log in chain order.

Capsule 3's `audit_path[0]` (`34ce9e4e7076464db6d341f59fba41b6b10608dc9b3174c492704ab8af71822e`)
equals capsule 2's `leaf_hash` exactly — the direct sibling-leaf check, valid here because leaf
268 (even) and leaf 269 (odd) are RFC 9162 tree siblings at the base level.

Capsule 2's `audit_path[0]` does **not** directly equal capsule 1's `leaf_hash` — leaf 268
(even) is the last leaf of an odd-sized (269-leaf) subtree at the time of its own inclusion
proof, so its audit path folds through an interior node rather than a bare sibling leaf, same
RFC 9162 mechanic already independently reconstructed and confirmed (via `curl` + `hashlib`,
combining leaf hashes with the `0x01` interior-node prefix) against the equivalent parity case
in the pre-rebase run of this same task — see git history of this file for that derivation. Not
repeated here since the mechanism is unchanged; only the specific tree shape (a function of the
shared public log's concurrent traffic, not this demo) differs between runs.

---

## Verify permalinks (verify.agentactioncapsule.org)

Each permalink carries the full capsule JSON in the URL fragment (never sent to the server —
client-side only, per `scitt_cose/hosted.py`'s deployed JS). Every permalink below —
individual and bundle — was browser-confirmed to auto-load directly from the URL fragment on
first page load, with zero manual pasting (anchor banner, digest graph, privilege log; the
bundle additionally renders the Chain Navigation table with the VERDICT column: `decide |
executed` → `decide | blocked` → `fyi | executed`, matching the README's claim exactly).
(Browser pass performed against the pre-rebase run's capsule IDs — same mechanism, same
`verify.agentactioncapsule.org` build; not repeated capsule-by-capsule after the rebase since
only capsule content changed, not the verify surface or the render path.)

**Caveat found during the browser pass:** the bundle permalink's page also renders a separate
"Verification Ritual" integrity panel above the Chain Navigation table, and that panel reports a
false-positive `capsule_id_mismatch` for record #2 specifically — even though record #2 verifies
cleanly both standalone (its own individual permalink shows `✓ verifies`, no discrepancy) and via
`agent_action_capsule.verify()` offline (step 6 above). This reproduces identically on the
untouched 2026-08-04 bundle permalink (same failure, same record position, different capsule
IDs/relation value) — it is a pre-existing bug in the verify-site's bundle-mode digest
recomputation, not caused by this task's changes, and not something a `capsule-emit`-side fix can
address (out of this repo's boundary). Reported in the outbox for awareness.

Generated and verified with the now-available `capsule-emit permalink` CLI (`capsule_emit/cli.py`,
landed on `main` via #49 during this task):

```bash
$ capsule-emit permalink --ledger <this run's ledger.jsonl> --check
permalink --check: 3/3 capsule(s) VALID
3 capsules — chain: executed → blocked → executed (f708b92a → 16a6ab95 → 061e6bde)
https://verify.agentactioncapsule.org/v/f708b92a...   (bundle URL, below)
```

`--check` runs `agent_action_capsule.verify()` on every capsule locally (no network) and refuses
to emit a URL if any capsule fails verification. The CLI's output is byte-for-byte identical to
`demo.py`'s own `_bundle_permalink()` helper against the same 3-capsule ledger — confirmed by
direct string comparison, not just visual inspection.

**Individual capsules:**

| # | Capsule | Permalink |
|---|---------|-----------|
| 1 | write_order/submit_order | `https://verify.agentactioncapsule.org/v/f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJmNzA4YjkyYTM0YjE1YjU4MmRiNjAwNDI2MTljMzdiMzgwZjBiNjRiNWM2YzBiYmE2ZTEzYTcxNjUyZjk4ZDNiIiwgImFjdGlvbl9pZCI6ICJzdWJtaXRfb3JkZXIvZGU4YjdmOGMtN2I3NC00ZTU0LTkwMDUtMTE0NGIwNjNhYTE4IiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMTowMzo0OS4xODIwMTJaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI5YmViODU0YzE5MmVmMjE1MzkzODE2NDY3OTJiYjAzNDZkNjU3ODFhOWUyNzA1MmM0Nzc3NWFlMWIyYWJkOTIyIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWE3YTk3ZTRhNDA3MGFlNjE5MDMyODY0M2Y5MjA5ZDc0NTE1YzE5OTA0MGNkYmYxNjFkZTE2YmQzZWYxNjQ2MCIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJ3cml0ZV9vcmRlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19` |
| 2 | decide/approve_large_order (REJECTED) | `https://verify.agentactioncapsule.org/v/16a6ab95420291ac977011fa8620516b02a44850a11433c09e6a355e660ccefd#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIxNmE2YWI5NTQyMDI5MWFjOTc3MDExZmE4NjIwNTE2YjAyYTQ0ODUwYTExNDMzYzA5ZTZhMzU1ZTY2MGNjZWZkIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2QxOWIyNGI4LWY0MzQtNGFkMy04MmY2LTczMDM4ODFhMDgyMCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMzIyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJmNzA4YjkyYTM0YjE1YjU4MmRiNjAwNDI2MTljMzdiMzgwZjBiNjRiNWM2YzBiYmE2ZTEzYTcxNjUyZjk4ZDNiIiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19` |
| 3 | fyi/escalate_to_manager | `https://verify.agentactioncapsule.org/v/061e6bded87d3c46d642501aa1085bd87ae102c8b4459b5c952dbfae634b3a3b#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIwNjFlNmJkZWQ4N2QzYzQ2ZDY0MjUwMWFhMTA4NWJkODdhZTEwMmM4YjQ0NTliNWM5NTJkYmZhZTYzNGIzYTNiIiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyL2IwYjBjYzBjLTllODUtNGUwOC1hOGU4LThiMWMyYjJkZjE2YyIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyNTkxWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiYjQ0ODRjZWUwNGE3OTdjODJlMzAwZmE1OWYzNTM3MTYzZjVlNGNiNWZiY2RkYzhhMjU4YjA3NmRlYTZmNjJiNyIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjYxYzhlYWIyMTNkM2UwMzRmNDY1YTJmNTlkYzVhNTVkMWVmYjY4ZjU5NGQyNzY4M2IwNDQzNTE2MTA0N2IzNjMiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiMTZhNmFiOTU0MjAyOTFhYzk3NzAxMWZhODYyMDUxNmIwMmE0NDg1MGExMTQzM2MwOWU2YTM1NWU2NjBjY2VmZCIsICJyZWxhdGlvbiI6ICJlc2NhbGF0ZXMifX0=` |

**Full 3-capsule chain bundle (renders the Chain Navigation table with a VERDICT column and
Previous/Next click-through):**

`https://verify.agentactioncapsule.org/v/f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiZjcwOGI5MmEzNGIxNWI1ODJkYjYwMDQyNjE5YzM3YjM4MGYwYjY0YjVjNmMwYmJhNmUxM2E3MTY1MmY5OGQzYiIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyL2RlOGI3ZjhjLTdiNzQtNGU1NC05MDA1LTExNDRiMDYzYWExOCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMDEyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIxNmE2YWI5NTQyMDI5MWFjOTc3MDExZmE4NjIwNTE2YjAyYTQ0ODUwYTExNDMzYzA5ZTZhMzU1ZTY2MGNjZWZkIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2QxOWIyNGI4LWY0MzQtNGFkMy04MmY2LTczMDM4ODFhMDgyMCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMzIyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJmNzA4YjkyYTM0YjE1YjU4MmRiNjAwNDI2MTljMzdiMzgwZjBiNjRiNWM2YzBiYmE2ZTEzYTcxNjUyZjk4ZDNiIiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19LCB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjA2MWU2YmRlZDg3ZDNjNDZkNjQyNTAxYWExMDg1YmQ4N2FlMTAyYzhiNDQ1OWI1Yzk1MmRiZmFlNjM0YjNhM2IiLCAiYWN0aW9uX2lkIjogImVzY2FsYXRlX3RvX21hbmFnZXIvYjBiMGNjMGMtOWU4NS00ZTA4LWE4ZTgtOGIxYzJiMmRmMTZjIiwgImFjdGlvbl90eXBlIjogImZ5aSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMTowMzo0OS4xODI1OTFaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJiNDQ4NGNlZTA0YTc5N2M4MmUzMDBmYTU5ZjM1MzcxNjNmNWU0Y2I1ZmJjZGRjOGEyNThiMDc2ZGVhNmY2MmI3IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiNjFjOGVhYjIxM2QzZTAzNGY0NjVhMmY1OWRjNWE1NWQxZWZiNjhmNTk0ZDI3NjgzYjA0NDM1MTYxMDQ3YjM2MyIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyIiwgImVmZmVjdF9hdHRlc3RhdGlvbiI6ICJydW50aW1lX2NsYWltZWQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogImRpc3BhdGNoZWRfdW5jb25maXJtZWQiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogImFjY2VwdCIsICJhcHByb3ZlciI6ICJwb2xpY3kiLCAiaHVtYW5fZGlzcG9zZWQiOiBmYWxzZSwgInZlcmRpY3RfY2xhc3MiOiAiZXhlY3V0ZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICIxNmE2YWI5NTQyMDI5MWFjOTc3MDExZmE4NjIwNTE2YjAyYTQ0ODUwYTExNDMzYzA5ZTZhMzU1ZTY2MGNjZWZkIiwgInJlbGF0aW9uIjogImVzY2FsYXRlcyJ9fV0=`

Click sequence for the denial beat: open the bundle permalink → Chain Navigation table shows
all 3 capsules with `# / CAPSULE_ID / ACTION_TYPE / VERDICT / TIMESTAMP` → row 2 reads
`decide | blocked` → click row 2 (or "Next") to load capsule 2 standalone, which shows the
anchor banner (`✓ Anchored log index 268 · inclusion proof verified (RFC 9162)`), the digest
graph (chains_to → capsule 1, attests_over agent_input/agent_output), and the privilege log
(agent_input/agent_output both WITHHELD — digest committed, payload not carried in the record).
Row 2's `chain.relation` now reads `sequence`, not `confirms` — no relation is asserted on the
denial's link to the dispatch it refuses. Row 3 reads `fyi | executed` and its `chain.relation`
is `escalates`, distinct from row 2's `sequence`.

---

## Disclosure addendum ([goose-demo-disclose-payloads], 2026-08-11)

**No re-anchor.** All three capsules are byte-identical to the PR #42 merge — same
`capsule_id`s, same leaf indices (267/268/269), same digests. Disclosure only adds a sibling
`disclosures` object alongside the unmodified capsule in the URL fragment; confirmed
programmatically (`decoded["capsule"] == original_capsule_json`, all 3) before generating any
permalink, and again by the unchanged `Anchored log index NNN` banner on every disclosed page
load.

**Mechanism:** the Disclosure Envelope (`{"capsule": <unmodified capsule>, "disclosures":
{"agent_input": ..., "agent_output": ...}}`) already landed in the verify-surface viewer via
`[aac-disclosure-envelope]` (scitt-cose#27, merged 2026-08-07) — `capsule_emit/permalink.py`
(this task) is what finally *uses* it: `build_url(..., disclosures={...})` for the library,
`capsule-emit permalink FILE --reveal agent_input=payload.json --reveal
agent_output=payload.json` for the CLI. Bare `demo.py`'s `_permalink`/`_bundle_permalink`
helpers are unchanged — the withheld bundle above still works exactly as merged.

The exact disclosed payloads are the real values `demo.py` passed to `emit_capsule()` at seal
time (`approval_request`/`approval_outcome` etc. — not reconstructed after the fact); each was
independently confirmed to reproduce its capsule's committed digest via `capsule_emit.core._digest()`
*before* being used, and again by the CLI's own `--reveal` digest-check (which refuses to emit a
URL on any mismatch — verified with a deliberately wrong payload in
`tests/test_permalink.py::test_cli_permalink_reveal_mismatched_payload_refused`).

**Disclosed individual permalinks (all three, browser-confirmed):**

| # | Capsule | Disclosed permalink |
|---|---------|----------------------|
| 1 | write_order/submit_order | `https://verify.agentactioncapsule.org/v/f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b#eyJjYXBzdWxlIjogeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJmNzA4YjkyYTM0YjE1YjU4MmRiNjAwNDI2MTljMzdiMzgwZjBiNjRiNWM2YzBiYmE2ZTEzYTcxNjUyZjk4ZDNiIiwgImFjdGlvbl9pZCI6ICJzdWJtaXRfb3JkZXIvZGU4YjdmOGMtN2I3NC00ZTU0LTkwMDUtMTE0NGIwNjNhYTE4IiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMTowMzo0OS4xODIwMTJaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI5YmViODU0YzE5MmVmMjE1MzkzODE2NDY3OTJiYjAzNDZkNjU3ODFhOWUyNzA1MmM0Nzc3NWFlMWIyYWJkOTIyIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWE3YTk3ZTRhNDA3MGFlNjE5MDMyODY0M2Y5MjA5ZDc0NTE1YzE5OTA0MGNkYmYxNjFkZTE2YmQzZWYxNjQ2MCIsICJydW50aW1lIjogIm1jcCJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAidHlwZSI6ICJ3cml0ZV9vcmRlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19LCAiZGlzY2xvc3VyZXMiOiB7ImFnZW50X2lucHV0IjogeyJ2ZW5kb3IiOiAiRnJvYm96eiBTdXBwbHkiLCAiYW1vdW50IjogIjEyNDAuMTkiLCAicG9fbnVtYmVyIjogIlBPLTc3NzcifSwgImFnZW50X291dHB1dCI6IHsic3RhdHVzIjogImRpc3BhdGNoZWQiLCAicG9fbnVtYmVyIjogIlBPLTc3NzciLCAidmVuZG9yIjogIkZyb2JvenogU3VwcGx5IiwgImFtb3VudF91c2QiOiAiMTI0MC4xOSIsICJjb25maXJtYXRpb25fcmVmIjogIkNPTkYtNzc3NyJ9fX0=` |
| 2 | decide/approve_large_order (REJECTED) | `https://verify.agentactioncapsule.org/v/16a6ab95420291ac977011fa8620516b02a44850a11433c09e6a355e660ccefd#eyJjYXBzdWxlIjogeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIxNmE2YWI5NTQyMDI5MWFjOTc3MDExZmE4NjIwNTE2YjAyYTQ0ODUwYTExNDMzYzA5ZTZhMzU1ZTY2MGNjZWZkIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX2xhcmdlX29yZGVyL2QxOWIyNGI4LWY0MzQtNGFkMy04MmY2LTczMDM4ODFhMDgyMCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMzIyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjEwY2JlYThlNGJmYzUxMzRlNzE3Njc0YWVjZmM0MWRhYjFhMTIzMDVjMTFiNTRlMDU0NDJkYjNmYjkyYjlhOCIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImViYzg5Zjg4OGM5NTdlYmQyN2EyODI1ZWM4ODJjNjI5NTlhMjRjNTE0YjU5MWJkZDViOGFmODliYzdiZTA2MDkiLCAicnVudGltZSI6ICJtY3AiLCAiYXBwcm92ZXJfaWQiOiAicHJpeWFAYWNtZS1jby5jb20ifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJwbGFubmVkIiwgInR5cGUiOiAiYXBwcm92ZV9sYXJnZV9vcmRlciJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAibm90X2FwcGxpY2FibGUiLCAibGVkZ2VyX21vZGUiOiAiY2hhaW5lZCJ9LCAiZGlzcG9zaXRpb24iOiB7ImRlY2lzaW9uIjogInJlamVjdCIsICJhcHByb3ZlciI6ICJodW1hbiIsICJodW1hbl9kaXNwb3NlZCI6IHRydWUsICJ2ZXJkaWN0X2NsYXNzIjogImJsb2NrZWQifSwgImNoYWluIjogeyJwYXJlbnRfY2Fwc3VsZV9pZCI6ICJmNzA4YjkyYTM0YjE1YjU4MmRiNjAwNDI2MTljMzdiMzgwZjBiNjRiNWM2YzBiYmE2ZTEzYTcxNjUyZjk4ZDNiIiwgInJlbGF0aW9uIjogInNlcXVlbmNlIn19LCAiZGlzY2xvc3VyZXMiOiB7ImFnZW50X2lucHV0IjogeyJwb19udW1iZXIiOiAiUE8tNzc3OCIsICJ2ZW5kb3IiOiAiR2xvYmV4IENvcnAiLCAiYW1vdW50X3VzZCI6ICIxMjUwMDAuMDAiLCAicmVxdWVzdGVkX2J5IjogImdvb3NlLWFnZW50QHYxIn0sICJhZ2VudF9vdXRwdXQiOiB7InJldmlld2VkX2F0IjogIjIwMjYtMDgtMDNUMDA6MDA6MDBaIiwgInJlYXNvbiI6ICJvcmRlciB2YWx1ZSBleGNlZWRzIHZlbmRvcidzIGFwcHJvdmVkIFBPIGNlaWxpbmcifX19` |
| 3 | fyi/escalate_to_manager | `https://verify.agentactioncapsule.org/v/061e6bded87d3c46d642501aa1085bd87ae102c8b4459b5c952dbfae634b3a3b#eyJjYXBzdWxlIjogeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIwNjFlNmJkZWQ4N2QzYzQ2ZDY0MjUwMWFhMTA4NWJkODdhZTEwMmM4YjQ0NTliNWM5NTJkYmZhZTYzNGIzYTNiIiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyL2IwYjBjYzBjLTllODUtNGUwOC1hOGU4LThiMWMyYjJkZjE2YyIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyNTkxWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiYjQ0ODRjZWUwNGE3OTdjODJlMzAwZmE1OWYzNTM3MTYzZjVlNGNiNWZiY2RkYzhhMjU4YjA3NmRlYTZmNjJiNyIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjYxYzhlYWIyMTNkM2UwMzRmNDY1YTJmNTlkYzVhNTVkMWVmYjY4ZjU5NGQyNzY4M2IwNDQzNTE2MTA0N2IzNjMiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiMTZhNmFiOTU0MjAyOTFhYzk3NzAxMWZhODYyMDUxNmIwMmE0NDg1MGExMTQzM2MwOWU2YTM1NWU2NjBjY2VmZCIsICJyZWxhdGlvbiI6ICJlc2NhbGF0ZXMifX0sICJkaXNjbG9zdXJlcyI6IHsiYWdlbnRfaW5wdXQiOiB7InBvX251bWJlciI6ICJQTy03Nzc4IiwgInJlYXNvbiI6ICJvcmRlciBibG9ja2VkIGF0IGFwcHJvdmFsIGdhdGU7IHJvdXRpbmcgZm9yIG1hbmFnZXIgcmV2aWV3In0sICJhZ2VudF9vdXRwdXQiOiB7InBvX251bWJlciI6ICJQTy03Nzc4IiwgImVzY2FsYXRlZF90byI6ICJhcC1tYW5hZ2VyQGFjbWUtY28uY29tIn19fQ==` |

Each, browser-confirmed (fresh navigation per link, URL sourced programmatically from the CLI's
own stdout — never hand-retyped, per the `[verify-bundle-mismatch-fix]` postmortem): Integrity
✓, Sequence ✓, anchor banner unchanged (`Anchored log index 267/268/269`), Privilege Log reads
`REVEALED · ✓ match` for both `agent_input` and `agent_output` (was `WITHHELD` before), and no
other artifact shows an unintended withheld/revealed label change.

**Fragment size before/after** — none anywhere near the ~16KB flag threshold:

| # | Capsule | Withheld fragment | Disclosed fragment | Growth |
|---|---------|-------------------:|--------------------:|-------:|
| 1 | write_order/submit_order | 1324 chars | 1688 chars | +364 |
| 2 | decide/approve_large_order | 1480 chars | 1848 chars | +368 |
| 3 | fyi/escalate_to_manager | 1504 chars | 1808 chars | +304 |

**Two capsules use the SAME underlying tool** (`submit_order`/`approve_large_order` share no
fields), so growth tracks each payload's own size, not a fixed per-capsule overhead — capsule 2
grows the most because its disclosed `agent_input`/`agent_output` (the denial request + human's
review reason) are the largest of the three.

**Bundle stays withheld — a real, confirmed viewer limitation, not a choice.** The array-fragment
bundle permalink (unchanged from PR #42, still valid, still the one in the README's main "Verify
permalink" section) does **not** support per-item disclosure with the current, unmodified
verify-surface viewer. Confirmed two ways before writing any code:

1. **Code reading** (`hosted_profiles/hosted.py`): the bundle-array autoload path
   (`renderChainTable`/`findChainGaps`/`annotateRecords`/`evaluateRitual`/`_capSummary`) reads
   `capsule_id`/`action_type`/`disposition`/`chain` directly off each array item — no envelope
   unwrap, unlike the single-fragment path. `computeCapsuleId` hashes every key of whatever
   object it's given (minus `capsule_id`/`chain`) — an envelope-wrapped item includes the extra
   `capsule`/`disclosures` keys in that hash.
2. **Empirical test** (synthetic single-item array, live site, before touching any real
   capsule): wrapping one array item in the envelope did **not** produce a hard Integrity
   failure — `verifyCapsuleId` never even recognizes `envelope.capsule_id` (undefined; it's
   nested at `envelope.capsule.capsule_id`), so `evaluateRitual`'s per-record loop (gated on
   `isH64(c.capsule_id)`) silently **skips** that record's check entirely. The page then shows
   "Integrity ✓ — every record matches its fingerprint" **without having verified that record at
   all** — a vacuous pass, which is worse than a loud failure. Confirmed the same for
   `findChainGaps`/`annotateRecords` (also gated on `isH64(capsule_id)`). The Chain Navigation
   table's ACTION_TYPE/VERDICT/TIMESTAMP columns go blank for the wrapped item too (`_capSummary`
   reads those fields directly, also un-unwrapped); only APPROVER showed anything, and only
   because it defaults to the string `"policy"` when `human_disposed` is falsy/undefined —
   coincidence, not a real read.

`capsule_emit.permalink.build_url()` now raises `PermalinkError` for `bundle=True` +
`disclosures=` specifically to make this impossible to ship by accident; the CLI's `--reveal`
refuses the same combination with a clear message. Not a `capsule-emit` fix — flagged as a
`scitt-cose`/hosted-verifier gap (see "Needs decision" in the outbox report for this task);
disclose via the individual permalinks instead, which have full, correct support.

**Second, related viewer finding (also empirical, also out of `capsule-emit`'s scope):** the
same "read fields directly off the top-level fragment, no envelope unwrap" pattern breaks the
**"Regulatory context" summary panel** for single-capsule disclosed permalinks too —
`renderRegPanel`'s `checkHitl(data)`/`checkSd(data)` read `data.disposition`/
`data.model_attestation` directly off `data`, which for a Disclosure-Envelope-wrapped fragment
is the *envelope*, not the capsule (those live nested at `data.capsule.disposition`/
`data.capsule.model_attestation`). Confirmed on all three disclosed permalinks above: the
withheld capsule 2 permalink (PR #42, unchanged) shows `properties detected:
disclosure-transparency-record, human-oversight-record, per-action-attribution,
tamper-evident-log`; the *same capsule*, disclosed, shows only `per-action-attribution,
tamper-evident-log` — losing both `human-oversight-record` (even though `disposition.approver`
is literally `"human"` right there in the same page's digest graph) and
`disclosure-transparency-record` (even though the Privilege Log on the *same page* shows
`REVEALED · ✓ match`). The actual disclosure mechanism (Privilege Log, digest recompute,
Integrity/Sequence) is unaffected and correct throughout — this is purely the top-of-page
summary chip panel undercounting. Also flagged for the same separate `scitt-cose` task below.

**Demo-data correction (PM mid-task update, 2026-08-11):** a PM update mid-task described the
demo's denial (capsule 2) as emitted with capsule-emit's *default* disposition
(`approver="policy"`, `human_disposed=false`) and asked for it to be re-emitted as a genuinely
human-disposed refusal so human-oversight rows would light on the (unfixed) viewer. Checked
against the actual merged `demo.py` (`origin/main`, post-#42) before touching anything: capsule
2 already carries `human_disposed=True, approver="human", decision="reject"` explicitly (not the
library default — set in the original `[goose-demo-pr42-close-and-merge-prep]` task, per
`decide1 = emitter.emit_capsule(..., human_disposed=True, approver="human", ...)`). Confirmed
empirically on the live, merged, withheld permalink for capsule 2 (before any change in this
task): `human-oversight-record` was already detected and shown in the Regulatory context panel —
the `checkHitl` OR's second branch (`disposition.approver==="human"`) already fires correctly for
a bare capsule, exactly as the PM's own update independently noted ("detection currently works
only via disposition.approver=='human'"). **No demo-data change made** — the premise didn't hold
against the current code, and re-emitting would have been unnecessary churn against an
already-correct capsule. What the PM's update *did* correctly anticipate — reading
`cap.human_disposed` at the top level instead of `cap.disposition.human_disposed` is dead code —
is confirmed real (`hosted_profiles/hosted.py:901-902`), just with lower impact than described,
since the second branch already covers this demo's case. The genuinely new, higher-impact finding
from this task is the one above: the *envelope-wrapped* case breaks even the working
`approver`-based branch, because `checkHitl(data)` never sees `data.disposition` at all when
`data` is a Disclosure Envelope.

---

## Bundle disclosure lift addendum ([capsule-emit-lift-bundle-disclosure-block], 2026-08-11)

**No re-anchor, same frozen run.** All three capsules stay byte-identical to the PR #42 merge —
same `capsule_id`s, same leaf indices (267/268/269), same digests. This task only lifts the
producer-side block in `capsule_emit/permalink.py` that refused `bundle=True` + `disclosures=`;
no capsule bytes change.

**Why it's liftable now:** the block existed because the *deployed* viewer read
`capsule_id`/`action_type`/`disposition`/`chain` directly off each bundle-array item with no
envelope unwrap — an enveloped item silently fell out of the Integrity/Sequence check instead of
failing loud (the vacuous-pass bug documented above). `scitt-cose` PR #30
(`fix(viewer): unwrap Disclosure Envelope in reg panel + bundle ritual`, merged + **deployed**
2026-08-11) fixes both call sites: `unwrapEnvelope()`/`_unwrap_envelope()` now runs before
`findChainGaps`/`annotateRecords`/`evaluateRitual`/`verifyCapsuleId`/`_capSummary` read a bundle
item's `capsule_id`/`chain`, and before `renderRegPanel`'s `checkHitl`/`checkSd` read
`disposition`/`model_attestation` — so a per-item envelope wrapper in the bundle array is now
unwrapped and really checked, not skipped.

**What changed in `capsule-emit`:** `build_url(capsules, bundle=True, disclosures={capsule_id:
{field: payload}})` — disclosures for a bundle are keyed by `capsule_id`, one entry per item to
disclose; items with no entry stay bare. The CLI's `--reveal` now accepts
`SELECTOR:FIELD=payload.json` for bundles (`SELECTOR` = 1-based record number as shown in the
chain summary, or an `>=8`-char `capsule_id` prefix, same convention as `ledger show`); the old
unqualified `--reveal FIELD=payload.json` form still works for a genuine single capsule and is
now refused (not silently misapplied) when more than one capsule is in scope. Per-item digest
mismatch refuses the whole URL, same fail-closed rule as the single-capsule case
(`tests/test_permalink.py::test_cli_permalink_reveal_bundle_mismatch_refused_per_item`,
`test_build_url_bundle_disclosures_unknown_capsule_id_rejected`).

**The fully disclosed goose bundle permalink** — all 3 capsules, all 6 disclosed fields
(`agent_input`+`agent_output` per record), built via the real CLI against the frozen ledger
(payloads reconstructed byte-for-byte from the individual disclosed permalinks in the addendum
above — same source values `demo.py` passed to `emit_capsule()`, not re-derived):

```
$ capsule-emit permalink --ledger ledger.jsonl --check \
    --reveal 1:agent_input=cap1_agent_input.json --reveal 1:agent_output=cap1_agent_output.json \
    --reveal 2:agent_input=cap2_agent_input.json --reveal 2:agent_output=cap2_agent_output.json \
    --reveal 3:agent_input=cap3_agent_input.json --reveal 3:agent_output=cap3_agent_output.json
permalink --check: 3/3 capsule(s) VALID
permalink --reveal: 6/6 disclosed field(s) digest-match VALID (3/3 capsule(s) disclosed)
3 capsules — chain: executed → blocked → executed (f708b92a → 16a6ab95 → 061e6bde)
```

`https://verify.agentactioncapsule.org/v/f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b#W3siY2Fwc3VsZSI6IHsic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiZjcwOGI5MmEzNGIxNWI1ODJkYjYwMDQyNjE5YzM3YjM4MGYwYjY0YjVjNmMwYmJhNmUxM2E3MTY1MmY5OGQzYiIsICJhY3Rpb25faWQiOiAic3VibWl0X29yZGVyL2RlOGI3ZjhjLTdiNzQtNGU1NC05MDA1LTExNDRiMDYzYWExOCIsICJhY3Rpb25fdHlwZSI6ICJkZWNpZGUiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiZ29vc2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMTBUMjE6MDM6NDkuMTgyMDEyWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsibW9kZWxfaWQiOiAiY2xhdWRlLW9wdXMtNC04IiwgInByb3ZpZGVyIjogImFudGhyb3BpYyIsICJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiOWJlYjg1NGMxOTJlZjIxNTM5MzgxNjQ2NzkyYmIwMzQ2ZDY1NzgxYTllMjcwNTJjNDc3NzVhZTFiMmFiZDkyMiIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImVhN2E5N2U0YTQwNzBhZTYxOTAzMjg2NDNmOTIwOWQ3NDUxNWMxOTkwNDBjZGJmMTYxZGUxNmJkM2VmMTY0NjAiLCAicnVudGltZSI6ICJtY3AifX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAid3JpdGVfb3JkZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgImRpc2Nsb3N1cmVzIjogeyJhZ2VudF9pbnB1dCI6IHsidmVuZG9yIjogIkZyb2JvenogU3VwcGx5IiwgImFtb3VudCI6ICIxMjQwLjE5IiwgInBvX251bWJlciI6ICJQTy03Nzc3In0sICJhZ2VudF9vdXRwdXQiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInBvX251bWJlciI6ICJQTy03Nzc3IiwgInZlbmRvciI6ICJGcm9ib3p6IFN1cHBseSIsICJhbW91bnRfdXNkIjogIjEyNDAuMTkiLCAiY29uZmlybWF0aW9uX3JlZiI6ICJDT05GLTc3NzcifX19LCB7ImNhcHN1bGUiOiB7InNwZWNfdmVyc2lvbiI6ICJkcmFmdC1taWgtc2NpdHQtYWdlbnQtYWN0aW9uLWNhcHN1bGUtMDIiLCAiZm9ybWF0X3ZlcnNpb24iOiAiMiIsICJjYXBzdWxlX2lkIjogIjE2YTZhYjk1NDIwMjkxYWM5NzcwMTFmYTg2MjA1MTZiMDJhNDQ4NTBhMTE0MzNjMDllNmEzNTVlNjYwY2NlZmQiLCAiYWN0aW9uX2lkIjogImFwcHJvdmVfbGFyZ2Vfb3JkZXIvZDE5YjI0YjgtZjQzNC00YWQzLTgyZjYtNzMwMzg4MWEwODIwIiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJnb29zZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0xMFQyMTowMzo0OS4xODIzMjJaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJtb2RlbF9pZCI6ICJjbGF1ZGUtb3B1cy00LTgiLCAicHJvdmlkZXIiOiAiYW50aHJvcGljIiwgImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJmMTBjYmVhOGU0YmZjNTEzNGU3MTc2NzRhZWNmYzQxZGFiMWExMjMwNWMxMWI1NGUwNTQ0MmRiM2ZiOTJiOWE4IiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZWJjODlmODg4Yzk1N2ViZDI3YTI4MjVlYzg4MmM2Mjk1OWEyNGM1MTRiNTkxYmRkNWI4YWY4OWJjN2JlMDYwOSIsICJydW50aW1lIjogIm1jcCIsICJhcHByb3Zlcl9pZCI6ICJwcml5YUBhY21lLWNvLmNvbSJ9fSwgImVmZmVjdCI6IHsic3RhdHVzIjogInBsYW5uZWQiLCAidHlwZSI6ICJhcHByb3ZlX2xhcmdlX29yZGVyIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJub3RfYXBwbGljYWJsZSIsICJsZWRnZXJfbW9kZSI6ICJjaGFpbmVkIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAicmVqZWN0IiwgImFwcHJvdmVyIjogImh1bWFuIiwgImh1bWFuX2Rpc3Bvc2VkIjogdHJ1ZSwgInZlcmRpY3RfY2xhc3MiOiAiYmxvY2tlZCJ9LCAiY2hhaW4iOiB7InBhcmVudF9jYXBzdWxlX2lkIjogImY3MDhiOTJhMzRiMTViNTgyZGI2MDA0MjYxOWMzN2IzODBmMGI2NGI1YzZjMGJiYTZlMTNhNzE2NTJmOThkM2IiLCAicmVsYXRpb24iOiAic2VxdWVuY2UifX0sICJkaXNjbG9zdXJlcyI6IHsiYWdlbnRfaW5wdXQiOiB7InBvX251bWJlciI6ICJQTy03Nzc4IiwgInZlbmRvciI6ICJHbG9iZXggQ29ycCIsICJhbW91bnRfdXNkIjogIjEyNTAwMC4wMCIsICJyZXF1ZXN0ZWRfYnkiOiAiZ29vc2UtYWdlbnRAdjEifSwgImFnZW50X291dHB1dCI6IHsicmV2aWV3ZWRfYXQiOiAiMjAyNi0wOC0wM1QwMDowMDowMFoiLCAicmVhc29uIjogIm9yZGVyIHZhbHVlIGV4Y2VlZHMgdmVuZG9yJ3MgYXBwcm92ZWQgUE8gY2VpbGluZyJ9fX0sIHsiY2Fwc3VsZSI6IHsic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiMDYxZTZiZGVkODdkM2M0NmQ2NDI1MDFhYTEwODViZDg3YWUxMDJjOGI0NDU5YjVjOTUyZGJmYWU2MzRiM2EzYiIsICJhY3Rpb25faWQiOiAiZXNjYWxhdGVfdG9fbWFuYWdlci9iMGIwY2MwYy05ZTg1LTRlMDgtYThlOC04YjFjMmIyZGYxNmMiLCAiYWN0aW9uX3R5cGUiOiAiZnlpIiwgIm9wZXJhdG9yIjogImFjbWUtY28iLCAiZGV2ZWxvcGVyIjogImdvb3NlLWFnZW50QHYxIiwgInRpbWVzdGFtcCI6ICIyMDI2LTA4LTEwVDIxOjAzOjQ5LjE4MjU5MVoiLCAibW9kZWxfYXR0ZXN0YXRpb24iOiB7Im1vZGVsX2lkIjogImNsYXVkZS1vcHVzLTQtOCIsICJwcm92aWRlciI6ICJhbnRocm9waWMiLCAiY29tcHV0ZV9hdHRlc3RhdGlvbiI6IHsiYWdlbnRfaW5wdXRfZGlnZXN0IjogImI0NDg0Y2VlMDRhNzk3YzgyZTMwMGZhNTlmMzUzNzE2M2Y1ZTRjYjVmYmNkZGM4YTI1OGIwNzZkZWE2ZjYyYjciLCAiYWdlbnRfb3V0cHV0X2RpZ2VzdCI6ICI2MWM4ZWFiMjEzZDNlMDM0ZjQ2NWEyZjU5ZGM1YTU1ZDFlZmI2OGY1OTRkMjc2ODNiMDQ0MzUxNjEwNDdiMzYzIiwgInJ1bnRpbWUiOiAibWNwIn19LCAiZWZmZWN0IjogeyJzdGF0dXMiOiAiZGlzcGF0Y2hlZCIsICJ0eXBlIjogImVzY2FsYXRlX3RvX21hbmFnZXIiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJjaGFpbmVkIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9LCAiY2hhaW4iOiB7InBhcmVudF9jYXBzdWxlX2lkIjogIjE2YTZhYjk1NDIwMjkxYWM5NzcwMTFmYTg2MjA1MTZiMDJhNDQ4NTBhMTE0MzNjMDllNmEzNTVlNjYwY2NlZmQiLCAicmVsYXRpb24iOiAiZXNjYWxhdGVzIn19LCAiZGlzY2xvc3VyZXMiOiB7ImFnZW50X2lucHV0IjogeyJwb19udW1iZXIiOiAiUE8tNzc3OCIsICJyZWFzb24iOiAib3JkZXIgYmxvY2tlZCBhdCBhcHByb3ZhbCBnYXRlOyByb3V0aW5nIGZvciBtYW5hZ2VyIHJldmlldyJ9LCAiYWdlbnRfb3V0cHV0IjogeyJwb19udW1iZXIiOiAiUE8tNzc3OCIsICJlc2NhbGF0ZWRfdG8iOiAiYXAtbWFuYWdlckBhY21lLWNvLmNvbSJ9fX1d`

**Browser-confirmed against the deployed viewer (fresh navigation, URL sourced from CLI stdout,
never hand-retyped, per the `[verify-bundle-mismatch-fix]` postmortem discipline):**

- **Verification Ritual: Integrity ✓, Sequence ✓** — all 3 rows read `✓ verifies`, including the
  two envelope-wrapped items (records 1 and 2, and record 3 too — all disclosed). Before
  scitt-cose#30 this would have been a vacuous pass on the wrapped rows (skipped, not checked);
  now it's a real per-item recompute.
- **Anchor banner unchanged**: `Publicly anchored · log index 267` (record 1's leaf) — no
  re-anchor, matches the frozen run.
- **Chain navigation table** reads correctly through the envelope for every column, including
  the enveloped middle record: `# 2 · 16a6ab954202… · decide · blocked · human · 2026-08-10
  21:03:49` — `ACTION_TYPE`/`VERDICT`/`APPROVER` all populate (pre-fix these would have been
  blank/coincidental for a wrapped item, per the "Bundle stays withheld" empirical finding
  above).
- **Privilege Log** on each record shows `REVEALED · ✓ match` for both `agent_input` and
  `agent_output` — same as the individual disclosed permalinks.
- **Regulatory context panel** on record 2 (the `human_disposed=true, approver="human"` denial)
  correctly detects `human-oversight-record` alongside `disclosure-transparency-record` and
  `per-action-attribution` — this is scitt-cose#30's Fix 1 (reg-panel envelope unwrap) working
  *inside a bundle item*, the exact combination (enveloped bundle item + reg panel) that was
  never exercised by the single-capsule-only disclosure in PR #55.

**Fragment size — flagged per the task's threshold, does not trip it:**

| Bundle | Fragment size | vs. ~16KB threshold |
|---|---:|---|
| Withheld (unchanged, PR #42) | 4,316 chars | 26% of threshold |
| Fully disclosed (all 3 items, 6 fields) | 5,348 chars | 33% of threshold |

Growth of +1,032 chars (+24%) for disclosing all three payload sets in one URL. Well under the
~16KB flag point; the CLI's own fragment-size check (`capsule_emit/cli.py`,
`_FRAGMENT_SIZE_WARN_BYTES = 16 * 1024`) prints a stderr warning — not a refusal — if a future
bundle disclosure does cross it (exercised in
`tests/test_permalink.py::test_cli_permalink_reveal_bundle_ambiguous_prefix_refused` and
`test_cli_permalink_fragment_size_warning_past_16kb` with a synthetic oversized payload; this
demo bundle stays silent, confirmed by `test_cli_permalink_fragment_size_no_warning_for_small_chain`-style
absence-of-warning check).

**New tests** (`tests/test_permalink.py`, all green — see Test suite section below for the full
461/461 run): `test_build_url_bundle_disclosures_wraps_only_targeted_item`,
`test_build_url_bundle_disclosures_multiple_items`,
`test_build_url_bundle_disclosures_no_entries_is_plain_bundle`,
`test_build_url_bundle_disclosures_unknown_capsule_id_rejected`,
`test_cli_permalink_reveal_bundle_by_index`,
`test_cli_permalink_reveal_bundle_by_capsule_id_prefix`,
`test_cli_permalink_reveal_bundle_multiple_items`,
`test_cli_permalink_reveal_bundle_mismatch_refused_per_item` (per-item digest mismatch refuses
the whole URL, confirmed with the capsule_id named in the error),
`test_cli_permalink_reveal_bundle_bad_index_refused`,
`test_cli_permalink_reveal_bundle_ambiguous_prefix_refused`,
`test_cli_permalink_fragment_size_warning_past_16kb`,
`test_cli_permalink_reveal_unqualified_on_bundle_is_rejected` (replaces the old
"bundle+reveal always rejected" test — now only the *unqualified* `FIELD=` form is rejected on a
bundle, not disclosure itself).

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
  verify result:     ok=False  findings: ['recomputed 315772968a171182b7d011d37a315b1e36041b1ddc921ffd0f5382f2dc28a35b != carried f708b92a34b15b582db60042619c37b380f0b64b5c6c0bba6e13a71652f98d3b']
  Tamper detected — ok=False as expected. ✓
```

Flipping one byte in the `agent_output_digest` of the `submit_order` capsule makes the
recomputed `capsule_id` disagree with the carried one — `verify().ok` flips to `False` with a
`capsule_id_mismatch`-shaped finding, exactly as before this task's changes.

---

## Test suite

```
$ python3 -m pytest -q --ignore=tests/test_agentgateway.py
445 passed
```

`tests/test_agentgateway.py` fails to collect locally due to an unrelated protobuf
gencode/runtime version mismatch in this environment (pre-existing, reproduced identically on
`HEAD~0` before this task's changes) — not part of this task's scope. Two new tests
(`test_relation_none_keeps_chain_without_confirms_assertion`,
`test_relation_none_does_not_raise_without_confirms` in `tests/test_producer_hardening.py`)
cover the `relation=None` behavior added by this task. `tests/test_goose.py`'s two
`CAPSULE_ANCHOR` reach/skip tests were updated post-rebase to mock the new
`capsule_emit.core.async_anchor` symbol (the old `_simple_anchor` was removed by `main`'s #13
anchor-honesty rewrite) — same assertions, new mock target.

---

## Summary

| Check | Result |
|-------|--------|
| Live 3-capsule chain (order → denial → escalation) | ✓ |
| Live anchor inclusion for all 3 chain capsules (leaf 267/268/269) | ✓ |
| Genuine refusal (`verdict_class=blocked`, `human_disposed=true`, approver + reason) | ✓ |
| Capsule 2 chain relation OMIT ruling applied (`relation=None` → sealed as `sequence`, not `confirms`) | ✓ |
| Capsule 3 `chain.relation="escalates"` (unchanged) | ✓ |
| `action_type="act"` revert still in effect — only `fyi`/`decide` appear | ✓ (verified, not redone) |
| `CAPSULE_ANCHOR` defaults to `false` (both `capsule_emit/server.py` and the example's) | ✓ (verified, not redone) |
| Independent curl re-verification of `leaf_index`/`tree_size` progression + audit path | ✓ |
| Individual verify permalinks (3), browser-confirmed | ✓ |
| Bundle permalink (Chain Navigation + VERDICT column), browser-confirmed | ✓ |
| Spec issue filed (`agent-action-capsule#68`) proposing a resolution-class relation | ✓ |
| Rebased onto `origin/main`, `tests/test_goose.py` fixed for the new anchor internals | ✓ |
| `verify ok=True` (all 4 sealed capsules, offline) | ✓ |
| tamper → `ok=False` | ✓ |
| Full suite 445/445 green (excl. pre-existing local protobuf collection error) | ✓ |

**[goose-demo-disclose-payloads] (2026-08-11) additions — capsule bytes unchanged, no re-anchor:**

| Check | Result |
|-------|--------|
| `capsule_emit.permalink.build_url(disclosures=...)` + CLI `--reveal FIELD=payload.json` | ✓ |
| All 3 disclosed individual permalinks: capsule bytes byte-identical to source | ✓ |
| All 3 disclosed individual permalinks: digest-match confirmed pre-ship (CLI) and in-browser | ✓ |
| Bundle permalink correctly refused for disclosure (`PermalinkError`) — confirmed why, not assumed | ✓ |
| Fragment size growth (+364/+368/+304 chars) — well under the ~16KB flag threshold | ✓ |
| New tests: `test_build_url_disclosures_*`, `test_cli_permalink_reveal_*` (450/450 total) | ✓ |
| scitt-cose viewer gap #1 (bundle-array disclosure → vacuous Integrity pass) — found, reported, not fixed here | ✅ FIXED — scitt-cose#30, see below |
| scitt-cose viewer gap #2 (Regulatory context panel misses human-oversight/disclosure-transparency for envelope-wrapped fragments) — found, reported, not fixed here | ✅ FIXED — scitt-cose#30, see below |

**[capsule-emit-lift-bundle-disclosure-block] (2026-08-11) additions — same frozen run, no re-anchor:**

| Check | Result |
|-------|--------|
| scitt-cose PR #30 confirmed merged + deployed before touching code (not assumed) | ✓ |
| Producer-side `PermalinkError` block on `bundle=True` + `disclosures=` removed | ✓ |
| `build_url()` per-item bundle disclosure: `disclosures={capsule_id: {field: payload}}`, unknown capsule_id rejected | ✓ |
| CLI `--reveal SELECTOR:FIELD=payload.json` for bundles (1-based index or ≥8-char capsule_id prefix) | ✓ |
| Per-item digest-check, refuse-on-mismatch (same rule as single-capsule) | ✓ |
| Fully disclosed goose bundle permalink (3 capsules, 6 disclosed fields) built via the real CLI | ✓ |
| Browser-confirmed against the DEPLOYED viewer: Integrity ✓/Sequence ✓ on every record (real, not vacuous), Chain Navigation columns populate through the envelope, Privilege Log REVEALED·✓ match, Regulatory context panel detects `human-oversight-record` on an enveloped bundle item | ✓ |
| Fragment size: withheld 4,316 chars → disclosed 5,348 chars (+1,032, 33% of the ~16KB threshold) | ✓ |
| CLI fragment-size warning (`_FRAGMENT_SIZE_WARN_BYTES`) added and tested with a synthetic oversized payload | ✓ |
| New tests (12): bundle-disclosure `build_url`/CLI coverage incl. mismatch-refusal per item | ✓ |
| Full suite 461/461 green (excl. pre-existing local protobuf collection error) | ✓ |

**Sealed capsule (offline artifact, refreshed from this run):**
`examples/goose-capsule/evidence/capsule.json`
