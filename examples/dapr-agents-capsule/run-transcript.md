# Dapr Agents Demo — Run Transcript

Run: `python3 examples/dapr-agents-capsule/demo.py`
Anchor: `https://anchor.agentactioncapsule.org` (production, live)
Branch: `task/dapr-agents-demo-run`
Run date: 2026-08-02 (re-run against dapr-agents 1.0.5 script with 3-capsule chain + real HITL denial)

**Note on this re-run:** the previous committed transcript (dated 2026-07-30) documented an
earlier 2-capsule run where the decide capsule was APPROVED. The demo script was subsequently
updated (2026-07-31) to script a 3-capsule chain including a REAL denial, but was never
re-executed until this run. This transcript replaces the stale one with the actual live output
below.

**Editable-install fix required to run:** the globally registered `capsule-emit` editable
install pointed at a different, already-merged worktree (`_worktrees/capsule-emit/dapr-agents-adapter`)
that predates the `prior_capsule_id` chaining parameter added in this worktree (commit db08e63).
Running `demo.py` as a script (not `python -c`) put the script's own directory on `sys.path`,
not this worktree, so it silently picked up the stale adapter and failed at Step 3 with
`TypeError: DaprAgentsCapsuleEmitter.tool() got an unexpected keyword argument 'prior_capsule_id'`.
Fixed by re-running `pip install -e .` from this worktree to re-point the editable install here
before re-running the demo.

---

## Live capsule IDs (leaf_index confirmed on live anchor, 2026-08-02)

| # | Capsule | capsule_id | leaf_index | tree_size | verdict |
|---|---------|-----------|-----------|-----------|---------|
| 1 | fyi (check_invoice) | `b823aa8f5be968084235043d1b0d7dd3a66cb60d16f724ddcfc8d54a5ca5e978` | 242 | 243 | executed |
| 2 | decide (approve_payment) **REJECTED** | `08bec0383378c13cc8046964b3d4ffb8ebca2c573f3b26305f026bed0aa8b4cd` | 243 | 244 | **blocked** |
| 3 | fyi (escalate_to_manager) | `ff507525ececb1d1c6bdb1de7348ef7673dbe7a9d36ed5002d597ae447c6d710` | 244 | 245 | executed |

Capsule 2 (the denial): `verdict_class=blocked`, `effect.status=planned` (action gated, never
dispatched), `human_disposed=true`, `approver=bob@acme.com` (via `dapr_agents.approver_id`
extension field), reason: "amount exceeds vendor's approved contract ceiling". Chained to
capsule 1 via `chain.parent_capsule_id`. Capsule 3 chains past the denial to capsule 2, proving
the chain continues after a blocked action (escalation to a human manager instead of retrying).

All three registered synchronously via `POST /v1/digest`, confirmed via
`GET /v1/inclusion/<capsule_id>` (HTTP 200, independently re-confirmed via curl — see below),
and inclusion-proven via `GET /anchor/inclusion-proof-ct`. Every capsule verified offline via
both `agent_action_capsule.verify()` and `scitt_cose.verify_receipt()` against the log's Ed25519
public key fetched from `/.well-known/did.json` — no trust in the anchor server required.

---

## Full output (live run, exit code 0)

