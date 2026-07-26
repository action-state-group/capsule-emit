#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verified invoice: run checks → emit capsule → anchor → verify.

Run:
    cd examples/verified-invoice
    python run_example.py

What this shows
---------------
1. A minimal invoice action runs three deterministic checks.
2. The capsule records WHAT WAS VERIFIED — per-check results in
   ``constraints[]``, each carrying: check id, pass/fail, tier
   (check_type), the evidence digest the check ran on, and whether
   it was blocking.
3. The capsule records WHICH manifest (declared ruleset) applied —
   ``extra_compute.manifest_ref`` commits the manifest file by SHA-256
   so an auditor can retrieve and re-read the exact policy version.
4. The overall verdict (executed / blocked) is in
   ``disposition.verdict_class``.
5. The capsule is anchored to the public transparency log and then
   verified offline — no payload crosses the wire, only a digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path plumbing: resolve the repo root from any working directory.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.parent.parent

sys.path.insert(0, str(_REPO_ROOT))

from agent_action_capsule.contracts import ConstraintRecord  # type: ignore[import-untyped]
from agent_action_capsule.emit import emit as _base_emit  # type: ignore[import-untyped]
from agent_action_capsule.verify import verify  # type: ignore[import-untyped]
from capsule_emit.ledger import append_to_ledger

try:
    from agent_action_capsule.anchor import anchor as _anchor  # type: ignore[import-untyped]
    _HAS_ANCHOR = True
except ImportError:
    _HAS_ANCHOR = False

from invoice_checks import (
    amount_under_policy_cap,
    formal_arithmetic_verified,
    invoice_reconciles,
    value_grounded,
)

LEDGER = _HERE / "invoice_ledger.jsonl"
MANIFEST = _HERE / "manifest.md"

# ---------------------------------------------------------------------------
# Sample data — illustrative only
# ---------------------------------------------------------------------------

INVOICE = {
    "invoice_id": "INV-2026-0042",
    "vendor": "Acme Office Supplies",
    "line_items": [
        {"description": "Ergonomic chair", "qty": 2, "unit_price": "349.99", "amount": "699.98"},
        {"description": "Standing desk",   "qty": 1, "unit_price": "529.00", "amount": "529.00"},
        {"description": "Monitor arm",     "qty": 3, "unit_price": "89.99",  "amount": "269.97"},
    ],
    "unit_price": "349.99",
    "total": "1498.95",
    "currency": "USD",
    "due_date": "2026-08-15",
}

SOURCE_DOC = {
    "doc_id": "QUOTE-2026-0018",
    "vendor": "Acme Office Supplies",
    "unit_price": "349.99",
}


def _file_digest(path: Path) -> str:
    """SHA-256 of a file's contents."""
    h = hashlib.sha256(path.read_bytes())
    return h.hexdigest()


def _pretty(label: str, value: object) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print('='*60)
    if isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, default=str))
    else:
        print(value)


