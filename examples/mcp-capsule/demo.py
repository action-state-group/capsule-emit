#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""MCP capsule demo — wrap a tool call, get a verifiable record.

"Any MCP tool call → verifiable record, in one decorator."

Shows the capsule-emit + MCP compose pattern end-to-end:
  1. Wrap  — decorate a tool with @emitter.tool (no action name needed)
  2. Call  — invoke normally (MCP dispatch calls it the same way)
  3. Trail — three consecutive calls produce three sealed ledger rows
  4. Verify — any party, offline: agent-action-capsule verify

Run:
    pip install "capsule-emit[dev]"
    python examples/mcp-capsule/demo.py             # anchored (live)
    python examples/mcp-capsule/demo.py --no-anchor # offline / sandbox

Compose posture — where capsule-emit fits in an MCP stack:

    ┌───────────────────────────────────────────────────────┐
    │  MCP client (LLM or agent)                            │
    │    ↓  tool_call { name, arguments }                   │
    │  MCP server (your Python code)                        │
    │    ↓  @server.tool()  ← MCP protocol layer            │
    │       @emitter.tool() ← record layer (capsule-emit)   │
    │  tool handler (this function)                         │
    │    → capsule-emit seals INPUT+OUTPUT digests          │
    │      effect.status="dispatched" (tool ran)            │
    │    ↑  tool_result { content }                         │
    │  MCP client                                            │
    └───────────────────────────────────────────────────────┘

The capsule does NOT live inside the MCP message.  The MCP protocol is
unchanged.  capsule-emit is the record layer you compose into your MCP
server.  The capsule commits INPUT and OUTPUT by SHA-256 digest (canonical
JSON); raw values stay local.

Verify bytes offline:
    agent-action-capsule verify --store ledger.jsonl

Verify inclusion on the public log (after anchoring):
    agent-action-capsule verify --transparent statement.cose ...
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