```

─── Step 1 — seal fyi capsule (tool call: check_invoice) ──────────────
  [1 fyi/check_invoice] capsule_id  : b823aa8f5be968084235043d1b0d7dd3a66cb60d16f724ddcfc8d54a5ca5e978
  [1 fyi/check_invoice] action_type : fyi
  [1 fyi/check_invoice] verdict     : executed
  [1 fyi/check_invoice] verify().ok : True
  [1 fyi/check_invoice] POST /v1/digest        HTTP 200  leaf=242 tree=243
  [1 fyi/check_invoice] GET /v1/inclusion/<id> HTTP 200  root=a5a10dee037b0900...
  [1 fyi/check_invoice] GET /anchor/inclusion-proof-ct HTTP 200
  [1 fyi/check_invoice] verify_receipt (offline) : ok=True

─── Step 2 — seal decide capsule (HITL approval: REJECTED) ────────────
  [2 decide/approve_payment(REJECTED)] capsule_id  : 08bec0383378c13cc8046964b3d4ffb8ebca2c573f3b26305f026bed0aa8b4cd
  [2 decide/approve_payment(REJECTED)] action_type : decide
  [2 decide/approve_payment(REJECTED)] verdict     : blocked
  [2 decide/approve_payment(REJECTED)] verify().ok : True
  [2 decide/approve_payment(REJECTED)] POST /v1/digest        HTTP 200  leaf=243 tree=244
  [2 decide/approve_payment(REJECTED)] GET /v1/inclusion/<id> HTTP 200  root=33c9095e46b5fecc...
  [2 decide/approve_payment(REJECTED)] GET /anchor/inclusion-proof-ct HTTP 200
  [2 decide/approve_payment(REJECTED)] verify_receipt (offline) : ok=True
  [2] chained to  : b823aa8f5be968084235043d1b0d7dd3a66cb60d16f724ddcfc8d54a5ca5e978

─── Step 3 — seal fyi capsule (tool call: escalate_to_manager) ────────
  [3 fyi/escalate_to_manager] capsule_id  : ff507525ececb1d1c6bdb1de7348ef7673dbe7a9d36ed5002d597ae447c6d710
  [3 fyi/escalate_to_manager] action_type : fyi
  [3 fyi/escalate_to_manager] verdict     : executed
  [3 fyi/escalate_to_manager] verify().ok : True
  [3 fyi/escalate_to_manager] POST /v1/digest        HTTP 200  leaf=244 tree=245
  [3 fyi/escalate_to_manager] GET /v1/inclusion/<id> HTTP 200  root=ea25f18e909c460c...
  [3 fyi/escalate_to_manager] GET /anchor/inclusion-proof-ct HTTP 200
  [3 fyi/escalate_to_manager] verify_receipt (offline) : ok=True
  [3] chained to  : 08bec0383378c13cc8046964b3d4ffb8ebca2c573f3b26305f026bed0aa8b4cd

─── Summary ───────────────────────────────────────────────────────────
  1 fyi/check_invoice                capsule_id=b823aa8f5be968084235043d1b0d7dd3a66cb60d16f724ddcfc8d54a5ca5e978  leaf=242  verify=True  receipt=True
  2 decide/approve_payment(REJECTED) capsule_id=08bec0383378c13cc8046964b3d4ffb8ebca2c573f3b26305f026bed0aa8b4cd  leaf=243  verify=True  receipt=True
  3 fyi/escalate_to_manager          capsule_id=ff507525ececb1d1c6bdb1de7348ef7673dbe7a9d36ed5002d597ae447c6d710  leaf=244  verify=True  receipt=True

  All checks PASS. Chain: fyi -> decide(BLOCKED) -> fyi (escalation).

  Full record dump written to /tmp/dapr_demo_dump.json
```

---

## Independent inclusion re-confirmation (curl, after the run)

```
$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/b823aa8f5be968084235043d1b0d7dd3a66cb60d16f724ddcfc8d54a5ca5e978
HTTP 200 — leaf_index=242, entry_hash=c14910a2ee1ce48cda88e14e23192bff89b8bcf03846610605eb3c73f6915dbc, root_hash=a5a10dee037b090071d71f569a549c7750501e051189d5c1c63fc17b7629ea6d

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/08bec0383378c13cc8046964b3d4ffb8ebca2c573f3b26305f026bed0aa8b4cd
HTTP 200 — leaf_index=243, entry_hash=58bbf1811f16fa3f72e1a528388328f8ca507bda2090bbc7c112848160e3ce63, root_hash=33c9095e46b5fecc992983996dc5ca4e23d979b09c30d867ab3a0eb4dff6ccd4

$ curl -s https://anchor.agentactioncapsule.org/v1/inclusion/ff507525ececb1d1c6bdb1de7348ef7673dbe7a9d36ed5002d597ae447c6d710
HTTP 200 — leaf_index=244, entry_hash=51a56b3efe159cd4a5c296be37169ccc06f4d809342868e321f254a6aba548bb, root_hash=ea25f18e909c460cf29a4459d1681982311be8acd63a915e0364420ba1a23e9d
```

---

## Verify permalinks (verify.agentactioncapsule.org)

Each permalink carries the full capsule JSON in the URL fragment (never sent to the server —
confirmed client-side only, see `/static/capsule.js`). All confirmed rendering live, fresh page
load, zero manual pasting required (see finding below).

**Individual capsules:**

