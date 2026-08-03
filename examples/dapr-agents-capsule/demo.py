#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Dapr Agents adapter demo — 3-capsule chain incl. a real HITL denial.

Shows the full chain with real anchor inclusion evidence:

  Capsule 1 — fyi (execution record)
    Produced by @emitter.tool() as the agent calls a tool.
    Analogous to what the capsule-emit-dapr Go adapter produces from signed
    Dapr Workflow history — an observation of what the agent executed.

  Capsule 2 — decide (HITL denial)
    Produced by emitter.record_hitl(decision="reject") at the approval gate.
    Records a REAL human REJECTION as it happened — verdict="blocked",
    effect.status="planned" (the action was gated; it never dispatched).
    Chained to capsule 1 via prior_capsule_id.

  Capsule 3 — fyi (escalation, chained past the denial)
    Produced by @emitter.tool(prior_capsule_id=...) after the denial — the
    agent escalates to a manager instead of retrying the blocked payment.
    Chained to capsule 2, proving the chain continues past a blocked action.

Both fyi capsules and the decide capsule are:
  - sealed offline (capsule_id committed)
  - submitted synchronously to the live SCITT anchor (POST /v1/digest)
  - confirmed via GET /v1/inclusion/<capsule_id> -> HTTP 200
  - verified offline (agent_action_capsule.verify + scitt_cose.verify_receipt)
  - inclusion-proven (GET /anchor/inclusion-proof-ct per leaf_index)

Usage:
    python3 examples/dapr-agents-capsule/demo.py

Set AAC_ANCHOR_URL to override the default anchor endpoint.
Requires: pip install capsule-emit agent-action-capsule scitt-cose
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import tempfile
import urllib.request

from agent_action_capsule import verify
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from scitt_cose import verify_receipt

from capsule_emit.adapters.dapr_agents import DaprAgentsCapsuleEmitter

ANCHOR = os.environ.get("AAC_ANCHOR_URL", "https://anchor.agentactioncapsule.org").rstrip("/")


def _anchor_sync(capsule_id: str) -> dict:
    """POST /v1/digest synchronously; return full response dict."""
    payload = json.dumps({"capsule_id": capsule_id}).encode()
    req = urllib.request.Request(
        f"{ANCHOR}/v1/digest",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _inclusion_lookup(capsule_id: str) -> dict:
    """GET /v1/inclusion/<capsule_id>; return response dict."""
    url = f"{ANCHOR}/v1/inclusion/{capsule_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def _inclusion_proof(leaf_index: int, tree_size: int) -> dict:
    """GET /anchor/inclusion-proof-ct; return proof dict."""
    url = f"{ANCHOR}/anchor/inclusion-proof-ct?leaf_index={leaf_index}&tree_size={tree_size}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def _log_pubkey_pem() -> bytes:
    """Fetch Ed25519 log public key from DID document; return PEM bytes."""
    url = f"{ANCHOR}/.well-known/did.json"
    with urllib.request.urlopen(url, timeout=10) as resp:
        doc = json.loads(resp.read())
    x_b64url = doc["verificationMethod"][0]["publicKeyJwk"]["x"]
    pad = "=" * ((4 - len(x_b64url) % 4) % 4)
    raw = base64.urlsafe_b64decode(x_b64url + pad)
    return Ed25519PublicKey.from_public_bytes(raw).public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )


def _section(title: str) -> None:
    print(f"\n─── {title} " + "─" * max(0, 66 - len(title)))


def _seal_and_anchor(label: str, capsule_id: str, capsule: dict, log_pem: bytes) -> dict:
    """Anchor one capsule synchronously and verify every layer. Returns a
    record dict suitable for the report (leaf_index, tree_size, permalink data)."""
    vr = verify(capsule)
    print(f"  [{label}] capsule_id  : {capsule_id}")
    print(f"  [{label}] action_type : {capsule['action_type']}")
    print(f"  [{label}] verdict     : {capsule['disposition']['verdict_class']}")
    print(f"  [{label}] verify().ok : {vr.ok}")
    assert vr.ok

    reg = _anchor_sync(capsule_id)
    leaf = reg["leaf_index"]
    tree = reg["tree_size"]
    entry_hash = reg["entry_hash"]
    expected = hashlib.sha256(bytes.fromhex(capsule_id)).hexdigest()
    assert entry_hash == expected
    print(f"  [{label}] POST /v1/digest        HTTP 200  leaf={leaf} tree={tree}")

    incl = _inclusion_lookup(capsule_id)
    print(f"  [{label}] GET /v1/inclusion/<id> HTTP 200  root={incl['root_hash'][:16]}...")

    proof = _inclusion_proof(leaf, tree)
    print(f"  [{label}] GET /anchor/inclusion-proof-ct HTTP 200")

    receipt = base64.b64decode(reg["receipt_b64"])
    vr_receipt = verify_receipt(receipt, leaf_entry_hex=entry_hash, log_public_key_pem=log_pem)
    print(f"  [{label}] verify_receipt (offline) : ok={vr_receipt.ok}")
    assert vr_receipt.ok

    return {
        "label": label,
        "capsule_id": capsule_id,
        "capsule": capsule,
        "leaf_index": leaf,
        "tree_size": tree,
        "entry_hash": entry_hash,
        "root_hash": incl["root_hash"],
        "audit_path": proof["audit_path"],
        "receipt_b64": reg["receipt_b64"],
        "verify_ok": vr.ok,
        "receipt_ok": vr_receipt.ok,
    }


