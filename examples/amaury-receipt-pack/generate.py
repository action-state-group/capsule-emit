#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Amaury sample receipt pack — generate 4 representative capsules.

Produces a local JSONL ledger with four capsules covering the primary
Agent Action Capsule (draft-mih-scitt-agent-action-capsule-02) patterns:

  1. Standard executed capsule
  2. Blocked / refusal capsule
  3. Gate-checked capsule (two constraints passing, one failing)
  4. Chain-linked capsule (confirms → capsule #1)

All capsules are emitted with anchor=False; no network calls are made.
Run this script, then verify with:

    capsule-emit verify --store examples/amaury-receipt-pack/sample_ledger.jsonl

or equivalently:

    python3 -c "
    from agent_action_capsule import verify_store
    results = verify_store('examples/amaury-receipt-pack/sample_ledger.jsonl')
    for r in results: print(r.capsule_id[:16], 'ok' if r.ok else 'FAIL', r.findings)
    "
"""
from __future__ import annotations

from pathlib import Path

from agent_action_capsule import emit as base_emit
from agent_action_capsule.contracts import (
    ConstraintRecord,
    Disposition,
    EffectRecord,
)

from capsule_emit import emit
from capsule_emit.ledger import append_to_ledger

# ---------------------------------------------------------------------------
# Resolve paths — works both from repo root and from this file's directory.
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent.resolve()
LEDGER = HERE / "sample_ledger.jsonl"

# ---------------------------------------------------------------------------
# Reset ledger on each run
# ---------------------------------------------------------------------------
if LEDGER.exists():
    LEDGER.unlink()


# ---------------------------------------------------------------------------
# 1. Standard executed capsule — approve_purchase
# ---------------------------------------------------------------------------
print("Emitting capsule 1: approve_purchase (executed) …")
cap1 = emit(
    action="approve_purchase",
    operator="acme-research",
    developer="procurement-agent@v1",
    model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    agent_input={
        "vendor": "BioSupplies GmbH",
        "item": "reagent-kit-A90",
        "quantity": 200,
        "unit_price_eur": 47.50,
    },
    agent_output={
        "po_number": "PO-2026-0701",
        "status": "dispatched",
        "vendor_ref": "BSG-INV-3841",
    },
    verdict="executed",
    effect={"type": "purchase_order", "status": "dispatched"},
    anchor=False,
    ledger=LEDGER,
)
print(f"  capsule_id: {cap1.capsule_id}")
print(f"  anchored:   {cap1.anchored}  (anchor=False — no network call)\n")


# ---------------------------------------------------------------------------
# 2. Blocked / refusal capsule — transfer_funds
# ---------------------------------------------------------------------------
print("Emitting capsule 2: transfer_funds (blocked) …")
# Use base_emit directly so we can include the failing ConstraintRecord.
_c2_constraint = ConstraintRecord(
    id="transfer_limit_eur_check",
    result="fail",
    severity="blocking",
    check_type="policy",
    method="rule_engine",
)
_c2_disp = Disposition(
    decision="reject",
    approver="policy",
    human_disposed=False,
    verdict_class="blocked",
)
_c2_effect = EffectRecord(
    type="fund_transfer",
    status="planned",
)
_raw_c2 = base_emit(
    tool_name="transfer_funds",
    action_type="decide",
    operator="acme-research",
    developer="procurement-agent@v1",
    model_id="claude-sonnet-4-6",
    provider="anthropic",
    compute_attestation={
        "agent_input_digest": "a" * 64,
        "note": "digest of {amount_eur: 150000, target_iban: DE89370400440532013000}",
    },
    effect=_c2_effect,
    disposition=_c2_disp,
    constraints=(_c2_constraint,),
)
append_to_ledger(_raw_c2, LEDGER)
cap2_id = _raw_c2["capsule_id"]
print(f"  capsule_id: {cap2_id}")
print("  verdict:    blocked (transfer_limit_eur_check: fail)\n")


# ---------------------------------------------------------------------------
# 3. Gate-checked capsule — generate_report with two passing constraints
# ---------------------------------------------------------------------------
print("Emitting capsule 3: generate_report (executed, gate_checks) …")
_c3_constraints = (
    ConstraintRecord(
        id="value_grounded",
        result="pass",
        severity="blocking",
        check_type="semantic",
        method="nli_entailment",
    ),
    ConstraintRecord(
        id="invoice_reconciles",
        result="pass",
        severity="blocking",
        check_type="accounting",
        method="ledger_sum_match",
    ),
)
_c3_disp = Disposition(
    decision="accept",
    approver="policy",
    human_disposed=False,
    verdict_class="executed",
)
_c3_effect = EffectRecord(
    type="report_artifact",
    status="dispatched",
)
_raw_c3 = base_emit(
    tool_name="generate_report",
    action_type="decide",
    operator="acme-research",
    developer="procurement-agent@v1",
    model_id="claude-sonnet-4-6",
    provider="anthropic",
    compute_attestation={
        "agent_input_digest": "b" * 64,
        "agent_output_digest": "c" * 64,
        "runtime": "langchain",
    },
    effect=_c3_effect,
    disposition=_c3_disp,
    constraints=_c3_constraints,
)
append_to_ledger(_raw_c3, LEDGER)
cap3_id = _raw_c3["capsule_id"]
print(f"  capsule_id: {cap3_id}")
print("  verdict:    executed (value_grounded: pass, invoice_reconciles: pass)\n")


# ---------------------------------------------------------------------------
# 4. Chain-linked capsule — confirm_purchase chains → capsule #1
# ---------------------------------------------------------------------------
print("Emitting capsule 4: confirm_purchase (confirmed, chained → capsule 1) …")
cap4 = emit(
    action="confirm_purchase",
    operator="acme-research",
    developer="procurement-agent@v1",
    model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    agent_output={
        "po_number": "PO-2026-0701",
        "vendor_confirmation": "BSG-ACK-7712",
        "delivery_eta": "2026-07-14",
    },
    verdict="confirmed",
    effect={"type": "purchase_order", "status": "confirmed"},
    confirms=cap1.capsule_id,
    relation="confirms",
    anchor=False,
    ledger=LEDGER,
)
print(f"  capsule_id:  {cap4.capsule_id}")
print(f"  chained to:  {cap1.capsule_id}")
print(f"  anchored:    {cap4.anchored}  (anchor=False — no network call)\n")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
print("=" * 78)
print(f"{'Capsule ID (first 16 chars)':<29}  {'Type':<14}  {'Verdict':<10}  {'Chained To'}")
print("-" * 78)
print(f"{cap1.capsule_id[:16]:<29}  {'executed':<14}  {'executed':<10}  —")
print(f"{cap2_id[:16]:<29}  {'blocked':<14}  {'blocked':<10}  —")
print(f"{cap3_id[:16]:<29}  {'gate_checks':<14}  {'executed':<10}  —")
print(f"{cap4.capsule_id[:16]:<29}  {'chained':<14}  {'confirmed':<10}  {cap1.capsule_id[:16]}…")
print("=" * 78)

print("\nFull capsule IDs:")
print(f"  [1] approve_purchase  : {cap1.capsule_id}")
print(f"  [2] transfer_funds    : {cap2_id}")
print(f"  [3] generate_report   : {cap3_id}")
print(f"  [4] confirm_purchase  : {cap4.capsule_id}")

print(f"\nLedger written to: {LEDGER}")
print("\nVerify with:")
print(f"  capsule-emit verify --store {LEDGER}")
