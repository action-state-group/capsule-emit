#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Approval record demo — block → pending → approve → resolved lineage.

Demonstrates the full approval-record + pending-action pattern:

  Step 1. Gate runs with a blocking constraint → blocked capsule emitted
  Step 2. list_pending() → blocked capsule shows as pending
  Step 3. Approver seals approval capsule chained to the blocked one
  Step 4. list_pending() → blocked capsule is resolved (no longer pending)
  Step 5. Both capsules verify ok=True
  Step 6. Full lineage printed: blocked → approved

Run:
    python3 examples/approval/demo.py
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile

from agent_action_capsule import verify

from capsule_emit.adapters.mcp import MCPCapsuleEmitter
from capsule_emit.approval import list_pending, seal_approval
from capsule_emit.constraints.apache import AmountUnderCap, VendorKnown
from capsule_emit.gate import gate_and_emit
from capsule_emit.ledger import view_chains

# ---------------------------------------------------------------------------
# Setup — temp ledger, no network
# ---------------------------------------------------------------------------

_tmpdir = tempfile.mkdtemp(prefix="approval-demo-")
_ledger = os.path.join(_tmpdir, "ledger.jsonl")

emitter = MCPCapsuleEmitter(
    operator="acme-co",
    developer="po-agent@v1",
    ledger=_ledger,
    anchor=False,
)

print("=" * 64)
print("Approval-record demo")
print(f"ledger: {_ledger}")
print("=" * 64)

# ---------------------------------------------------------------------------
# Step 1: gate blocks the action → blocked capsule emitted
# ---------------------------------------------------------------------------

print("\nStep 1 — gate blocks action (vendor not approved)")
print("-" * 64)

constraints = [
    AmountUnderCap(5000),
    VendorKnown({"Acme", "Globex"}),
]
inputs = {"vendor": "EvilCorp", "amount": 1200}
output = {"po_id": None}

blocked_events: list = []

gate_and_emit(
    action="write_po",
    constraints=constraints,
    inputs=inputs,
    output=output,
    emitter=emitter,
    on_block=lambda action, gr: blocked_events.append((action, gr)),
)

assert emitter.last is not None
blocked_capsule = emitter.last.capsule
blocked_id = blocked_capsule["capsule_id"]

# Compute the action digest (same method the approval capsule will reference)
action_digest = hashlib.sha256(
    json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()

print(f"blocked capsule_id : {blocked_id}")
print(f"verdict            : {blocked_capsule['disposition']['verdict_class']}")
print(f"effect.status      : {blocked_capsule.get('effect', {}).get('status')}")
print(f"action_digest      : {action_digest[:16]}…")

assert blocked_capsule["disposition"]["verdict_class"] == "blocked"
assert blocked_capsule.get("effect", {}).get("status") == "planned"

# ---------------------------------------------------------------------------
# Step 2: list_pending → shows the blocked capsule
# ---------------------------------------------------------------------------

print("\nStep 2 — list_pending before approval")
print("-" * 64)

pending_before = list_pending(_ledger)
print(f"pending count: {len(pending_before)}")
for p in pending_before:
    print(f"  - {p['capsule_id']} / verdict={p['disposition']['verdict_class']}")

assert len(pending_before) == 1
assert pending_before[0]["capsule_id"] == blocked_id
print("✓ blocked capsule appears in list_pending()")

# ---------------------------------------------------------------------------
# Step 3: approver reviews and seals approval
# ---------------------------------------------------------------------------

print("\nStep 3 — alice@org.example approves the blocked action")
print("-" * 64)

approval_result = seal_approval(
    blocked_capsule_id=blocked_id,
    approver_id="alice@org.example",
    decision="approve",
    action_digest=action_digest,
    ledger=_ledger,
    anchor=False,
    action="review_action",
    operator="acme-co",
    developer="approval-agent@v1",
)

approval_capsule = approval_result.capsule
approval_id = approval_result.capsule_id
approval_ca = approval_capsule["model_attestation"]["compute_attestation"]

print(f"approval capsule_id : {approval_id}")
print(f"verdict             : {approval_capsule['disposition']['verdict_class']}")
print(f"chain.relation      : {approval_capsule.get('chain', {}).get('relation')}")
print(f"chain.parent_id     : {approval_capsule.get('chain', {}).get('parent_capsule_id', '')[:16]}…")
print(f"approver_id         : {approval_ca.get('approver_id')}")
print(f"human_disposed      : {approval_ca.get('human_disposed')}")

assert approval_capsule["disposition"]["verdict_class"] == "executed"
assert approval_capsule["disposition"]["decision"] == "approve"
assert approval_capsule["chain"]["relation"] == "resolves"
assert approval_capsule["chain"]["parent_capsule_id"] == blocked_id
assert approval_ca["human_disposed"] is True
assert approval_ca["approver_id"] == "alice@org.example"

# ---------------------------------------------------------------------------
# Step 4: list_pending → blocked capsule now resolved
# ---------------------------------------------------------------------------

print("\nStep 4 — list_pending after approval")
print("-" * 64)

pending_after = list_pending(_ledger)
print(f"pending count: {len(pending_after)}")

assert len(pending_after) == 0, f"Expected 0 pending, got {len(pending_after)}"
print("✓ blocked capsule no longer in list_pending() after approval")

# ---------------------------------------------------------------------------
# Step 5: verify both capsules
# ---------------------------------------------------------------------------

print("\nStep 5 — verify both capsules")
print("-" * 64)

v_blocked = verify(blocked_capsule)
v_approval = verify(approval_capsule)

print(f"blocked capsule verify  : {v_blocked.ok}")
print(f"approval capsule verify : {v_approval.ok}")

assert v_blocked.ok, f"Blocked capsule failed verify: {v_blocked}"
assert v_approval.ok, f"Approval capsule failed verify: {v_approval}"
print("✓ both capsules verify ok=True")

# ---------------------------------------------------------------------------
# Step 6: full lineage
# ---------------------------------------------------------------------------

print("\nStep 6 — full lineage: blocked → approved")
print("-" * 64)

view_chains(_ledger)

print("=" * 64)
print("All demo steps passed.")
print("=" * 64)