| # | Capsule | Permalink |
|---|---------|-----------|
| 1 | fyi/check_invoice | `https://verify.agentactioncapsule.org/v/b823aa8f5be968084235043d1b0d7dd3a66cb60d16f724ddcfc8d54a5ca5e978#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJiODIzYWE4ZjViZTk2ODA4NDIzNTA0M2QxYjBkN2RkM2E2NmNiNjBkMTZmNzI0ZGRjZmM4ZDU0YTVjYTVlOTc4IiwgImFjdGlvbl9pZCI6ICJjaGVja19pbnZvaWNlLzYxZDZkNzJjLTdkY2QtNGVkYS05YTBiLWU2MDdmYWZlYmIwNCIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiaW52b2ljZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wM1QwMDozMjo0MC43NzE4NzhaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZDQ0MzViOWM4ZTJmMjNlYjAzMjUwOWY1YTk3MDU5MjkwNjIwMjM0ZjQ0MzgzN2RiMjk5Mjk1M2VkODQzNzVhMSIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogIjk0OTQ4NTU4NTliNDllM2I3ZmQxN2ViYzZlNWVmNWQxZmE2ZjJmN2RiZjgxMWI5NDg1MmM4YmM1YTYzYTIwOGMiLCAicnVudGltZSI6ICJkYXByX2FnZW50cyIsICJkYXByX2FnZW50cyI6IHsiYWdlbnRfbmFtZSI6ICJpbnZvaWNlLWNoZWNrZXIiLCAidG9vbF9uYW1lIjogImNoZWNrX2ludm9pY2UiLCAid29ya2Zsb3dfaW5zdGFuY2VfaWQiOiAid2YtZGVtby0yMDI2LTA3LTMwIiwgImFwcF9pZCI6ICJpbnZvaWNlLWFwcCJ9fX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiY2hlY2tfaW52b2ljZSIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogInN0YW5kYWxvbmUifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn19` |
| 2 | decide/approve_payment (REJECTED) | `https://verify.agentactioncapsule.org/v/08bec0383378c13cc8046964b3d4ffb8ebca2c573f3b26305f026bed0aa8b4cd#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIwOGJlYzAzODMzNzhjMTNjYzgwNDY5NjRiM2Q0ZmZiOGViY2EyYzU3M2YzYjI2MzA1ZjAyNmJlZDBhYThiNGNkIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX3BheW1lbnQvY2Y5NTkzZjAtZTM3Yy00NTQ0LWExZjktMjViYzcyOGFlMmE1IiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJpbnZvaWNlLWFnZW50QHYxIiwgInRpbWVzdGFtcCI6ICIyMDI2LTA4LTAzVDAwOjMyOjQxLjQ0OTkzMloiLCAibW9kZWxfYXR0ZXN0YXRpb24iOiB7ImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI4NzE4ZjYyNGQ2MTk3YmVkNzc0NmIwNjZlOGZhNGU5YWRhYmVjNDdjZDhhYzYzZTVlNzQyMzBiZTI4NWYzNThmIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZGNmOGJhZTdhYzU4NDhjM2IxZjk1ZTcwMWEyNjFiNTYzMDE3MjEyNGYzYWEwNmE5MjViOWFkMjg2ZDE3OTQxMiIsICJydW50aW1lIjogImRhcHJfYWdlbnRzIiwgImRhcHJfYWdlbnRzIjogeyJhZ2VudF9uYW1lIjogImludm9pY2UtY2hlY2tlciIsICJ0b29sX25hbWUiOiAiYXBwcm92ZV9wYXltZW50IiwgIndvcmtmbG93X2luc3RhbmNlX2lkIjogIndmLWRlbW8tMjAyNi0wNy0zMCIsICJhcHBfaWQiOiAiaW52b2ljZS1hcHAiLCAiYXBwcm92ZXJfaWQiOiAiYm9iQGFjbWUuY29tIn19fSwgImVmZmVjdCI6IHsic3RhdHVzIjogInBsYW5uZWQiLCAidHlwZSI6ICJhcHByb3ZlX3BheW1lbnQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogIm5vdF9hcHBsaWNhYmxlIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJyZWplY3QiLCAiYXBwcm92ZXIiOiAiaHVtYW4iLCAiaHVtYW5fZGlzcG9zZWQiOiB0cnVlLCAidmVyZGljdF9jbGFzcyI6ICJibG9ja2VkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiYjgyM2FhOGY1YmU5NjgwODQyMzUwNDNkMWIwZDdkZDNhNjZjYjYwZDE2ZjcyNGRkY2ZjOGQ1NGE1Y2E1ZTk3OCIsICJyZWxhdGlvbiI6ICJjb25maXJtcyJ9fQ==` |
| 3 | fyi/escalate_to_manager | `https://verify.agentactioncapsule.org/v/ff507525ececb1d1c6bdb1de7348ef7673dbe7a9d36ed5002d597ae447c6d710#eyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJmZjUwNzUyNWVjZWNiMWQxYzZiZGIxZGU3MzQ4ZWY3NjczZGJlN2E5ZDM2ZWQ1MDAyZDU5N2FlNDQ3YzZkNzEwIiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyLzE5NDM0OTU0LTQ2MGQtNGQ1My05NTcwLWI5OGU1YjA2MGIwNSIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiaW52b2ljZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wM1QwMDozMjo0Mi40NDM4NDZaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjgwYjIwNTU4NDc4YmM4MTdjMzQzZTAxNGU5M2UzMDc3ZjczODRlN2NkOGU0YjJjN2YyY2Y3ZTI3MDgzMTk4YSIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImIxM2Y4YmE0YmY4NGRlYjBlNzdiMjdhN2I1NzgyMDU5MWNjOGU2MDNhMmVmMTQ1MmIwMDZjZTU4ZTM3ODE3YmYiLCAicnVudGltZSI6ICJkYXByX2FnZW50cyIsICJkYXByX2FnZW50cyI6IHsiYWdlbnRfbmFtZSI6ICJpbnZvaWNlLWNoZWNrZXIiLCAidG9vbF9uYW1lIjogImVzY2FsYXRlX3RvX21hbmFnZXIiLCAid29ya2Zsb3dfaW5zdGFuY2VfaWQiOiAid2YtZGVtby0yMDI2LTA3LTMwIiwgImFwcF9pZCI6ICJpbnZvaWNlLWFwcCJ9fX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiMDhiZWMwMzgzMzc4YzEzY2M4MDQ2OTY0YjNkNGZmYjhlYmNhMmM1NzNmM2IyNjMwNWYwMjZiZWQwYWE4YjRjZCIsICJyZWxhdGlvbiI6ICJjb25maXJtcyJ9fQ==` |

