#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bilateral attestation demo — Org A (requester) ↔ Org B (responder).

Demonstrates the four-move bilateral handshake from
draft-mih-agent-bilateral-attestation-00 using HMAC-SHA256 as the demo
verifier (symmetric keys — production would use Ed25519 or equivalent).

Four moves:
  1. Request attestation  — A signs a commitment to the action
  2. Constraint evaluation — B evaluates the action at the boundary
  3. Action attestation   — B signs its disposition
  4. Acknowledgment       — both parties confirm the other's attestation

After the handshake reaches BilateralState.BILATERAL, each org has emitted
a capsule.  A third party trusting neither org can run:

    agent-action-capsule verify --store /tmp/bilateral_demo.jsonl

Run:
    python3 examples/bilateral/demo.py
    python3 examples/bilateral/demo.py --no-anchor
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

from capsule_emit.bilateral import (
    BilateralHandshake,
    BilateralState,
    action_payload,
    confirm_payload,
    dict_signer,
    dict_verifier,
    request_payload,
    seal_action,
    seal_bilateral,
    seal_request,
    sig_digest,
)

_SEP = "=" * 60


def _banner(t: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {t}")
    print(_SEP)


def _ok(m: str) -> None:
    print(f"  ok  {m}")


# ---------------------------------------------------------------------------
# Demo key material (HMAC — symmetric, demonstration only)
# ---------------------------------------------------------------------------

_KEYS = {
    "org-a": b"demo-key-for-org-a-not-for-production",
    "org-b": b"demo-key-for-org-b-not-for-production",
}
_sign = dict_signer(_KEYS)
_verify_fn = dict_verifier(_KEYS)


def _action_digest(action: dict) -> str:
    raw = json.dumps(action, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# The shared action
# ---------------------------------------------------------------------------

ACTION: dict = {
    "type": "procure_equipment",
    "vendor": "Acme Corp",
    "amount_usd": 4800,
    "requester_id": "agent-a@org-a.example",
    "recipient_id": "agent-b@org-b.example",
}


def main(argv: list[str] | None = None) -> int:
    no_anchor = "--no-anchor" in (argv or sys.argv[1:])
    anchor = not no_anchor

    ledger = os.path.join(tempfile.mkdtemp(prefix="bilateral-demo-"), "ledger.jsonl")
    hs = BilateralHandshake(verify_fn=_verify_fn)

    _banner("Bilateral attestation demo — Org A ↔ Org B")
    print(f"  Ledger:  {ledger}")
    print(f"  Anchor:  {'off (--no-anchor)' if no_anchor else 'default'}")
    print("  Verifier: HMAC-SHA256 (demo only — use Ed25519 in production)")

    action_dig = _action_digest(ACTION)
    print(f"\n  Action:  {ACTION['type']} — {ACTION['vendor']} ${ACTION['amount_usd']}")
    print(f"  Digest:  {action_dig[:20]}...")

    # ── Move 1: Request attestation ─────────────────────────────────────────
    print("\n[1] Org A signs the request attestation")
    req_pay = request_payload("org-a", "org-b", action_dig)
    req_sig = _sign("org-a", req_pay)
    rec = hs.open("org-a", "org-b", action_dig, req_sig)
    hid = rec.handshake_id
    _ok(f"state: {rec.state.value}  handshake_id: {hid[:12]}...")

    cap_a = seal_request(
        "org-a", "agent-a@v1", "procure_equipment", action_dig,
        ledger=ledger, anchor=anchor,
    )
    _ok(f"capsule_a: {cap_a.capsule_id[:20]}...")

    # ── Move 2: B evaluates constraints ──────────────────────────────────────
    print("\n[2] Org B evaluates constraints at the boundary")
    print("    constraint: amount < 5000  →  PASS (4800 < 5000)")
    print("    constraint: vendor known   →  PASS ('Acme Corp' in approved list)")

    # ── Move 3: Action attestation ───────────────────────────────────────────
    print("\n[3] Org B signs the action attestation (constraints passed)")
    act_pay = action_payload(hid, "org-b", sig_digest(rec.request_sig))
    act_sig = _sign("org-b", act_pay)
    rec2 = hs.respond(hid, act_sig)
    _ok(f"state: {rec2.state.value}")

    gate_checks = [
        {"name": "amount_under_5000", "passed": True},
        {"name": "vendor_approved", "passed": True},
    ]
    cap_b = seal_action(
        "org-b", "agent-b@v1", "procure_equipment", action_dig, cap_a.capsule_id,
        verdict="executed", effect_status="confirmed",
        ledger=ledger, anchor=anchor, gate_checks=gate_checks,
    )
    _ok(f"capsule_b: {cap_b.capsule_id[:20]}...")

    # ── Move 4: Acknowledgments ───────────────────────────────────────────────
    print("\n[4] Both parties acknowledge the counterparty's attestation")

    # A acks B's action sig
    rq_pay = confirm_payload(hid, "org-a", sig_digest(rec2.action_sig))
    rq_confirm = _sign("org-a", rq_pay)
    hs.confirm(hid, "org-a", rq_confirm)

    # B acks A's request sig
    rs_pay = confirm_payload(hid, "org-b", sig_digest(rec2.request_sig))
    rs_confirm = _sign("org-b", rs_pay)
    rec_final = hs.confirm(hid, "org-b", rs_confirm)
    _ok(f"state: {rec_final.state.value}")

    assert rec_final.state is BilateralState.BILATERAL, f"expected BILATERAL, got {rec_final.state}"

    # Emit a bilateral-completion capsule (optional — records the BILATERAL state)
    seal_bilateral(
        "org-a", "agent-a@v1", "procure_equipment", action_dig, cap_a.capsule_id,
        ledger=ledger, anchor=anchor,
    )

    # ── Shared digest check ───────────────────────────────────────────────────
    print("\n[5] Shared action_digest confirms both parties attested over the same action")
    dig_a = cap_a.capsule["model_attestation"]["compute_attestation"]["action_digest"]
    dig_b = cap_b.capsule["model_attestation"]["compute_attestation"]["action_digest"]
    assert dig_a == dig_b == action_dig
    _ok(f"action_digest matches: {action_dig[:20]}...")

    # ── Verify ────────────────────────────────────────────────────────────────
    print("\n[6] Class-1 verify — both capsules")
    from agent_action_capsule import verify

    from capsule_emit.ledger import read_ledger

    all_ok = True
    for r in read_ledger(ledger):
        vr = verify(r)
        org = r.get("operator", "?")
        role = (r.get("model_attestation") or {}).get("compute_attestation", {}).get("role", "—")
        status = "ok" if vr.ok else "FAIL"
        print(f"    [{org}/{role}] {r.get('capsule_id', '')[:20]}...  {status}")
        if not vr.ok:
            all_ok = False

    if not all_ok:
        print("  !! Verification FAILED")
        return 1

    _banner("Summary")
    print(f"  Action:        {ACTION['type']} — {ACTION['vendor']} ${ACTION['amount_usd']}")
    print(f"  Org A capsule: {cap_a.capsule_id[:20]}...")
    print(f"  Org B capsule: {cap_b.capsule_id[:20]}...")
    print(f"  Shared digest: {action_dig[:20]}...")
    print(f"  Final state:   {rec_final.state.value}")
    print()
    print("  Third-party verify:")
    print(f"    agent-action-capsule verify --store {ledger}")
    print(f"\n  Demo complete.\n{_SEP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