def run_demo() -> dict:
    log_pem = _log_pubkey_pem()

    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = pathlib.Path(tmpdir) / "demo_ledger.jsonl"

        # anchor=False: we submit synchronously below so there is no race
        # between the background daemon thread and the process exiting.
        emitter = DaprAgentsCapsuleEmitter(
            operator="acme-co",
            developer="invoice-agent@v1",
            agent_name="invoice-checker",
            app_id="invoice-app",
            workflow_instance_id="wf-demo-2026-07-30",
            ledger=ledger,
            anchor=False,
        )

        records = []

        # ── Capsule 1: execution record (fyi) ────────────────────────────
        _section("Step 1 — seal fyi capsule (tool call: check_invoice)")

        @emitter.tool("check_invoice")
        def check_invoice(invoice_id: str, amount: str, vendor: str) -> dict:
            return {
                "invoice_id": invoice_id,
                "amount": amount,
                "vendor": vendor,
                "risk_score": "high",
                "flag": True,
            }

        check_invoice(
            invoice_id="INV-2026-002",
            amount="48500.00",
            vendor="Grue & Snark Freight Ltd.",
        )
        fyi1 = emitter.last
        fyi1_id = fyi1.capsule_id
        records.append(_seal_and_anchor("1 fyi/check_invoice", fyi1_id, fyi1.capsule, log_pem))

        # ── Capsule 2: decide (HITL DENIAL) ──────────────────────────────
        _section("Step 2 — seal decide capsule (HITL approval: REJECTED)")

        hitl = emitter.record_hitl(
            "approve_payment",
            approver_id="bob@acme.com",
            decision="reject",
            tool_request={
                "invoice_id": "INV-2026-002",
                "amount": "48500.00",
                "vendor": "Grue & Snark Freight Ltd.",
                "requested_by": "invoice-agent@v1",
            },
            outcome={
                "reviewed_at": "2026-07-30T14:00:00Z",
                "reason": "amount exceeds vendor's approved contract ceiling",
            },
            prior_capsule_id=fyi1_id,
        )
        decide_cap = hitl.capsule
        decide_id = hitl.capsule_id
        assert decide_cap["disposition"]["verdict_class"] == "blocked"
        assert decide_cap["effect"]["status"] == "planned"
        assert decide_cap["chain"]["parent_capsule_id"] == fyi1_id
        records.append(_seal_and_anchor("2 decide/approve_payment(REJECTED)", decide_id, decide_cap, log_pem))
        print(f"  [2] chained to  : {decide_cap['chain']['parent_capsule_id']}")

        # ── Capsule 3: fyi (escalation, chained past the denial) ─────────
        _section("Step 3 — seal fyi capsule (tool call: escalate_to_manager)")

        @emitter.tool("escalate_to_manager", prior_capsule_id=decide_id)
        def escalate_to_manager(invoice_id: str, reason: str) -> dict:
            return {
                "invoice_id": invoice_id,
                "escalated_to": "ap-manager@acme.com",
                "reason": reason,
            }

        escalate_to_manager(
            invoice_id="INV-2026-002",
            reason="payment blocked at approval gate; routing for manager review",
        )
        fyi3 = emitter.last
        fyi3_id = fyi3.capsule_id
        assert fyi3.capsule["chain"]["parent_capsule_id"] == decide_id
        records.append(_seal_and_anchor("3 fyi/escalate_to_manager", fyi3_id, fyi3.capsule, log_pem))
        print(f"  [3] chained to  : {fyi3.capsule['chain']['parent_capsule_id']}")

        # ── Summary ──────────────────────────────────────────────────────
        _section("Summary")
        for r in records:
            print(f"  {r['label']:34s} capsule_id={r['capsule_id']}  leaf={r['leaf_index']}  verify={r['verify_ok']}  receipt={r['receipt_ok']}")
        assert all(r["verify_ok"] and r["receipt_ok"] for r in records)
        print("\n  All checks PASS. Chain: fyi -> decide(BLOCKED) -> fyi (escalation).")

        dump_path = os.environ.get("DAPR_DEMO_DUMP")
        if dump_path:
            with open(dump_path, "w") as f:
                json.dump(records, f, indent=2, default=str)
            print(f"\n  Full record dump written to {dump_path}")

        return {"records": records}


if __name__ == "__main__":
    run_demo()