**Full 3-capsule chain bundle (recommended for the demo — enables Chain Navigation table with
Previous/Next click-through and a VERDICT column showing executed → blocked → executed):**

`https://verify.agentactioncapsule.org/v/b823aa8f5be968084235043d1b0d7dd3a66cb60d16f724ddcfc8d54a5ca5e978#W3sic3BlY192ZXJzaW9uIjogImRyYWZ0LW1paC1zY2l0dC1hZ2VudC1hY3Rpb24tY2Fwc3VsZS0wMiIsICJmb3JtYXRfdmVyc2lvbiI6ICIyIiwgImNhcHN1bGVfaWQiOiAiYjgyM2FhOGY1YmU5NjgwODQyMzUwNDNkMWIwZDdkZDNhNjZjYjYwZDE2ZjcyNGRkY2ZjOGQ1NGE1Y2E1ZTk3OCIsICJhY3Rpb25faWQiOiAiY2hlY2tfaW52b2ljZS82MWQ2ZDcyYy03ZGNkLTRlZGEtOWEwYi1lNjA3ZmFmZWJiMDQiLCAiYWN0aW9uX3R5cGUiOiAiZnlpIiwgIm9wZXJhdG9yIjogImFjbWUtY28iLCAiZGV2ZWxvcGVyIjogImludm9pY2UtYWdlbnRAdjEiLCAidGltZXN0YW1wIjogIjIwMjYtMDgtMDNUMDA6MzI6NDAuNzcxODc4WiIsICJtb2RlbF9hdHRlc3RhdGlvbiI6IHsiY29tcHV0ZV9hdHRlc3RhdGlvbiI6IHsiYWdlbnRfaW5wdXRfZGlnZXN0IjogImQ0NDM1YjljOGUyZjIzZWIwMzI1MDlmNWE5NzA1OTI5MDYyMDIzNGY0NDM4MzdkYjI5OTI5NTNlZDg0Mzc1YTEiLCAiYWdlbnRfb3V0cHV0X2RpZ2VzdCI6ICI5NDk0ODU1ODU5YjQ5ZTNiN2ZkMTdlYmM2ZTVlZjVkMWZhNmYyZjdkYmY4MTFiOTQ4NTJjOGJjNWE2M2EyMDhjIiwgInJ1bnRpbWUiOiAiZGFwcl9hZ2VudHMiLCAiZGFwcl9hZ2VudHMiOiB7ImFnZW50X25hbWUiOiAiaW52b2ljZS1jaGVja2VyIiwgInRvb2xfbmFtZSI6ICJjaGVja19pbnZvaWNlIiwgIndvcmtmbG93X2luc3RhbmNlX2lkIjogIndmLWRlbW8tMjAyNi0wNy0zMCIsICJhcHBfaWQiOiAiaW52b2ljZS1hcHAifX19LCAiZWZmZWN0IjogeyJzdGF0dXMiOiAiZGlzcGF0Y2hlZCIsICJ0eXBlIjogImNoZWNrX2ludm9pY2UiLCAiZWZmZWN0X2F0dGVzdGF0aW9uIjogInJ1bnRpbWVfY2xhaW1lZCJ9LCAiYXNzdXJhbmNlIjogeyJhdHRlc3RhdGlvbl9tb2RlIjogInNlbGZfYXR0ZXN0ZWQiLCAiZWZmZWN0X21vZGUiOiAiZGlzcGF0Y2hlZF91bmNvbmZpcm1lZCIsICJsZWRnZXJfbW9kZSI6ICJzdGFuZGFsb25lIn0sICJkaXNwb3NpdGlvbiI6IHsiZGVjaXNpb24iOiAiYWNjZXB0IiwgImFwcHJvdmVyIjogInBvbGljeSIsICJodW1hbl9kaXNwb3NlZCI6IGZhbHNlLCAidmVyZGljdF9jbGFzcyI6ICJleGVjdXRlZCJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICIwOGJlYzAzODMzNzhjMTNjYzgwNDY5NjRiM2Q0ZmZiOGViY2EyYzU3M2YzYjI2MzA1ZjAyNmJlZDBhYThiNGNkIiwgImFjdGlvbl9pZCI6ICJhcHByb3ZlX3BheW1lbnQvY2Y5NTkzZjAtZTM3Yy00NTQ0LWExZjktMjViYzcyOGFlMmE1IiwgImFjdGlvbl90eXBlIjogImRlY2lkZSIsICJvcGVyYXRvciI6ICJhY21lLWNvIiwgImRldmVsb3BlciI6ICJpbnZvaWNlLWFnZW50QHYxIiwgInRpbWVzdGFtcCI6ICIyMDI2LTA4LTAzVDAwOjMyOjQxLjQ0OTkzMloiLCAibW9kZWxfYXR0ZXN0YXRpb24iOiB7ImNvbXB1dGVfYXR0ZXN0YXRpb24iOiB7ImFnZW50X2lucHV0X2RpZ2VzdCI6ICI4NzE4ZjYyNGQ2MTk3YmVkNzc0NmIwNjZlOGZhNGU5YWRhYmVjNDdjZDhhYzYzZTVlNzQyMzBiZTI4NWYzNThmIiwgImFnZW50X291dHB1dF9kaWdlc3QiOiAiZGNmOGJhZTdhYzU4NDhjM2IxZjk1ZTcwMWEyNjFiNTYzMDE3MjEyNGYzYWEwNmE5MjViOWFkMjg2ZDE3OTQxMiIsICJydW50aW1lIjogImRhcHJfYWdlbnRzIiwgImRhcHJfYWdlbnRzIjogeyJhZ2VudF9uYW1lIjogImludm9pY2UtY2hlY2tlciIsICJ0b29sX25hbWUiOiAiYXBwcm92ZV9wYXltZW50IiwgIndvcmtmbG93X2luc3RhbmNlX2lkIjogIndmLWRlbW8tMjAyNi0wNy0zMCIsICJhcHBfaWQiOiAiaW52b2ljZS1hcHAiLCAiYXBwcm92ZXJfaWQiOiAiYm9iQGFjbWUuY29tIn19fSwgImVmZmVjdCI6IHsic3RhdHVzIjogInBsYW5uZWQiLCAidHlwZSI6ICJhcHByb3ZlX3BheW1lbnQifSwgImFzc3VyYW5jZSI6IHsiYXR0ZXN0YXRpb25fbW9kZSI6ICJzZWxmX2F0dGVzdGVkIiwgImVmZmVjdF9tb2RlIjogIm5vdF9hcHBsaWNhYmxlIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJyZWplY3QiLCAiYXBwcm92ZXIiOiAiaHVtYW4iLCAiaHVtYW5fZGlzcG9zZWQiOiB0cnVlLCAidmVyZGljdF9jbGFzcyI6ICJibG9ja2VkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiYjgyM2FhOGY1YmU5NjgwODQyMzUwNDNkMWIwZDdkZDNhNjZjYjYwZDE2ZjcyNGRkY2ZjOGQ1NGE1Y2E1ZTk3OCIsICJyZWxhdGlvbiI6ICJjb25maXJtcyJ9fSwgeyJzcGVjX3ZlcnNpb24iOiAiZHJhZnQtbWloLXNjaXR0LWFnZW50LWFjdGlvbi1jYXBzdWxlLTAyIiwgImZvcm1hdF92ZXJzaW9uIjogIjIiLCAiY2Fwc3VsZV9pZCI6ICJmZjUwNzUyNWVjZWNiMWQxYzZiZGIxZGU3MzQ4ZWY3NjczZGJlN2E5ZDM2ZWQ1MDAyZDU5N2FlNDQ3YzZkNzEwIiwgImFjdGlvbl9pZCI6ICJlc2NhbGF0ZV90b19tYW5hZ2VyLzE5NDM0OTU0LTQ2MGQtNGQ1My05NTcwLWI5OGU1YjA2MGIwNSIsICJhY3Rpb25fdHlwZSI6ICJmeWkiLCAib3BlcmF0b3IiOiAiYWNtZS1jbyIsICJkZXZlbG9wZXIiOiAiaW52b2ljZS1hZ2VudEB2MSIsICJ0aW1lc3RhbXAiOiAiMjAyNi0wOC0wM1QwMDozMjo0Mi40NDM4NDZaIiwgIm1vZGVsX2F0dGVzdGF0aW9uIjogeyJjb21wdXRlX2F0dGVzdGF0aW9uIjogeyJhZ2VudF9pbnB1dF9kaWdlc3QiOiAiZjgwYjIwNTU4NDc4YmM4MTdjMzQzZTAxNGU5M2UzMDc3ZjczODRlN2NkOGU0YjJjN2YyY2Y3ZTI3MDgzMTk4YSIsICJhZ2VudF9vdXRwdXRfZGlnZXN0IjogImIxM2Y4YmE0YmY4NGRlYjBlNzdiMjdhN2I1NzgyMDU5MWNjOGU2MDNhMmVmMTQ1MmIwMDZjZTU4ZTM3ODE3YmYiLCAicnVudGltZSI6ICJkYXByX2FnZW50cyIsICJkYXByX2FnZW50cyI6IHsiYWdlbnRfbmFtZSI6ICJpbnZvaWNlLWNoZWNrZXIiLCAidG9vbF9uYW1lIjogImVzY2FsYXRlX3RvX21hbmFnZXIiLCAid29ya2Zsb3dfaW5zdGFuY2VfaWQiOiAid2YtZGVtby0yMDI2LTA3LTMwIiwgImFwcF9pZCI6ICJpbnZvaWNlLWFwcCJ9fX0sICJlZmZlY3QiOiB7InN0YXR1cyI6ICJkaXNwYXRjaGVkIiwgInR5cGUiOiAiZXNjYWxhdGVfdG9fbWFuYWdlciIsICJlZmZlY3RfYXR0ZXN0YXRpb24iOiAicnVudGltZV9jbGFpbWVkIn0sICJhc3N1cmFuY2UiOiB7ImF0dGVzdGF0aW9uX21vZGUiOiAic2VsZl9hdHRlc3RlZCIsICJlZmZlY3RfbW9kZSI6ICJkaXNwYXRjaGVkX3VuY29uZmlybWVkIiwgImxlZGdlcl9tb2RlIjogImNoYWluZWQifSwgImRpc3Bvc2l0aW9uIjogeyJkZWNpc2lvbiI6ICJhY2NlcHQiLCAiYXBwcm92ZXIiOiAicG9saWN5IiwgImh1bWFuX2Rpc3Bvc2VkIjogZmFsc2UsICJ2ZXJkaWN0X2NsYXNzIjogImV4ZWN1dGVkIn0sICJjaGFpbiI6IHsicGFyZW50X2NhcHN1bGVfaWQiOiAiMDhiZWMwMzgzMzc4YzEzY2M4MDQ2OTY0YjNkNGZmYjhlYmNhMmM1NzNmM2IyNjMwNWYwMjZiZWQwYWE4YjRjZCIsICJyZWxhdGlvbiI6ICJjb25maXJtcyJ9fV0=`