def run() -> None:
    print("\nVerified Invoice Example")
    print("=" * 60)
    print("Invoice:", INVOICE["invoice_id"], " Total:", INVOICE["total"])

    # ------------------------------------------------------------------
    # Step 1 — run deterministic checks (all three assurance tiers)
    # ------------------------------------------------------------------
    print("\n[1] Running checks...")

    passed_std1, reason_std1, digest_std1 = invoice_reconciles(INVOICE)
    passed_std2, reason_std2, digest_std2 = value_grounded(INVOICE, SOURCE_DOC)
    passed_pol,  reason_pol,  digest_pol  = amount_under_policy_cap(INVOICE)
    passed_frm,  reason_frm,  digest_frm  = formal_arithmetic_verified(INVOICE)

    check_results = [
        ("invoice_reconciles",       "standard", True,  passed_std1, reason_std1, digest_std1),
        ("value_grounded",           "standard", True,  passed_std2, reason_std2, digest_std2),
        ("amount_under_policy_cap",  "policy",   True,  passed_pol,  reason_pol,  digest_pol),
        ("formal_arithmetic_verified","formal",  False, passed_frm,  reason_frm,  digest_frm),
    ]

    gate_passed = all(ok for _, _, blocking, ok, _, _ in check_results if blocking)

    for cid, tier, blocking, ok, reason, _ in check_results:
        status = "PASS" if ok else "FAIL"
        block_flag = "(blocking)" if blocking else "(warn)"
        line = f"  [{tier:8s}] {cid:30s} {status} {block_flag}"
        if not ok and reason:
            line += f"\n           reason: {reason}"
        print(line)

    print(f"\nGate: {'PASS — capsule will record verdict=executed' if gate_passed else 'BLOCK — capsule will record verdict=blocked'}")

    # ------------------------------------------------------------------
    # Step 2 — build ConstraintRecord entries (§8.1)
    # ------------------------------------------------------------------
    constraints: list[ConstraintRecord] = []
    for cid, tier, blocking, ok, reason, ev_digest in check_results:
        constraints.append(ConstraintRecord(
            id=cid,
            result="pass" if ok else "fail",
            check_type=tier,
            blocking=blocking,
            method={
                "invoice_reconciles":        "arithmetic_sum",
                "value_grounded":            "exact_match",
                "amount_under_policy_cap":   "threshold",
                "formal_arithmetic_verified":"symbolic_proof",
            }[cid],
            evidence_digest=ev_digest,
        ))

    # ------------------------------------------------------------------
    # Step 3 — pin the manifest (declared ruleset) by digest
    # ------------------------------------------------------------------
    manifest_digest = _file_digest(MANIFEST) if MANIFEST.exists() else "0" * 64
    manifest_ref = f"sha256:{manifest_digest}"

    # ------------------------------------------------------------------
    # Step 4 — emit the capsule (base AAC emit, composing capsule-emit ledger)
    # ------------------------------------------------------------------
    print("\n[2] Emitting capsule...")
    from agent_action_capsule.contracts import Disposition, EffectRecord

    verdict = "executed" if gate_passed else "blocked"
    effect_status = "dispatched" if gate_passed else "planned"

    capsule = _base_emit(
        action_id=None,
        action_type="decide",
        operator="example-org",
        developer="invoice-agent@v1",
        tool_name="pay_invoice",
        compute_attestation={
            "manifest_ref": manifest_ref,
            "runtime": "capsule-emit-example",
        },
        effect=EffectRecord(
            type="pay_invoice",
            status=effect_status,
        ),
        disposition=Disposition(
            decision="accept" if gate_passed else "reject",
            approver="policy",
            verdict_class=verdict,
        ),
        constraints=tuple(constraints),
    )

    append_to_ledger(capsule, LEDGER)
    capsule_id = capsule["capsule_id"]
    print(f"  capsule_id: {capsule_id}")

    # ------------------------------------------------------------------
    # Step 5 — anchor (digest-only; no invoice content crosses the wire)
    # ------------------------------------------------------------------
    anchored = False
    if _HAS_ANCHOR:
        anchor_url = os.environ.get("AAC_ANCHOR_URL")
        try:
            print("\n[3] Anchoring (digest only, no payload sent)...")
            _anchor(capsule_id, **({"endpoint": anchor_url} if anchor_url else {}))
            anchored = True
            print(f"  anchored: {anchored}")
        except Exception as exc:
            print(f"  anchor skipped (no network or anchor down): {exc}")
    else:
        print("\n[3] Anchor module not available — skipping")

    # ------------------------------------------------------------------
    # Step 6 — verify the capsule offline
    # ------------------------------------------------------------------
    print("\n[4] Verifying capsule integrity offline...")
    report = verify(capsule)
    _pretty("Verify report", report)

    # ------------------------------------------------------------------
    # Step 7 — show what the capsule records about verification
    # ------------------------------------------------------------------
    print("\n[5] What the capsule records about WHAT WAS VERIFIED:")
    print(f"  verdict:      {capsule['disposition']['verdict_class']}")
    print(f"  manifest_ref: {capsule['model_attestation']['compute_attestation']['manifest_ref']}")
    print(f"  anchored:     {anchored}")
    print(f"\n  Per-check results (constraints[]):")
    for cr in capsule.get("constraints", []):
        tier = cr.get("check_type", "?")
        result = cr.get("result", "?")
        print(f"    [{tier:8s}] {cr['id']:30s} {result}  evidence={cr.get('evidence_digest','')[:12]}...")

    print("\n  Full capsule JSON:")
    print(json.dumps(capsule, indent=2))

    # ------------------------------------------------------------------
    # Assertions — make the acceptance test self-contained
    # ------------------------------------------------------------------
    assert capsule["disposition"]["verdict_class"] in ("executed", "blocked")
    assert "constraints" in capsule and len(capsule["constraints"]) == 4
    tiers = {cr["check_type"] for cr in capsule["constraints"]}
    assert tiers == {"standard", "policy", "formal"}, f"expected all three tiers, got {tiers}"
    assert all(cr.get("evidence_digest") for cr in capsule["constraints"])
    assert capsule["model_attestation"]["compute_attestation"]["manifest_ref"].startswith("sha256:")
    assert report.ok, f"capsule did not verify: {report}"

    print("\n[OK] All acceptance assertions passed.")
    print(f"     Ledger: {LEDGER}")


if __name__ == "__main__":
    run()
