#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Dapr Agents adapter demo — execution capsule + decide capsule side by side.

Shows two capsule types anchored to the live SCITT Transparency Service and
verified offline:

  Capsule 1 — fyi (execution record)
    Produced by @emitter.tool() as the agent calls a tool.
    Analogous to what the capsule-emit-dapr Go adapter produces from signed
    Dapr Workflow history — an observation of what the agent executed.

  Capsule 2 — decide (HITL decision record)
    Produced by emitter.record_hitl() at the approval gate.
    Records the REAL human decision (accept/reject, approver identity) as it
    happened — the live decision-point layer this adapter owns.

Usage:
    python3 examples/dapr-agents-capsule/demo.py

Set AAC_ANCHOR_URL to override the default anchor endpoint.
Requires: pip install capsule-emit agent-action-capsule
"""
from __future__ import annotations

import pathlib
import tempfile

from agent_action_capsule import verify

from capsule_emit.adapters.dapr_agents import DaprAgentsCapsuleEmitter


def run_demo() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = pathlib.Path(tmpdir) / "demo_ledger.jsonl"

        emitter = DaprAgentsCapsuleEmitter(
            operator="acme-co",
            developer="invoice-agent@v1",
            agent_name="invoice-checker",
            app_id="invoice-app",
            workflow_instance_id="wf-demo-2026-07-28",
            ledger=ledger,
            anchor=True,   # submits digest to live anchor
        )

        # ── Capsule 1: execution record (fyi) ────────────────────────────
        # Simulates what the agent's tool-call layer observes when the LLM
        # triggers check_invoice.  The Go adapter reads this kind of record
        # from signed Dapr Workflow history; this adapter records it live.

        @emitter.tool("check_invoice")
        def check_invoice(invoice_id: str, amount: str, vendor: str) -> dict:
            # Simulated tool logic (no live sidecar required)
            return {
                "invoice_id": invoice_id,
                "amount": amount,
                "vendor": vendor,
                "risk_score": "low",
                "flag": False,
            }

        check_invoice(
            invoice_id="INV-2026-001",
            amount="1240.00",
            vendor="Frobozz Supply Co.",
        )
        fyi_result = emitter.last

        print("\n─── Capsule 1: fyi (execution record) ────────────────────────────")
        print(f"  capsule_id : {fyi_result.capsule_id}")
        print(f"  action_type: {fyi_result.capsule['action_type']}")
        print(f"  verdict    : {fyi_result.capsule['disposition']['verdict_class']}")
        print(f"  anchored   : {fyi_result.anchored}")
        v1 = verify(fyi_result.capsule)
        print(f"  verified   : {v1.ok}")

        # ── Capsule 2: decide (HITL decision) ────────────────────────────
        # Simulates a human approver responding to wait_for_external_event().
        # In production: extract approver_id and decision from the actual
        # external event payload your auth layer delivers.
        #
        # NEVER fabricate this.  The approver_id and decision here represent
        # what alice@acme.com ACTUALLY decided — recorded as it happened.

        hitl_result = emitter.record_hitl(
            "approve_payment",
            approver_id="alice@acme.com",
            decision="accept",
            tool_request={
                "invoice_id": "INV-2026-001",
                "amount": "1240.00",
                "vendor": "Frobozz Supply Co.",
                "requested_by": "invoice-agent@v1",
            },
            outcome={
                "approved_at": "2026-07-28T10:00:00Z",
                "approval_reference": "APPR-7788",
            },
            prior_capsule_id=fyi_result.capsule_id,
        )

        print("\n─── Capsule 2: decide (HITL decision) ────────────────────────────")
        print(f"  capsule_id   : {hitl_result.capsule_id}")
        print(f"  action_type  : {hitl_result.capsule['action_type']}")
        print(f"  verdict      : {hitl_result.capsule['disposition']['verdict_class']}")
        print(f"  human_disposed: {hitl_result.capsule['disposition']['human_disposed']}")
        print(f"  decision     : {hitl_result.capsule['disposition']['decision']}")
        print(f"  chained to   : {hitl_result.capsule['chain']['parent_capsule_id']}")
        print(f"  anchored     : {hitl_result.anchored}")
        v2 = verify(hitl_result.capsule)
        print(f"  verified     : {v2.ok}")

        # ── Side-by-side summary ──────────────────────────────────────────
        print("\n─── Side-by-side summary ─────────────────────────────────────────")
        print(f"  fyi    capsule_id: {fyi_result.capsule_id}")
        print(f"  decide capsule_id: {hitl_result.capsule_id}")
        print(f"  Both verified    : {v1.ok and v2.ok}")
        print(f"  Both anchored    : {fyi_result.anchored and hitl_result.anchored}")

        if not (v1.ok and v2.ok):
            raise SystemExit("Verification failed — see output above")

        print("\nDone.  Both capsule_ids are live on the anchor.")
        print("Verify offline with: python3 -c \"")
        print("  from agent_action_capsule import verify")
        print("  import json")
        print("  # paste capsule JSON here")
        print("  print(verify(capsule).ok)")
        print("\"")


if __name__ == "__main__":
    run_demo()