Click sequence for the denial beat: open the bundle permalink → chain-navigation table shows
all 3 capsules with `# / CAPSULE_ID / ACTION_TYPE / VERDICT / APPROVER / TIMESTAMP` → row 2 reads
`decide | blocked | human` → click row 2 (or "Next") to load capsule 2 standalone, which shows
the anchor banner (`✓ Anchored log index 243 · inclusion proof verified (RFC 9162)`),
the digest graph (chains_to → capsule 1, attests_over agent_input/agent_output), and the privilege
log (agent_input/agent_output both WITHHELD — digest committed, payload not carried in the
record).

**Live-verified (this session, fresh page loads, zero pasting):**
- Capsule 1 permalink → renders anchor banner "Anchored log index 242 · inclusion proof verified (RFC 9162)"
- Capsule 2 (denial) permalink → renders anchor banner "Anchored log index 243 · inclusion proof verified (RFC 9162)", digest graph, privilege log
- Bundle permalink → renders full 3-row chain navigation table with correct verdicts (executed / blocked / executed) and Previous/Next buttons

---

## Auto-load-fragment finding (investigated per task)

**Finding: NOT a bug — auto-load-fragment parsing is currently wired and working correctly.**

Tested against the known-good baseline (refund-chain leaf 212, capsule_id
`56bebce8ec982f7d8f5c1a4d62be58a33930994e96e384804c28bbdd9e1bc419`, from
`scitt-cose/demo/fixtures/capsule_c_deny.json`) with both fragment formats:
- Bare single-capsule JSON object (base64-encoded, not array-wrapped)
- JSON array bundle (matching `bundle_permalink_stubs` format in `anchor_results.json`)

