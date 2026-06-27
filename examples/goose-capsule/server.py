#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Demo Goose extension — purchase-order tools, every call sealed into a capsule.

This is the file you'd hand to Goose as a custom MCP extension.  It wraps
two business-logic tools with @emitter.tool() so every tool call Goose makes
is automatically sealed into a verifiable Agent Action Capsule.

Add to ~/.config/goose/config.yaml:

    extensions:
      po_agent:
        enabled: true
        type: stdio
        name: po_agent
        description: "Purchase-order tools with capsule audit trail"
        cmd: python
        args: ["/path/to/examples/goose-capsule/server.py"]
        timeout: 30
        envs:
          CAPSULE_OPERATOR: "acme-co"
          CAPSULE_DEVELOPER: "goose-agent@v1"

After adding, every time Goose calls submit_order or get_price the call is
sealed into ledger.jsonl — verifiable with:

    agent-action-capsule verify --store ledger.jsonl

Compose posture:

    Goose (LLM agent)
      ↓  tool_call { name="submit_order", arguments={…} }
    MCP server (this file)
      ↓  @server.tool()  ← MCP protocol layer
         @emitter.tool() ← capsule-emit record layer
      tool handler runs
         → capsule-emit seals INPUT + OUTPUT digests
         → effect.status="dispatched"
      ↑  tool_result { content }
    Goose

The capsule does NOT live inside the MCP message.  The protocol is unchanged.
capsule-emit is the record layer composing into the server.

Run standalone (stdio MCP server):
    pip install "capsule-emit[mcp]" mcp
    python examples/goose-capsule/server.py

Verify after a session:
    agent-action-capsule verify --store ledger.jsonl
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from capsule_emit.adapters.mcp import MCPCapsuleEmitter

_OPERATOR = os.environ.get("CAPSULE_OPERATOR", "acme-co")
_DEVELOPER = os.environ.get("CAPSULE_DEVELOPER", "goose-agent@v1")
_LEDGER = os.environ.get("CAPSULE_LEDGER", "ledger.jsonl")

server = FastMCP("po-agent", instructions="Purchase-order agent with capsule audit trail.")

emitter = MCPCapsuleEmitter(
    operator=_OPERATOR,
    developer=_DEVELOPER,
    ledger=_LEDGER,
    anchor=False,  # set anchor=True to fire-and-forget digest to a transparency log
)


@server.tool()
@emitter.tool(effect_type="write_order")  # seeded registry value (§12 / REGISTRY.md §3)
def submit_order(vendor: str, amount: float, po_number: str) -> dict:
    """Submit a purchase order (consequential — every call sealed into a capsule)."""
    return {
        "status": "dispatched",
        "po_number": po_number,
        "vendor": vendor,
        "amount_usd": amount,
        "confirmation_ref": f"CONF-{po_number[-4:]}",
    }


@server.tool()
@emitter.tool(action_type="fyi")  # read-only: sealed as observation, not gate decision
def get_price(vendor: str, item: str) -> dict:
    """Look up the current price for an item from a vendor."""
    prices = {"widget": 42.00, "gadget": 128.50, "doohickey": 9.99}
    unit_price = prices.get(item.lower(), 0.00)
    return {"vendor": vendor, "item": item, "unit_price_usd": unit_price, "currency": "USD"}


if __name__ == "__main__":
    server.run()
