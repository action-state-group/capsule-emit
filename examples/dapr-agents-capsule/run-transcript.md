# Dapr Agents Demo — Run Transcript

Run: `python3 examples/dapr-agents-capsule/demo.py`
Anchor: `https://anchor.agentactioncapsule.org` (production)
Verify surface: `https://verify.agentactioncapsule.org`
Branch: `demo-permalink-builder`
Run date: 2026-08-06 (regenerated for `[demo-permalink-builder]` — adds Step 5,
the bundle verify permalink built via `capsule_emit.permalink`, the same code
`capsule-emit permalink` uses. capsule_id and leaf/tree numbers are fresh from
this run and supersede the 2026-07-28 transcript.)

---

## Live capsule IDs

| Capsule | capsule_id | leaf_index | tree_size | verdict |
|---------|-----------|-----------|-----------|---------|
| fyi (check_invoice) | `5179216505a0949e3195db1b9d180d43cea059babcfee12859628cb8a1dc8380` | 262 | 263 | executed |
| decide (approve_payment) | `735b1b01d41a29e855ca78a1b2e44d329745827150751dd66eb1bfdac5e7e95f` | 263 | 264 | executed |

Both registered idempotently via `POST /v1/digest` and confirmed at:
- `GET https://anchor.agentactioncapsule.org/v1/inclusion/<capsule_id>` → HTTP 200

---

## Full output

