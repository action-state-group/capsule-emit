#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Wicket gate demo — check->gate->seal with zero engine imports.

Demonstrates:
  Case 1: All constraints pass  -> sealed capsule with gate_checks + verify OK
  Case 2: Blocked with callback -> on_block fires, blocked capsule sealed
  MCP:    @emitter.tool(constraints=[...]) on sync functions (pass + block)

Run:
    python3 examples/wicket/demo.py
"""
from __future__ import annotations

import json
import sys
import os
import tempfile

from agent_action_capsule import verify

from capsule_emit.adapters.mcp import MCPCapsuleEmitter
from capsule_emit.constraints.apache import AmountUnderCap, VendorKnown
from capsule_emit.gate import GateBlockedError, gate_and_emit

# ---------------------------------------------------------------------------
# Shared emitter (anchor=False — no network call in the demo)
# ---------------------------------------------------------------------------

_tmpdir = tempfile.mkdtemp(prefix="wicket-demo-")
emitter = MCPCapsuleEmitter(
    operator="acme-co",
    developer="po-agent@v1",
    ledger=os.path.join(_tmpdir, "ledger.jsonl"),
    anchor=False,
)

# ---------------------------------------------------------------------------
# Case 1: All constraints pass
# ---------------------------------------------------------------------------

print("=" * 60)
print("Case 1: vendor=Acme, amount=1200 — should PASS")
print("=" * 60)

constraints = [
    AmountUnderCap(5000),
    VendorKnown({"Acme", "Globex"}),
]

inputs_1 = {"vendor": "Acme", "amount": 1200}
output_1 = {"po_id": "PO-001", "vendor": "Acme", "amount": 1200}

result_1 = gate_and_emit(
    action="write_po",
    constraints=constraints,
    inputs=inputs_1,
    output=output_1,
    emitter=emitter,
)

assert result_1 == output_1, "gate_and_emit must return output unchanged"
assert emitter.last is not None

capsule_1 = emitter.last.capsule
ca_1 = capsule_1["model_attestation"]["compute_attestation"]
gate_checks_1 = ca_1["gate_checks"]

print(f"capsule_id : {capsule_1['capsule_id']}")
print(f"verdict    : {capsule_1['disposition']['verdict_class']}")
print(f"gate_checks: {json.dumps(gate_checks_1, indent=2)}")

assert capsule_1["disposition"]["verdict_class"] == "executed"
assert all(c["passed"] for c in gate_checks_1), "All checks should pass"
assert len(gate_checks_1) == 2

v1 = verify(capsule_1)
assert v1.ok, f"Capsule did not verify: {v1}"
print(f"verify     : {v1.ok} (ok)")

# ---------------------------------------------------------------------------
# Case 2: Blocked with callback
# ---------------------------------------------------------------------------

print()
print("=" * 60)
print("Case 2: vendor=EvilCorp, amount=9999 — should BLOCK")
print("=" * 60)

blocked_events: list[dict] = []


def on_block(action: str, gate_result) -> None:
    blocked_events.append({"action": action, "gate_result": gate_result})
    failing = [r for r in gate_result.results if not r.passed]
    print(f"  [on_block] action={action!r}, failures:")
    for r in failing:
        print(f"    - {r.name}: {r.reason}")


emitter2 = MCPCapsuleEmitter(
    operator="acme-co",
    developer="po-agent@v1",
    ledger=os.path.join(_tmpdir, "ledger2.jsonl"),
    anchor=False,
)

inputs_2 = {"vendor": "EvilCorp", "amount": 9999}
output_2 = {"po_id": None}

result_2 = gate_and_emit(
    action="write_po",
    constraints=constraints,
    inputs=inputs_2,
    output=output_2,
    emitter=emitter2,
    on_block=on_block,
)

assert result_2 == output_2, "gate_and_emit must return output unchanged even on block"
assert len(blocked_events) == 1, "on_block must fire exactly once"
assert emitter2.last is not None

capsule_2 = emitter2.last.capsule
ca_2 = capsule_2["model_attestation"]["compute_attestation"]
gate_checks_2 = ca_2["gate_checks"]

print(f"capsule_id : {capsule_2['capsule_id']}")
print(f"verdict    : {capsule_2['disposition']['verdict_class']}")
print(f"gate_checks: {json.dumps(gate_checks_2, indent=2)}")

assert capsule_2["disposition"]["verdict_class"] == "blocked"
assert capsule_2.get("effect", {}).get("status") == "planned"
assert not all(c["passed"] for c in gate_checks_2), "Not all checks should pass"

v2 = verify(capsule_2)
assert v2.ok, f"Blocked capsule did not verify: {v2}"
print(f"verify     : {v2.ok} (ok)")

# ---------------------------------------------------------------------------
# MCP section: @emitter.tool(constraints=[...])
# ---------------------------------------------------------------------------

print()
print("=" * 60)
print("MCP: @emitter.tool(constraints=[...]) — pass case")
print("=" * 60)

mcp_emitter = MCPCapsuleEmitter(
    operator="acme-co",
    developer="po-agent@v1",
    ledger=os.path.join(_tmpdir, "ledger_mcp.jsonl"),
    anchor=False,
)


@mcp_emitter.tool(constraints=[AmountUnderCap(5000), VendorKnown({"Acme", "Globex"})])
def submit_order(vendor: str, amount: float) -> dict:
    return {"status": "accepted", "vendor": vendor, "amount": amount}


mcp_result = submit_order(vendor="Globex", amount=2500.0)
print(f"tool returned: {mcp_result}")

assert mcp_emitter.last is not None
mcp_ca = mcp_emitter.last.capsule["model_attestation"]["compute_attestation"]
print(f"gate_checks: {json.dumps(mcp_ca['gate_checks'], indent=2)}")

assert "gate_checks" in mcp_ca
assert all(c["passed"] for c in mcp_ca["gate_checks"])
assert verify(mcp_emitter.last.capsule).ok
print(f"verify: ok")

print()
print("=" * 60)
print("MCP: @emitter.tool(constraints=[...]) — block case")
print("=" * 60)

mcp_block_emitter = MCPCapsuleEmitter(
    operator="acme-co",
    developer="po-agent@v1",
    ledger=os.path.join(_tmpdir, "ledger_mcp_block.jsonl"),
    anchor=False,
)

mcp_block_calls: list = []


@mcp_block_emitter.tool(
    constraints=[AmountUnderCap(100)],
    on_block=lambda action, gr: mcp_block_calls.append((action, gr)),
)
def blocked_order(vendor: str, amount: float) -> dict:
    return {"status": "pending"}


blocked_result = blocked_order(vendor="Acme", amount=999.0)
print(f"tool returned: {blocked_result}")

assert mcp_block_emitter.last is not None
assert mcp_block_emitter.last.capsule["disposition"]["verdict_class"] == "blocked"
assert len(mcp_block_calls) == 1
print(f"on_block fired: action={mcp_block_calls[0][0]!r}")
print(f"verify: {verify(mcp_block_emitter.last.capsule).ok} (ok)")

print()
print("=" * 60)
print("MCP: @emitter.tool(constraints=[...]) — GateBlockedError (no callback)")
print("=" * 60)

err_emitter = MCPCapsuleEmitter(
    operator="acme-co",
    developer="po-agent@v1",
    ledger=os.path.join(_tmpdir, "ledger_err.jsonl"),
    anchor=False,
)


@err_emitter.tool(constraints=[AmountUnderCap(100)])
def order_no_callback(vendor: str, amount: float) -> dict:
    return {"status": "ok"}


try:
    order_no_callback(vendor="Acme", amount=9999.0)
    print("ERROR: should have raised GateBlockedError")
    sys.exit(1)
except GateBlockedError as e:
    print(f"GateBlockedError raised as expected: {e}")

print()
print("=" * 60)
print("All demo cases passed.")
print("=" * 60)
