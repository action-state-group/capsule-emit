#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A2A callee-seals capsule example — AP2 payment headline.

Demonstrates the compose pattern:
  AP2 CartMandate (the "may")  →  capsule agent_input_digest
  Payment outcome  (the "did") →  capsule response_digest + effect.type: send_payment

Scenarios:
  A  Mandate within limit → payment approved → dispatched capsule → confirmed capsule
  B  Mandate received, amount over limit → refusal capsule (payment NEVER executed)

Run (sandbox, no keys needed):
    pip install "capsule-emit"
    python examples/a2a-ap2/run_example.py

Run (real Stripe):
    STRIPE_API_KEY=sk_test_... DRY_RUN=0 python examples/a2a-ap2/run_example.py

After running, verify the ledger:
    agent-action-capsule verify --store /tmp/a2a_ap2_ledger.jsonl
"""
from __future__ import annotations

import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from a2a_sandbox import (  # noqa: E402
    A2ATask,
    AP2CartMandate,
    Money,
    execute_payment,
    is_sandbox,
)

from capsule_emit import emit  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPERATOR = "action-state-group"
DEVELOPER = "ap2-payment-agent@v1"
LEDGER_PATH = Path(tempfile.mkdtemp()) / "a2a_ap2_ledger.jsonl"
SPEND_LIMIT = Decimal("1000.00")   # per-mandate ceiling enforced by this agent

_SEP = "─" * 70


def _banner(title: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


def _ok(msg: str) -> None:
    print(f"  ✓  {msg}")


def _info(msg: str) -> None:
    print(f"     {msg}")


# ---------------------------------------------------------------------------
# Scenario A — mandate within limit → approve → dispatch → confirm
# ---------------------------------------------------------------------------

def scenario_a() -> tuple[str, str]:
    """Returns (dispatched_capsule_id, confirmed_capsule_id)."""
    _banner("Scenario A: AP2 mandate → payment approved → capsule (dispatch + confirm)")

    task = A2ATask(
        task_id="task-a2a-0001",
        session_id="sess-demo-001",
        mandate=AP2CartMandate(
            mandate_id="mandate-001",
            payee_name="Acme Office Supplies",
            payee_id="vendor-42",
            max_amount=Money("4250.00", "USD"),
            cart_ref="INV-2026-0418",
            authorized_by="procurement-policy@v2",
            expires_at="2026-06-30T23:59:59Z",
        ),
    )
    pay_amount = Money("4250.00", "USD")

    _info(f"A2A Task: {task.task_id}")
    _info(f"AP2 CartMandate: {task.mandate.mandate_id} — pay {pay_amount.value} {pay_amount.currency}")
    _info(f"  payee: {task.mandate.payee_name} ({task.mandate.payee_id})")
    _info(f"  cart:  {task.mandate.cart_ref}")
    _info(f"  limit check: {pay_amount.value} ≤ {task.mandate.max_amount.value}  → APPROVED")

    # Step 1: Dispatch capsule — payment initiated
    dispatched = emit(
        action="send_payment",
        operator=OPERATOR,
        developer=DEVELOPER,
        agent_input=task.input_dict(),           # AP2 CartMandate (the "may")
        agent_output=None,                       # output not yet known at dispatch
        model=None,
        verdict="executed",
        effect={"type": "send_payment", "status": "dispatched"},
        anchor=True,
        ledger=LEDGER_PATH,
        salt_digests=False,                      # deterministic for demo reproducibility
    )
    _ok(f"dispatched capsule_id: {dispatched.capsule_id}")

    # Execute the actual payment (sandbox by default)
    result = execute_payment(task.mandate, pay_amount)
    _info(f"payment_id: {result.payment_id}  status: {result.status}  mode: {'sandbox' if is_sandbox() else 'real Stripe'}")

    # Step 2: Confirm capsule — payment completed, chain onto dispatched
    confirmed = emit(
        action="send_payment",
        operator=OPERATOR,
        developer=DEVELOPER,
        agent_input=task.input_dict(),           # same mandate = same input digest
        agent_output=result.as_dict(),           # payment outcome (the "did")
        model=None,
        verdict="executed",
        effect={"type": "send_payment", "status": "confirmed"},
        confirms=dispatched.capsule_id,
        anchor=True,
        ledger=LEDGER_PATH,
        salt_digests=False,
    )
    _ok(f"confirmed capsule_id:  {confirmed.capsule_id}")
    _info(f"  chains → dispatched: {confirmed.capsule['chain']['parent_capsule_id'][:16]}…")
    _info(f"  response_digest:    {confirmed.capsule['effect']['response_digest'][:16]}…")

    # Wait for both receipts
    r1 = dispatched.wait_receipt(timeout=12)
    r2 = confirmed.wait_receipt(timeout=12)
    _ok(f"SCITT anchored:  dispatch leaf={r1.get('leaf_index', '?') if r1 else 'timeout'}, confirm leaf={r2.get('leaf_index', '?') if r2 else 'timeout'}")

    return dispatched.capsule_id, confirmed.capsule_id


# ---------------------------------------------------------------------------
# Scenario B — mandate over agent's per-transaction limit → refusal capsule
# ---------------------------------------------------------------------------

def scenario_b() -> str:
    """Returns refusal_capsule_id."""
    _banner("Scenario B: AP2 mandate over limit → refusal capsule (payment NEVER executed)")

    task = A2ATask(
        task_id="task-a2a-0002",
        session_id="sess-demo-001",
        mandate=AP2CartMandate(
            mandate_id="mandate-002",
            payee_name="Premium Consulting LLC",
            payee_id="vendor-99",
            max_amount=Money("50000.00", "USD"),
            cart_ref="INV-2026-CONSULTING",
            authorized_by="procurement-policy@v2",
            expires_at="2026-06-30T23:59:59Z",
        ),
    )
    requested_amount = Money("50000.00", "USD")
    attempted = Decimal(requested_amount.value)

    _info(f"A2A Task: {task.task_id}")
    _info(f"AP2 CartMandate: {task.mandate.mandate_id} — pay {requested_amount.value} {requested_amount.currency}")
    _info(f"  limit check: {attempted} > {SPEND_LIMIT}  → REFUSED (over agent spend limit)")

    # Seal a refusal — effect.status="planned" (payment was NEVER dispatched)
    refusal = emit(
        action="send_payment",
        operator=OPERATOR,
        developer=DEVELOPER,
        agent_input=task.input_dict(),   # the mandate we evaluated
        agent_output={
            "refusal_reason": "over_agent_spend_limit",
            "requested": requested_amount.as_dict(),
            "agent_limit": str(SPEND_LIMIT),
        },
        model=None,
        verdict="blocked",
        decision="reject",
        effect={"type": "send_payment", "status": "planned"},
        anchor=True,
        ledger=LEDGER_PATH,
        salt_digests=False,
    )
    _ok(f"refusal capsule_id: {refusal.capsule_id}")
    _info(f"  verdict_class:  {refusal.capsule['disposition']['verdict_class']}")
    _info(f"  effect.status:  {refusal.capsule['effect']['status']}  ← 'planned' = NEVER dispatched")

    r = refusal.wait_receipt(timeout=12)
    _ok(f"SCITT anchored: leaf={r.get('leaf_index', '?') if r else 'timeout'}")

    return refusal.capsule_id


# ---------------------------------------------------------------------------
# Verification pass
# ---------------------------------------------------------------------------

def verify_ledger() -> None:
    _banner("Verification — agent-action-capsule verify (Class-1)")
    from agent_action_capsule import verify_store

    with open(LEDGER_PATH) as f:
        capsules = [json.loads(line) for line in f if line.strip()]

    results = verify_store(capsules)
    all_ok = True
    for i, (cap, result) in enumerate(zip(capsules, results)):
        verdict = cap.get("disposition", {}).get("verdict_class", "?")
        status = cap.get("effect", {}).get("status", "?")
        ok_str = "VALID  ✓" if result.ok else "INVALID ✗"
        cid = cap.get("capsule_id", "")[:20]
        print(f"  [{i}] {ok_str}  {cid}…  verdict={verdict} effect.status={status}")
        if not result.ok:
            all_ok = False
            for finding in result.findings:
                print(f"       [{finding.severity}] {finding.code}: {finding.detail}")
    if all_ok:
        print("\n  All capsules VALID ✓")
    else:
        print("\n  VERIFICATION FAILURES — see above")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=== A2A callee-seals capsule example — AP2 payment headline ===")
    print(f"Ledger: {LEDGER_PATH}")
    print(f"Mode:   {'sandbox (DRY_RUN)' if is_sandbox() else 'real Stripe'}")

    dispatch_id, confirm_id = scenario_a()
    refusal_id = scenario_b()
    verify_ledger()

    print(f"\n{_SEP}")
    print("  Summary")
    print(_SEP)
    print("  Scenario A — approved payment")
    print(f"    dispatch capsule_id : {dispatch_id}")
    print(f"    confirm  capsule_id : {confirm_id}")
    print("  Scenario B — refusal")
    print(f"    refusal  capsule_id : {refusal_id}")
    print("\n  Compose framing:")
    print("    AP2 CartMandate  → agent_input_digest  (the 'may')")
    print("    Payment outcome  → response_digest     (the 'did')")
    print("    SCITT receipt    → independent proof that the capsule was registered")
    print("\n  Verify yourself:")
    print(f"    agent-action-capsule verify --store {LEDGER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