```
─── Step 1 — seal fyi capsule (tool call) ─────────────────────────────
  capsule_id  : 5179216505a0949e3195db1b9d180d43cea059babcfee12859628cb8a1dc8380
  action_type : fyi
  verdict     : executed
  verify().ok : True

─── Step 2 — anchor fyi capsule → POST /v1/digest ─────────────────────
  POST /v1/digest                  HTTP 200
  entry_hash                       : ab6bc9bb138e3d2cf0f177cb1b5469199d0461979673f60df05fa811ab702bee
  leaf_index                       : 262
  tree_size                        : 263

  GET /v1/inclusion/<fyi_id>       HTTP 200
  leaf_index                       : 262
  tree_size                        : 263
  root_hash                        : 6e4b8db7d7844d88e8a30576e28313a949ec2d8d4e9d1667ae52cbd6c8390323

  GET /anchor/inclusion-proof-ct   HTTP 200
  audit_path                       : ['984b355450da9b5c5dfaa49ae5e41843afb2262ec60b00325e57d681fc5d2030', 'fd0340b6891ed3e12198e7036ce41c6942caedc29290d20e2b2516391c677bfb', '8fec23db820d9ccf2ce2ca7f8d133a455830bc3efd07114db96d2d840976ee2c']

  verify_receipt (offline)         : ok=True

─── Step 3 — seal decide capsule (HITL approval) ──────────────────────
  capsule_id     : 735b1b01d41a29e855ca78a1b2e44d329745827150751dd66eb1bfdac5e7e95f
  action_type    : decide
  verdict        : executed
  human_disposed : True
  decision       : accept
  chained to     : 5179216505a0949e3195db1b9d180d43cea059babcfee12859628cb8a1dc8380
  verify().ok    : True

─── Step 4 — anchor decide capsule → POST /v1/digest ──────────────────
  POST /v1/digest                  HTTP 200
  entry_hash                       : ee4360c7eb3a20b98baa362bdf8f4a89963a0885bb9bfe0c695a4f51f60cda7c
  leaf_index                       : 263
  tree_size                        : 264

  GET /v1/inclusion/<decide_id>    HTTP 200
  leaf_index                       : 263
  tree_size                        : 264
  root_hash                        : 94ed58884c91da3b1ad18254fcfaec15f5ef124e5d4362a291f657a66c19ec72

  GET /anchor/inclusion-proof-ct   HTTP 200
  audit_path                       : ['3a2cafc8b9d654c4d6f8fb36e8d1a588b47e159313115390b44c46f3da0d67ab', '984b355450da9b5c5dfaa49ae5e41843afb2262ec60b00325e57d681fc5d2030', 'fd0340b6891ed3e12198e7036ce41c6942caedc29290d20e2b2516391c677bfb', '8fec23db820d9ccf2ce2ca7f8d133a455830bc3efd07114db96d2d840976ee2c']

  verify_receipt (offline)         : ok=True

─── Summary ───────────────────────────────────────────────────────────
  fyi    capsule_id : 5179216505a0949e3195db1b9d180d43cea059babcfee12859628cb8a1dc8380
         leaf_index : 262   tree_size : 263
         /v1/inclusion/<id> : HTTP 200
         verify().ok: True   receipt ok: True

  decide capsule_id : 735b1b01d41a29e855ca78a1b2e44d329745827150751dd66eb1bfdac5e7e95f
         leaf_index : 263   tree_size : 264
         /v1/inclusion/<id> : HTTP 200
         verify().ok: True   receipt ok: True

  All checks PASS.

─── Step 5 — verify permalink (bundle) ────────────────────────────────
  2 capsules — chain: executed → executed (51792165 → 735b1b01)
  https://verify.agentactioncapsule.org/v/5179216505a0949e3195db1b9d180d43cea059babcfee12859628cb8a1dc8380#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiNTE3OTIxNjUwNWEwOTQ5ZTMxOTVkYjFiOWQxODBkNDNjZWEwNTliYWJjZmVlMTI4NTk2MjhjYjhhMWRjODM4MCIsICJhY3Rpb25faWQiOiAiY2hlY2tfaW52b2ljZS85ZTQ1NWZmYi01NGJiLTQ3OTEtYWIxYS0zNTJjNjJkMmU5NmMiLCAiYWN0aW9uX3R5cGUiOiAiZnlpIiwgIm9wZXJhdG9yIjogImFjbWUtY28iLCAiZGV2ZWxvcGVyIjogImludm9pY2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDZUMjA6MTE6MTUuMDY4NDcxWiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsiY29tcHV0ZV9hdHRlc3RhdGlvbiI6IHsiYWdlbnRfaW5wdXRfZGlnZXN0IjogImE5ODFkNDUxMmIwNjE4NmMzYTA4MWUyZWQ5YjE4MzBhMjIxNmIxYjk4Y2FkMDcwNzJlMjAxZWIyMzU0ZGRjYzUiLCAiYWdlbnRfb3V0cHV0X2RpZ2VzdCI6ICJmZjg3MjE2NTc1Mjg1OTRjMGI3YTJiMzUxYjkwNzQ1NWNmNzkxMDdlMmUzMWE5NDViM2U2YjNkYTA2ZDRlNmZjIiwgInJ1bnRpbWUiOiAiZGFwcl9hZ2VudHMiLCAiZGFwcl9hZ2VudHMiOiB7ImFnZW50X25hbWUiOiAiaW52b2ljZS1jaGVja2VyIiwgInRvb2xfbmFtZSI6ICJjaGVja19pbnZvaWNlIiwgIndvcmtmbG93X2luc3RhbmNlX2lkIjogIndmLWRlbW8tMjAyNi0wNy0yOCIsICJhcHBfaWQiOiAiaW52b2ljZS1hcHAifX19LCAiZWZmZWN0IjogeyJzdGF0dXMiOiAiZGlzcGF0Y2hlZCIsICJ0eXBlIjogImNoZWNrX2ludm9pY2UiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICI3MzViMWIwMWQ0MWEyOWU4NTVjYTc4YTFiMmU0NGQzMjk3NDU4MjcxNTA3NTFkZDY2ZWIxYmZkYWM1ZTdlOTVmIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX3BheW1lbnQvMzExZDZlZTEtYTZlMy00Mzc3LTk0ZmUtMjljMmQxODhkZmI4IiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJpbnZvaWNlLWFnZW50QHYxIiwgInRpbWVzdGFtcCI6ICIyMDI2LTA4LTA2VDIwOjExOjE1LjY1NTU0OVoiLCAibW9kZWxfYXR0ZXN0YXRpb24iOiB7ImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICJmZGY4N2Y1NDk3OWY4NWExM2Q2YWRkZjkzOGZiZjE3NzE0YjQyMWNiZTdhMzkyZDkxNjFkYTNhNzM1ZjE1NWRkIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiOGE5NzJhZDBjMmM1NGY3MTQ1OGFjYmFhYTdjYjc3Zjg0NWJiY2NkZWUxMGE5YjRiMzgzMDBiOGViYjRmZjZlZiIsICJydW50aW1lIjogImRhcHJfYWdlbnRzIiwgImRhcHJfYWdlbnRzIjogeyJhZ2VudF9uYW1lIjogImludm9pY2UtY2hlY2tlciIsICJ0b29sX25hbWUiOiAiYXBwcm92ZV9wYXltZW50IiwgIndvcmtmbG93X2luc3RhbmNlX2lkIjogIndmLWRlbW8tMjAyNi0wNy0yOCIsICJhcHBfaWQiOiAiaW52b2ljZS1hcHAiLCAiYXBwcm92ZXJfaWQiOiAiYWxpY2VAYWNtZS5jb20ifX19LCAiZWZmZWN0IjogeyJzdGF0dXMiOiAiZGlzcGF0Y2hlZCIsICJ0eXBlIjogImFwcHJvdmVfcGF5bWVudCIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAiaHVtYW4iLCAiaHVtYW5fZGlzcG9zZWQiOiB0cnVlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9LCAiY2hhaW4iOiB7InBhcmVudF9jYXBzdWxlX2lkIjogIjUxNzkyMTY1MDVhMDk0OWUzMTk1ZGIxYjlkMTgwZDQzY2VhMDU5YmFiY2ZlZTEyODU5NjI4Y2I4YTFkYzgzODAiLCAicmVsYXRpb24iOiAiY29uZmlybXMifX1d
```

