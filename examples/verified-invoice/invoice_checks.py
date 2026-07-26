# SPDX-License-Identifier: Apache-2.0
"""Illustrative invoice verification checks — three assurance tiers.

These are deterministic, model-free predicates that demonstrate the pattern.
They run on plain dicts and have no external dependencies.  Copy and adapt for
real deployments — replace the illustrative logic with your domain's rules.

Three tiers (Steven's taxonomy):
  standard  — existence + math (cheap, universal, always reproducible)
  policy    — threshold / governance rules (declares the policy version)
  formal    — pre-existing formal-proof result, recorded at highest assurance
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any


def _digest(value: Any) -> str:
    """SHA-256 over JCS-style JSON (sorted keys, compact)."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Standard tier — arithmetic + existence
# ---------------------------------------------------------------------------


def invoice_reconciles(invoice: dict) -> tuple[bool, str | None, str]:
    """Sum of line-item amounts equals invoice total.

    Returns (passed, reason, evidence_digest).  The evidence_digest commits
    to the line-items + total so an auditor knows exactly what was summed.
    """
    try:
        line_items: list[dict] = invoice.get("line_items", [])
        if not line_items:
            return False, "no line_items present", _digest(invoice)
        computed = sum(Decimal(str(item["amount"])) for item in line_items)
        declared = Decimal(str(invoice["total"]))
    except (KeyError, InvalidOperation, TypeError) as exc:
        return False, f"malformed invoice: {exc}", _digest(invoice)

    evidence = {"line_items": invoice["line_items"], "declared_total": invoice["total"]}
    digest = _digest(evidence)
    if computed == declared:
        return True, None, digest
    return False, f"line items sum to {computed}, declared total is {declared}", digest


def value_grounded(invoice: dict, source_doc: dict) -> tuple[bool, str | None, str]:
    """Quoted unit-price in the invoice matches the cited source document.

    Checks that ``invoice["unit_price"]`` equals ``source_doc["unit_price"]``.
    The evidence_digest commits to both values together.
    """
    evidence = {
        "invoice_unit_price": invoice.get("unit_price"),
        "source_unit_price": source_doc.get("unit_price"),
        "source_doc_id": source_doc.get("doc_id"),
    }
    digest = _digest(evidence)
    inv_price = invoice.get("unit_price")
    src_price = source_doc.get("unit_price")
    if inv_price is None or src_price is None:
        return False, "unit_price absent from invoice or source document", digest
    try:
        match = Decimal(str(inv_price)) == Decimal(str(src_price))
    except InvalidOperation:
        return False, "unit_price not a valid decimal", digest
    if match:
        return True, None, digest
    return False, f"invoice unit_price {inv_price!r} != source {src_price!r}", digest


# ---------------------------------------------------------------------------
# Policy tier — threshold / governance rules
# ---------------------------------------------------------------------------

POLICY_CAP = Decimal("10000")
POLICY_VERSION = "invoice-policy-v1.0"


def amount_under_policy_cap(invoice: dict) -> tuple[bool, str | None, str]:
    """Invoice total is under the $10 000 no-further-approval cap.

    The policy version is committed in the evidence_digest so the capsule
    records WHICH version of the policy applied — not just that a check ran.
    """
    evidence = {
        "declared_total": invoice.get("total"),
        "cap": str(POLICY_CAP),
        "policy_version": POLICY_VERSION,
    }
    digest = _digest(evidence)
    try:
        total = Decimal(str(invoice.get("total", 0)))
    except InvalidOperation:
        return False, "invoice total is not a valid decimal", digest
    if total < POLICY_CAP:
        return True, None, digest
    return (
        False,
        f"total {total} >= policy cap {POLICY_CAP} ({POLICY_VERSION}); "
        "requires additional approval",
        digest,
    )


# ---------------------------------------------------------------------------
# Formal tier — record a pre-existing formal-proof result
# ---------------------------------------------------------------------------
#
# We do NOT build a prover here.  The point: a formal verification tool (e.g.
# a theorem prover or symbolic arithmetic checker) runs separately and produces
# a result.  The capsule records that result at the highest assurance tier —
# so an auditor knows the arithmetic was verified by a prover, not just a
# runtime check.
#
# In production, replace the stub below with the real prover call and proof
# reference.  The interface shape is identical: (passed, reason, evidence_digest).


def formal_arithmetic_verified(invoice: dict) -> tuple[bool, str | None, str]:
    """Record that the invoice arithmetic was formally verified by a symbolic prover.

    This stub returns a pre-computed proof result — in a real deployment this
    would invoke a solver (e.g. Z3, Lean, Isabelle) and return its verdict.

    The evidence_digest commits to the proof reference + the subject invoice
    fields so the formal result is traceable.
    """
    proof_ref = "sha256:0000000000000000000000000000000000000000000000000000000000000000"  # stub
    evidence = {
        "proof_system": "symbolic_arithmetic",
        "proof_ref": proof_ref,
        "invoice_total": invoice.get("total"),
        "invoice_line_count": len(invoice.get("line_items", [])),
    }
    digest = _digest(evidence)
    return True, None, digest
