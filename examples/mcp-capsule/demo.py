#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""MCP capsule demo — wrap a tool call, get a verifiable record.

"Any MCP tool call → verifiable record, in one decorator."

This demo shows the capsule-emit + MCP compose pattern end-to-end:
  1. Wrap — decorate a tool function with @emitter.tool
  2. Call — invoke the tool normally (MCP dispatch calls it the same way)
  3. Record — capsule is emitted automatically: sealed, anchored, ledger-written
  4. Verify — any party, any machine, offline: agent-action-capsule verify

Run:
    pip install "capsule-emit[dev]"
    python examples/mcp-capsule/demo.py

Offline (skip anchor network call):
    python examples/mcp-capsule/demo.py --no-anchor

Compose posture — where capsule-emit fits in an MCP stack:

    ┌─────────────────────────────────────────────┐
    │  MCP client (LLM or agent)                  │
    │    ↓  tool_call { name, arguments }         │
    │  MCP server (your Python code)              │
    │    ↓  @server.tool  +  @emitter.tool        │  ← both decorators, one function
    │  tool handler (this function)               │
    │    → capsule-emit records INPUT+OUTPUT      │  ← record layer, alongside MCP
    │        by digest, then returns the result   │
    │    ↑  tool_result { content }               │
    │  MCP client                                  │
    └─────────────────────────────────────────────┘

The capsule does NOT live inside the MCP message.  The MCP protocol is
unchanged.  capsule-emit is the record layer you **compose into** your
MCP server — the capsule references the tool call by digest (SHA-256 of
the canonical JSON of the inputs and output); you hold the raw values.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_action_capsule import verify

from capsule_emit import ledger_view, read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

LEDGER_PATH = Path(tempfile.mkdtemp()) / "mcp_capsule_ledger.jsonl"


# ---------------------------------------------------------------------------
# Step 0 — Set up the emitter (one per agent / service)
# ---------------------------------------------------------------------------

emitter = MCPCapsuleEmitter(
    operator="acme-co",       # accountable tenant
    developer="order-agent@v1",
    ledger=LEDGER_PATH,
    # anchor=True by default — submits the digest (only) to the public log
)


# ---------------------------------------------------------------------------
# Step 1 — Decorate your tool
# ---------------------------------------------------------------------------
# In a real MCP server you'd stack @server.tool on top:
#
#   @server.tool()            ← MCP protocol layer
#   @emitter.tool("submit_order")    ← record layer (capsule-emit)
#   def submit_order(...): ...
#
# Without the MCP SDK, the decorator works identically on any callable.
#
# capsule-emit records INPUT and OUTPUT by digest (SHA-256 of canonical JSON);
# you hold the raw values.  Nothing leaves your machine except the digest.

@emitter.tool("submit_order")
def submit_order(vendor: str, amount: str, po_number: str) -> dict:
    """Submit a purchase order to the vendor system (consequential action).

    Note: monetary values are carried as exact decimal STRINGS, not floats —
    §5.1 requires this for any digest-bearing field (a float like 4210.0 has no
    exact decimal representation, so capsule-emit fails closed on it).
    """
    # --- your actual tool logic here ---
    return {
        "status": "dispatched",
        "po_number": po_number,
        "vendor": vendor,
        "amount_usd": amount,
        "confirmation_ref": f"CONF-{po_number[-4:]}",
    }


def main(anchor: bool = True) -> int:
    emitter._anchor = anchor

    print("=== capsule-emit + MCP demo ===")
    print("wrap any MCP tool call → verifiable record, in one decorator\n")

    # -----------------------------------------------------------------------
    # Step 2 — Call the tool (MCP dispatch does this; so does any caller)
    # -----------------------------------------------------------------------
    print("Step 1/4 — Call the tool")
    result = submit_order(vendor="Frobozz Supply", amount="4210.00", po_number="PO-2026-0047")
    print(f"  tool returned: {result}\n")

    # -----------------------------------------------------------------------
    # Step 3 — Inspect the capsule that was emitted automatically
    # -----------------------------------------------------------------------
    cap = emitter.last
    assert cap is not None, "emitter.last is None — emit failed"

    print("Step 2/4 — Capsule emitted automatically")
    print(f"  capsule_id      : {cap.capsule_id}")
    print(f"  anchored        : {cap.anchored}  (digest submitted to public log)")
    print()

    c = cap.capsule
    compute = c["model_attestation"]["compute_attestation"]
    print("  input committed by digest  (raw values stay LOCAL):")
    print(f"    agent_input_digest  : {compute['agent_input_digest']}")
    print(f"    agent_output_digest : {compute['agent_output_digest']}")
    print()

    # -----------------------------------------------------------------------
    # Step 4 — Verify (any party, offline, from the bytes alone)
    # -----------------------------------------------------------------------
    print("Step 3/4 — Verify the capsule")
    vr = verify(c)
    if not vr.ok:
        print(f"  FAIL — {vr.findings}", file=sys.stderr)
        return 1
    print("  ✓ ok — tamper any byte and this fails\n")

    # -----------------------------------------------------------------------
    # Step 5 — CLI verify (identical to what a third party runs)
    # -----------------------------------------------------------------------
    print("Step 4/4 — CLI verify (what an auditor runs)")
    cmd = ["agent-action-capsule", "verify", "--store", str(LEDGER_PATH)]
    print(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout, end="")
        print(proc.stderr, end="", file=sys.stderr)
        return 1
    for line in proc.stdout.strip().splitlines():
        print(f"  {line}")
    print()

    # -----------------------------------------------------------------------
    # Ledger view
    # -----------------------------------------------------------------------
    print(f"Ledger ({LEDGER_PATH}):")
    ledger_view(LEDGER_PATH)
    print()

    # -----------------------------------------------------------------------
    # Assert regression: digests present even without a model= argument
    # -----------------------------------------------------------------------
    assert "agent_input_digest" in compute, "regression: input digest missing (no model)"
    assert "agent_output_digest" in compute, "regression: output digest missing (no model)"

    print("✓ Done. Copy this pattern into your MCP server.")
    print("  Replace `submit_order` with any consequential tool — emit handles the rest.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="capsule-emit MCP demo")
    parser.add_argument(
        "--no-anchor",
        action="store_true",
        help="skip the async anchor POST (run fully offline)",
    )
    args = parser.parse_args()
    sys.exit(main(anchor=not args.no_anchor))
