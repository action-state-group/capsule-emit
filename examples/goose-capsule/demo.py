#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Goose capsule demo — tool call → sealed capsule → verify ok=True → tamper → fail.

Simulates exactly what Goose does when it calls a tool in the po-agent MCP
server: the wrapped function is invoked, capsule-emit seals INPUT + OUTPUT
digests into a ledger row, and agent-action-capsule verifies the capsule
offline.

"Any Goose tool call → verifiable record, in one decorator."

Run:
    pip install "capsule-emit[dev]"
    python examples/goose-capsule/demo.py             # default (anchor off)
    python examples/goose-capsule/demo.py --no-anchor # explicit offline mode

What Goose does (the same path as this demo):

    Goose
      ↓  tool_call { name="submit_order", arguments={…} }
    po-agent MCP server (examples/goose-capsule/server.py)
      @server.tool()      ← MCP layer (Goose connects via stdio)
      @emitter.tool()     ← capsule-emit seals here
      → ledger.jsonl += { capsule_id, action_id, … }
      ↑  tool_result

Connect the real Goose extension (no LLM required for sealing):
    1. pip install "capsule-emit[mcp]" mcp
    2. Add to ~/.config/goose/config.yaml (see server.py header)
    3. goose run -t "call submit_order with vendor=Frobozz, amount=1240.19, po_number=PO-7777"
    4. agent-action-capsule verify --store ledger.jsonl
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from agent_action_capsule import verify

from capsule_emit import read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

_ANCHOR = "--no-anchor" not in sys.argv

# ── 1. Build the emitter (same config used in server.py) ──────────────────

with tempfile.TemporaryDirectory() as _tmp:
    ledger = Path(_tmp) / "goose-capsules.jsonl"

    emitter = MCPCapsuleEmitter(
        operator="acme-co",
        developer="goose-agent@v1",
        ledger=ledger,
        anchor=_ANCHOR,
        model={"provider": "anthropic", "model_id": "claude-opus-4-8"},
    )

    # ── 2. Wrap tools — decorator order mirrors server.py ─────────────────
    #    @server.tool() would be the outermost; @emitter.tool() is the inner.
    #    In this standalone demo we skip @server.tool() (no MCP server needed).

    @emitter.tool(effect_type="write_order")
    def submit_order(vendor: str, amount: float, po_number: str) -> dict:
        """Submit a purchase order."""
        return {
            "status": "dispatched",
            "po_number": po_number,
            "vendor": vendor,
            "amount_usd": amount,
            "confirmation_ref": f"CONF-{po_number[-4:]}",
        }

    @emitter.tool(action_type="fyi")
    def get_price(vendor: str, item: str) -> dict:
        """Look up item price."""
        prices = {"widget": 42.00, "gadget": 128.50, "doohickey": 9.99}
        unit_price = prices.get(item.lower(), 0.00)
        return {"vendor": vendor, "item": item, "unit_price_usd": unit_price, "currency": "USD"}

    # ── 3. Simulate Goose tool calls ──────────────────────────────────────

    print("=" * 60)
    print("Goose capsule demo — tool call → sealed capsule → verify")
    print("=" * 60)

    print("\n[step 1] Goose calls get_price (read-only, action_type=fyi)")
    price_result = get_price(vendor="Frobozz Supply", item="widget")
    print(f"  tool returned: {price_result}")

    print("\n[step 2] Goose calls submit_order (consequential, write_order)")
    order_result = submit_order(vendor="Frobozz Supply", amount=1240.19, po_number="PO-7777")
    print(f"  tool returned: {order_result}")

    print("\n[step 3] Second order (chained — same session)")
    submit_order(vendor="Globex Corp", amount=550.00, po_number="PO-7778")

    # ── 4. Inspect the ledger ─────────────────────────────────────────────

    records = read_ledger(ledger)
    print(f"\n[step 4] Ledger: {len(records)} capsule(s) sealed")
    for r in records:
        cid = r.get("capsule_id", "?")[:16]
        action = r.get("action_id", "?").split("/")[0]
        verdict_cls = r.get("disposition", {}).get("verdict_class", "?")
        ca = r.get("model_attestation", {}).get("compute_attestation", {})
        runtime = ca.get("runtime", "?")
        print(f"  {cid}… {action} [{verdict_cls}] runtime={runtime}")

    # ── 5. Verify — should all be ok=True ────────────────────────────────

    print("\n[step 5] Verify all capsules (offline — no network needed)")
    all_ok = True
    for r in records:
        vr = verify(r)
        cid = r.get("capsule_id", "?")[:16]
        status = "ok=True  ✓" if vr.ok else f"ok=False ✗ {[f.detail for f in vr.findings]}"
        print(f"  {cid}… {status}")
        if not vr.ok:
            all_ok = False

    assert all_ok, "expected all capsules to verify ok=True"
    print("\n  All capsules verified ok=True.")

    # ── 6. Tamper test — one byte change must break verification ──────────

    print("\n[step 6] Tamper test: flip one byte in output digest → verify fails")
    raw = records[1]  # the first submit_order capsule
    tampered = json.loads(json.dumps(raw))
    ca = tampered.get("model_attestation", {}).get("compute_attestation", {})
    output_digest = ca.get("agent_output_digest", "")
    if output_digest:
        flipped = output_digest[:-1] + ("0" if output_digest[-1] != "0" else "1")
        tampered["model_attestation"]["compute_attestation"]["agent_output_digest"] = flipped
        vr_bad = verify(tampered)
        print(f"  original  digest:  …{output_digest[-8:]}")
        print(f"  tampered  digest:  …{flipped[-8:]}")
        print(f"  verify result:     ok={vr_bad.ok}  findings: {[f.detail for f in vr_bad.findings]}")
        assert not vr_bad.ok, "tampered capsule must not verify ok=True"
        print("  Tamper detected — ok=False as expected. ✓")
    else:
        print("  (no output_digest found — skipping tamper test)")

    print("\n" + "=" * 60)
    print("Demo complete.")
    print(f"  ledger path: {ledger}  (temp; deleted on exit)")
    print("  To use with real Goose: see examples/goose-capsule/server.py")
    print("=" * 60)