**Both formats fully auto-rendered on a fresh page load** — anchor banner, digest graph, edges
table, and privilege log all appeared with zero manual interaction. Confirmed identically against
all 3 new dapr-agents capsules above (individual + bundle permalinks).

Source-level confirmation in the deployed `/static/capsule.js`:
- Lines 339–346: auto-load for a single-capsule object fragment (`if(!Array.isArray(_fragData)){loadCapsule(_fragData);}`), runs unconditionally on script load by reading `location.hash`.
- Lines 474–486: auto-load for an array/bundle fragment, sets up `_bundle`, renders the chain table, and loads capsule 0 — also runs unconditionally on script load.

The always-visible "Paste capsule JSON" textarea + "Load capsule" button on the page is a
**separate, independent entry point** for a different artifact type (bilateral/escrow terms —
placeholder shows `{"buyer_capsule": {...}, "seller_capsule": {...}, ...}`), not a
required fallback for the basic capsule/bundle case. It remains visible as an alternative way to
load a capsule interactively; it does not indicate that fragment auto-load is broken.

**This contradicts the PM's earlier same-day (2026-08-02) report** that fragment auto-load did
not work and required manual paste. Possible explanations (not confirmed — no evidence of a
same-day redeploy was found in `outbox.md`): a stale browser cache/service worker on the PM's
side, or a subtle difference in the exact fragment tested. Whatever the original cause, **the
live surface today renders correctly with zero pasting**, matching the intent described in
`scitt-cose/demo/README.md`'s `bundle_permalink_stubs` section.

**Recommendation:** No fix needed. The crib doc's "cold verification... zero pasting" line does
**not** need correction — it is accurate as currently deployed. Suggest a quick re-check on the
PM's original browser/device before the call, only if time allows, to rule out a local caching
artifact on that machine specifically.

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

**Capsule 2 chains to capsule 1, capsule 3 chains to capsule 2** via `chain.parent_capsule_id` —
a verifier can follow the full sequence: tool call → human denial → escalation past the denial,
all three anchored, all three verified, the chain intact across the blocked action.
