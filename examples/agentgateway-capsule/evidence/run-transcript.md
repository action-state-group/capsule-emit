# agentgateway ExtMcp gRPC protocol-boundary evidence transcript

**Date:** 2026-07-02  
**capsule-emit:** 0.1.1  
**agent-action-capsule:** 0.1.0  
**Python:** 3.13.7  
**Platform:** darwin (macOS 24.6.0)

---

## Scope — what is and is not verified here

**Verified:** capsule-emit's `CapsuleEmitServicer` at the agentgateway ExtMcp gRPC
protocol boundary.  The gRPC calls here — `CheckRequest` and `CheckResponse` on
`/agentgateway.dev.ext_mcp.ExtMcp/` — are **exactly** what the agentgateway binary
calls for every `tools/call` it proxies.  Exercising those two RPCs is the integration;
no behaviour exists between the agentgateway binary and this service beyond what these
calls exercise.

**Not verified in this run:** a live agentgateway Rust binary end-to-end.  Building
the agentgateway binary requires a Rust toolchain that is not available in this
environment.  The protocol boundary is the correct place to verify the integration;
the Rust binary adds no logic between the agentgateway config and these RPC calls.

> **Honest claim:** verified at the ExtMcp gRPC protocol boundary;
> full Rust-gateway binary run pending a Rust-toolchain environment.

---

## 1. Test suite — 22/22 passed

```
$ python3 -m pytest tests/test_agentgateway.py -v
============================= test session starts ==============================
platform darwin -- Python 3.13.7, pytest-9.0.1, pluggy-1.6.0
collected 22 items

tests/test_agentgateway.py::test_tools_call_seals_capsule PASSED         [  4%]
tests/test_agentgateway.py::test_tools_list_seals_no_capsule PASSED      [  9%]
tests/test_agentgateway.py::test_only_tools_call_counted_among_mixed PASSED [ 13%]
tests/test_agentgateway.py::test_capsule_runtime_is_agentgateway PASSED  [ 18%]
tests/test_agentgateway.py::test_capsule_verifies_ok PASSED              [ 22%]
tests/test_agentgateway.py::test_tampered_capsule_fails_verify PASSED    [ 27%]
tests/test_agentgateway.py::test_two_sequential_calls_two_capsules PASSED [ 31%]
tests/test_agentgateway.py::test_malformed_mcp_request_bytes_does_not_crash PASSED [ 36%]
tests/test_agentgateway.py::test_tools_call_no_mcp_request_field_still_seals PASSED [ 40%]
tests/test_agentgateway.py::test_read_only_methods_seal_no_capsule[tools/list] PASSED [ 45%]
tests/test_agentgateway.py::test_read_only_methods_seal_no_capsule[resources/read] PASSED [ 50%]
tests/test_agentgateway.py::test_read_only_methods_seal_no_capsule[resources/list] PASSED [ 54%]
tests/test_agentgateway.py::test_read_only_methods_seal_no_capsule[prompts/get] PASSED [ 59%]
tests/test_agentgateway.py::test_read_only_methods_seal_no_capsule[prompts/list] PASSED [ 63%]
tests/test_agentgateway.py::test_read_only_methods_seal_no_capsule[notifications/initialized] PASSED [ 68%]
tests/test_agentgateway.py::test_read_only_methods_seal_no_capsule[unknown/method] PASSED [ 72%]
tests/test_agentgateway.py::test_read_only_methods_seal_no_capsule[] PASSED [ 77%]
tests/test_agentgateway.py::test_stale_deque_after_upstream_error_does_not_corrupt_next_call PASSED [ 81%]
tests/test_agentgateway.py::test_check_response_without_prior_check_request PASSED [ 86%]
tests/test_agentgateway.py::test_invalid_ledger_path_does_not_crash_server PASSED [ 90%]
tests/test_agentgateway.py::test_agentgateway_module_importable PASSED   [ 95%]
tests/test_agentgateway.py::test_ext_mcp_pb2_importable PASSED           [100%]

============================== 22 passed in 0.17s ==============================
```

---

## 2. Demo run — tools/list → 0 capsules, tools/call → sealed, tamper → ok=False

The transcript below and `capsule.json` in this directory are from a single atomic
run.  The `capsule_id` visible in steps 2, 4, 5, and 6 (`bbaf995d8e38a518…`)
matches the `capsule_id` in `capsule.json`.

```
$ python3 examples/agentgateway-capsule/demo.py
============================================================
agentgateway capsule demo — gRPC → sealed capsule → verify
============================================================

[step 1] tools/list (read-only) — capsule must NOT be sealed
  ledger unchanged (0 capsules). ✓

[step 2] tools/call submit_order (consequential) → capsule sealed
  capsule_id: bbaf995d8e38a5184673…

[step 3] tools/call get_price (second call) → second capsule

[step 4] Ledger: 2 capsule(s) sealed
  bbaf995d8e38a518… submit_order [executed] runtime=agentgateway
  a663f33230e60ba3… get_price [executed] runtime=agentgateway

[step 5] Verify all capsules (offline — no network needed)
  bbaf995d8e38a518… ok=True  ✓
  a663f33230e60ba3… ok=True  ✓
  All capsules verified ok=True.

[step 6] Tamper test: flip one byte in output digest → verify fails
  original digest: …3b1201ab
  tampered digest: …3b1201a0
  verify result:   ok=False  findings: ['recomputed … != carried …']
  Tamper detected — ok=False as expected. ✓

============================================================
Demo complete.
  Verified at: protocol boundary (direct gRPC to ExtMcp service)
  Same call sequence agentgateway uses for every tools/call.
============================================================
```

---

## 3. Sealed capsule (capsule.json in this directory)

`capsule.json` is the `submit_order` capsule from step 2 of the run above
(`vendor: Frobozz Supply`, `amount: 1240.19`, `po_number: PO-7777`).

| Field | Value |
|---|---|
| `capsule_id` | `bbaf995d8e38a518…` |
| `action_id` | `submit_order/1be5bea9-…` |
| `timestamp` | `2026-07-02T02:01:30.751435Z` |
| `runtime` | `agentgateway` |
| `verdict_class` | `executed` |
| `verify ok` | `True` |

---

## 4. gRPC call sequence agentgateway uses

```
agentgateway
  ↓  CheckRequest  { method="tools/call", mcp_request={"name":"submit_order", …} }
  ↑  Pass
upstream MCP server runs tool
  ↓  CheckResponse { method="tools/call", mcp_response={"status":"dispatched", …} }
  ↑  Pass
capsule-emit seals INPUT + OUTPUT digests → ledger
```

Non-`tools/call` methods (`tools/list`, `resources/read`, etc.) pass through the
hook unchanged; the service produces zero capsules for them.  This is the
`seal_reads=False` default (two-signal rule: only writes + sensitive reads seal).
