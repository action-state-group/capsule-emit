#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""agentgateway capsule demo — gRPC tool call → sealed capsule → verify ok=True → tamper → fail.

This demo exercises the capsule-emit agentgateway ExtMcp gRPC service at the
protocol boundary — the same path agentgateway takes when it calls the service
for every MCP tools/call.  No real agentgateway binary is needed; the gRPC
calls ARE the integration.

What it proves:
  1. tools/call (consequential) → capsule sealed, ok=True
  2. tools/call tampered → verify fails (ok=False)
  3. tools/list (read-only) → NO capsule (0 records added)

How agentgateway calls this service (same sequence as this demo):

    agentgateway
      ↓  CheckRequest  { method="tools/call", mcp_request={"name":"submit_order", "arguments":{…}} }
      ↑  Pass
    upstream MCP server runs tool
      ↓  CheckResponse { method="tools/call", mcp_response={"content":[…]} }
      ↑  Pass
    capsule-emit seals INPUT + OUTPUT digests → ledger

Run:
    pip install "capsule-emit[agentgateway,dev]"
    python examples/agentgateway-capsule/demo.py
"""
from __future__ import annotations

import json
import socket
import tempfile
from pathlib import Path

import grpc
from agent_action_capsule import verify

from capsule_emit import read_ledger
from capsule_emit.adapters import ext_mcp_pb2
from capsule_emit.adapters.agentgateway import CapsuleEmitServicer, _make_server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _grpc_client(port: int):
    """Return (check_request, check_response) callables against localhost:port."""
    channel = grpc.insecure_channel(f"localhost:{port}")
    check_request = channel.unary_unary(
        "/agentgateway.dev.ext_mcp.ExtMcp/CheckRequest",
        request_serializer=ext_mcp_pb2.McpRequest.SerializeToString,
        response_deserializer=ext_mcp_pb2.McpRequestResult.FromString,
    )
    check_response = channel.unary_unary(
        "/agentgateway.dev.ext_mcp.ExtMcp/CheckResponse",
        request_serializer=ext_mcp_pb2.McpResponse.SerializeToString,
        response_deserializer=ext_mcp_pb2.McpResponseResult.FromString,
    )
    return channel, check_request, check_response


def _tools_call(check_request, check_response, tool_name: str, arguments: dict, tool_result: dict):
    """Simulate one agentgateway mcpGuardrails round-trip for a tools/call."""
    params = json.dumps({"name": tool_name, "arguments": arguments}).encode()
    result_bytes = json.dumps(tool_result).encode()

    check_request(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["po-agent"],
        mcp_request=params,
    ))
    check_response(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=["po-agent"],
        mcp_response=result_bytes,
    ))


def _tools_list(check_request, check_response):
    """Simulate a tools/list call — agentgateway does NOT send this to the hook
    when 'tools/list' is absent from the methods config.  Here we send it anyway
    to prove the service ignores it (no capsule produced)."""
    result_bytes = json.dumps({"tools": [{"name": "submit_order"}]}).encode()
    check_request(ext_mcp_pb2.McpRequest(
        method="tools/list",
        service_names=["po-agent"],
    ))
    check_response(ext_mcp_pb2.McpResponse(
        method="tools/list",
        service_names=["po-agent"],
        mcp_response=result_bytes,
    ))


def main():
    print("=" * 60)
    print("agentgateway capsule demo — gRPC → sealed capsule → verify")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "agentgateway-capsules.jsonl"
        port = _free_port()

        # ── Start in-process gRPC server ──────────────────────────────────────
        servicer = CapsuleEmitServicer(
            operator="acme-co",
            developer="agentgateway-agent@v1",
            ledger=str(ledger),
            anchor=False,
        )
        server = _make_server(servicer, port, workers=2)
        server.start()

        channel, check_request, check_response = _grpc_client(port)

        # ── Step 1: read-only call (tools/list) — must produce NO capsule ─────
        print("\n[step 1] tools/list (read-only) — capsule must NOT be sealed")
        before = len(read_ledger(ledger))
        _tools_list(check_request, check_response)
        after = len(read_ledger(ledger))
        assert after == before, f"tools/list should not seal (before={before}, after={after})"
        print("  ledger unchanged (0 capsules). ✓")

        # ── Step 2: consequential calls (tools/call) ──────────────────────────
        print("\n[step 2] tools/call submit_order (consequential) → capsule sealed")
        _tools_call(
            check_request, check_response,
            tool_name="submit_order",
            arguments={"vendor": "Frobozz Supply", "amount": 1240.19, "po_number": "PO-7777"},
            tool_result={"status": "dispatched", "confirmation_ref": "CONF-7777"},
        )
        records = read_ledger(ledger)
        assert len(records) == 1, f"expected 1 capsule, got {len(records)}"
        print(f"  capsule_id: {records[0]['capsule_id'][:20]}…")

        print("\n[step 3] tools/call get_price (second call) → second capsule")
        _tools_call(
            check_request, check_response,
            tool_name="get_price",
            arguments={"vendor": "Frobozz Supply", "item": "widget"},
            tool_result={"unit_price_usd": 42.00, "currency": "USD"},
        )
        records = read_ledger(ledger)
        assert len(records) == 2, f"expected 2 capsules, got {len(records)}"

        # ── Step 3: Inspect ledger ────────────────────────────────────────────
        print(f"\n[step 4] Ledger: {len(records)} capsule(s) sealed")
        for r in records:
            cid = r.get("capsule_id", "?")[:16]
            action = r.get("action_id", "?").split("/")[0]
            verdict_cls = r.get("disposition", {}).get("verdict_class", "?")
            runtime = r.get("model_attestation", {}).get("compute_attestation", {}).get("runtime", "?")
            print(f"  {cid}… {action} [{verdict_cls}] runtime={runtime}")

        # ── Step 4: Verify all capsules ───────────────────────────────────────
        print("\n[step 5] Verify all capsules (offline — no network needed)")
        all_ok = True
        for r in records:
            vr = verify(r)
            cid = r.get("capsule_id", "?")[:16]
            status = "ok=True  ✓" if vr.ok else f"ok=False ✗ {[f.detail for f in vr.findings]}"
            print(f"  {cid}… {status}")
            if not vr.ok:
                all_ok = False
        assert all_ok, "expected all capsules ok=True"
        print("  All capsules verified ok=True.")

        # ── Step 5: Tamper one byte → verify must fail ────────────────────────
        print("\n[step 6] Tamper test: flip one byte in output digest → verify fails")
        raw = records[0]  # first tools/call capsule
        import copy
        tampered = copy.deepcopy(raw)
        ca = tampered.get("model_attestation", {}).get("compute_attestation", {})
        output_digest = ca.get("agent_output_digest", "")
        if output_digest:
            flipped = output_digest[:-1] + ("0" if output_digest[-1] != "0" else "1")
            tampered["model_attestation"]["compute_attestation"]["agent_output_digest"] = flipped
            vr_bad = verify(tampered)
            print(f"  original digest: …{output_digest[-8:]}")
            print(f"  tampered digest: …{flipped[-8:]}")
            print(f"  verify result:   ok={vr_bad.ok}  findings: {[f.detail for f in vr_bad.findings]}")
            assert not vr_bad.ok, "tampered capsule must not verify ok=True"
            print("  Tamper detected — ok=False as expected. ✓")

        channel.close()
        server.stop(grace=0)

    print("\n" + "=" * 60)
    print("Demo complete.")
    print("  Verified at: protocol boundary (direct gRPC to ExtMcp service)")
    print("  Same call sequence agentgateway uses for every tools/call.")
    print("=" * 60)


if __name__ == "__main__":
    main()