def run_demo(anchor: bool) -> int:
    # -----------------------------------------------------------------------
    # Step 0 — set up the emitter (anchor= at construction, not _anchor poke)
    # -----------------------------------------------------------------------
    emitter = MCPCapsuleEmitter(
        operator="acme-co",
        developer="order-agent@v1",
        ledger=LEDGER_PATH,
        anchor=anchor,  # True → submit a digest to the public log
        # anchor_wait blocks for a real confirmed/failed outcome instead of
        # the default non-blocking "submitted" — so this flagship demo shows
        # a genuine anchored=True, not an apology for anchored=False.
        anchor_wait=10.0 if anchor else None,
        # action_type defaults to None → auto-derives "decide" for
        # verdict="executed" — correct for consequential tool calls (§5.1)
    )

    # -----------------------------------------------------------------------
    # Step 1 — decorate your tool
    # -----------------------------------------------------------------------
    # @emitter.tool() with NO name → action name inferred from fn.__name__
    #
    # In a real MCP server stack both decorators:
    #   @server.tool()            # MCP protocol layer (outermost)
    #   @emitter.tool()           # record layer (innermost)
    #   def submit_order(...): ...
    #
    # functools.wraps preserves the signature so @server.tool() still sees
    # the real typed params and generates the correct JSON schema.

    @emitter.tool(effect_type="write_order")  # seeded registry value (§12 / REGISTRY.md §3)
    def submit_order(vendor: str, amount: str, po_number: str) -> dict:
        """Submit a purchase order (consequential action).

        ``amount`` is an exact decimal string (e.g. ``"1240.19"``), not a
        float — a raw JSON float in a digest-bearing field is a §5.1 error
        (see ``capsule_emit.core._digest``); capsule-emit fails closed rather
        than sealing a value that could never reproducibly re-verify.
        """
        return {
            "status": "dispatched",
            "po_number": po_number,
            "vendor": vendor,
            "amount_usd": amount,
            "confirmation_ref": f"CONF-{po_number[-4:]}",
        }

    # -----------------------------------------------------------------------
    # Step 2 — call the tool (three times for a ledger trail)
    # -----------------------------------------------------------------------
    print("=== capsule-emit + MCP demo ===")
    print("wrap any MCP tool → verifiable record trail, in one decorator")
    if anchor:
        print("(each call blocks up to 10s for a real anchor confirmation — "
              "pass --no-anchor to skip)\n")
    else:
        print()

    orders = [
        ("Frobozz Supply", "4210.00", "PO-2026-0047"),
        ("Acme Widgets",   "1380.50", "PO-2026-0048"),
        ("Zork Industries", "975.00", "PO-2026-0049"),
    ]

    for vendor, amount, po in orders:
        submit_order(vendor=vendor, amount=amount, po_number=po)
        cap = emitter.last
        assert cap is not None
        c = cap.capsule
        eff_status = c.get("effect", {}).get("status", "—")
        print(f"  {po}: effect.status={eff_status!r}  capsule_id={cap.capsule_id[:16]}…")

    print()

    # -----------------------------------------------------------------------
    # Step 3 — inspect one capsule
    # -----------------------------------------------------------------------
    cap = emitter.last
    c = cap.capsule
    compute = c["model_attestation"]["compute_attestation"]

    print("Latest capsule:")
    print(f"  action_id       : {c['action_id']}")
    print(f"  action_type     : {c['action_type']}  ← 'decide'=consequential action (§5.1); 'fyi'=observation-only")
    print(f"  runtime         : {compute.get('runtime')}   ← auto-set by adapter")
    print(f"  effect.status   : {c.get('effect', {}).get('status')}")
    print("    'dispatched'  = tool ran; outcome not yet confirmed by a second party")
    print("    'confirmed'   = use emit_capsule(effect={status:'confirmed'}) after confirmation")
    print(f"  capsule_id      : {cap.capsule_id}")
    print(f"  anchored        : {cap.anchored}  ← True only once a receipt confirms it")
    print(f"  anchor_status   : {cap.anchor_status}  ← 'submitted' means dispatched, not yet confirmed")
    print()

    print("  Input/output committed by digest (raw values stay LOCAL):")
    print(f"    agent_input_digest  : {compute['agent_input_digest']}")
    print(f"    agent_output_digest : {compute['agent_output_digest']}")
    print()

    # -----------------------------------------------------------------------
    # Step 4 — verify the capsule in-process
    # -----------------------------------------------------------------------
    vr = verify(c)
    if not vr.ok:
        print(f"  FAIL — {vr.findings}", file=sys.stderr)
        return 1
    print("  ✓ verify(capsule).ok — tamper any byte and this fails\n")

    # -----------------------------------------------------------------------
    # Step 5 — CLI verify (what an auditor runs offline from the bytes)
    # -----------------------------------------------------------------------
    print("CLI verify (offline — from the ledger bytes, no network needed):")
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

    print("To verify INCLUSION on the public log (after anchoring):")
    print("  $ agent-action-capsule verify --transparent statement.cose \\")
    print("      --issuer-key issuer_pub.pem [--log-key log_pub.pem --leaf-entry-hex <hex>]")
    print("  'substrate.receipt_verified: True' proves the digest is in the log.\n")

    # -----------------------------------------------------------------------
    # Ledger view (shows the three-capsule trail)
    # -----------------------------------------------------------------------
    print(f"Ledger trail ({LEDGER_PATH}):")
    ledger_view(LEDGER_PATH)
    print()

    records = read_ledger(LEDGER_PATH)
    assert len(records) == 3, f"expected 3 ledger rows, got {len(records)}"
    assert all(verify(r).ok for r in records), "one or more ledger rows failed verify"
    assert compute.get("runtime") == "mcp", "runtime='mcp' not set in compute_attestation"

    print("✓ Done. Copy this pattern into your MCP server.")
    print("  Replace submit_order with any consequential tool — emit handles the rest.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="capsule-emit MCP demo")
    parser.add_argument(
        "--no-anchor",
        action="store_true",
        help="skip the async anchor POST (run fully offline)",
    )
    args = parser.parse_args()
    sys.exit(run_demo(anchor=not args.no_anchor))