The bundle permalink above was generated with `capsule_emit.permalink.build_url()` (the same
function `capsule-emit permalink` uses), was regenerated with `capsule-emit permalink
--ledger <this run's ledger.jsonl> --check` (both capsules `VALID`), and was **browser-loaded and
confirmed**: it renders the "Chain navigation" table with both rows (`fyi | executed` →
`decide | executed`) and the `✓ Anchored log index 262 · inclusion proof verified (RFC 9162)`
banner.

**Note on shape:** this demo currently seals 2 capsules, both `executed` — there is no `blocked`
verdict in this particular run. The 3-capsule `executed → blocked → executed` chain shape lives in
the Goose demo (`examples/goose-capsule/`), whose evidence this permalink builder was also wired
into. `--bundle` is exercised identically either way: it defaults on for any run with more than
one capsule, regardless of what the verdicts are.

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
COSE receipt in one call. This is what the manager can curl to confirm registration.

**GET /anchor/inclusion-proof-ct** returns the RFC 6962 Merkle audit path for the given
`leaf_index` and `tree_size`. The `audit_path` hashes can be walked up to reproduce the
`root_hash` independently.

**verify_receipt (offline)** calls `scitt_cose.verify_receipt(receipt_bytes,
leaf_entry_hex=entry_hash, log_public_key_pem=pem)`. The Ed25519 log public key is fetched from
`/.well-known/did.json`. A `True` result means the COSE receipt is cryptographically valid and
the entry_hash is committed in the signed Merkle tree — no trust in the anchor server required.

**Decide chains to fyi** via `chain.parent_capsule_id` — the decide capsule's chain field carries
the fyi capsule_id, so a verifier can follow the full sequence: tool call → human approval → both
anchored, both verified.

**The bundle permalink** (Step 5) carries both capsules' JSON in a base64-encoded JSON-array URL
fragment (never sent to the server — client-side only). Opening it always renders the
chain-navigation table, because `--bundle` is the default whenever more than one capsule is
supplied — this is precisely the fix for the 2026-08-03 incident where a stale single-capsule
link silently dropped the chain table.
