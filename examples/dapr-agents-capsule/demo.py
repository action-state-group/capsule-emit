#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Dapr Agents adapter demo — execution capsule + decide capsule side by side.

Shows two capsule types with real anchor inclusion evidence:

  Capsule 1 — fyi (execution record)
    Produced by @emitter.tool() as the agent calls a tool.
    Analogous to what the capsule-emit-dapr Go adapter produces from signed
    Dapr Workflow history — an observation of what the agent executed.

  Capsule 2 — decide (HITL decision record)
    Produced by emitter.record_hitl() at the approval gate.
    Records the REAL human decision (accept/reject, approver identity) as it
    happened — the live decision-point layer this adapter owns.

Both capsules are:
  - sealed offline (capsule_id committed)
  - submitted synchronously to the live SCITT anchor (POST /v1/digest)
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


def run_demo() -> None:
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
            workflow_instance_id="wf-demo-2026-07-28",
            ledger=ledger,
            anchor=False,
        )

        # ── Capsule 1: execution record (fyi) ────────────────────────────
        _section("Step 1 — seal fyi capsule (tool call)")

        @emitter.tool("check_invoice")
        def check_invoice(invoice_id: str, amount: str, vendor: str) -> dict:
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
        fyi = emitter.last
        fyi_cap = fyi.capsule
        fyi_id = fyi.capsule_id

        print(f"  capsule_id  : {fyi_id}")
        print(f"  action_type : {fyi_cap['action_type']}")
        print(f"  verdict     : {fyi_cap['disposition']['verdict_class']}")

        # Offline verify
        vr1 = verify(fyi_cap)
        print(f"  verify().ok : {vr1.ok}")

        # Anchor synchronously
        _section("Step 2 — anchor fyi capsule → POST /v1/digest")
        reg1 = _anchor_sync(fyi_id)
        fyi_leaf = reg1["leaf_index"]
        fyi_tree = reg1["tree_size"]
        fyi_entry_hash = reg1["entry_hash"]
        expected1 = hashlib.sha256(bytes.fromhex(fyi_id)).hexdigest()
        assert fyi_entry_hash == expected1, f"entry_hash mismatch: {fyi_entry_hash} != {expected1}"
        print("  POST /v1/digest          HTTP 200")
        print(f"  entry_hash               : {fyi_entry_hash}")
        print(f"  expected (sha256(id))    : {expected1}")
        print(f"  leaf_index               : {fyi_leaf}")
        print(f"  tree_size                : {fyi_tree}")
        print("  entry_hash matches       : True")

        # CT inclusion proof
        ip1 = _inclusion_proof(fyi_leaf, fyi_tree)
        print(f"\n  GET /anchor/inclusion-proof-ct?leaf_index={fyi_leaf}&tree_size={fyi_tree}")
        print("                           HTTP 200")
        print(f"  leaf_hash    : {ip1['leaf_hash']}")
        print(f"  audit_path   : {ip1['audit_path']}")
        print(f"  root_hash    : {ip1['root_hash']}")

        # Offline receipt verify
        receipt1 = base64.b64decode(reg1["receipt_b64"])
        vr1_receipt = verify_receipt(receipt1, leaf_entry_hex=fyi_entry_hash, log_public_key_pem=log_pem)
        print(f"\n  verify_receipt (scitt-cose offline) : ok={vr1_receipt.ok}")
        assert vr1_receipt.ok, f"receipt verify failed: {vr1_receipt.errors}"

        # ── Capsule 2: decide (HITL decision) ────────────────────────────
        _section("Step 3 — seal decide capsule (HITL approval)")

        hitl = emitter.record_hitl(
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
            prior_capsule_id=fyi_id,
        )
        decide_cap = hitl.capsule
        decide_id = hitl.capsule_id

        print(f"  capsule_id     : {decide_id}")
        print(f"  action_type    : {decide_cap['action_type']}")
        print(f"  verdict        : {decide_cap['disposition']['verdict_class']}")
        print(f"  human_disposed : {decide_cap['disposition']['human_disposed']}")
        print(f"  decision       : {decide_cap['disposition']['decision']}")
        print(f"  chained to     : {decide_cap['chain']['parent_capsule_id']}")
        assert decide_cap["chain"]["parent_capsule_id"] == fyi_id

        vr2 = verify(decide_cap)
        print(f"  verify().ok    : {vr2.ok}")

        _section("Step 4 — anchor decide capsule → POST /v1/digest")
        reg2 = _anchor_sync(decide_id)
        decide_leaf = reg2["leaf_index"]
        decide_tree = reg2["tree_size"]
        decide_entry_hash = reg2["entry_hash"]
        expected2 = hashlib.sha256(bytes.fromhex(decide_id)).hexdigest()
        assert decide_entry_hash == expected2, f"entry_hash mismatch: {decide_entry_hash} != {expected2}"
        print("  POST /v1/digest          HTTP 200")
        print(f"  entry_hash               : {decide_entry_hash}")
        print(f"  expected (sha256(id))    : {expected2}")
        print(f"  leaf_index               : {decide_leaf}")
        print(f"  tree_size                : {decide_tree}")
        print("  entry_hash matches       : True")

        ip2 = _inclusion_proof(decide_leaf, decide_tree)
        print(f"\n  GET /anchor/inclusion-proof-ct?leaf_index={decide_leaf}&tree_size={decide_tree}")
        print("                           HTTP 200")
        print(f"  leaf_hash    : {ip2['leaf_hash']}")
        print(f"  audit_path   : {ip2['audit_path']}")
        print(f"  root_hash    : {ip2['root_hash']}")

        receipt2 = base64.b64decode(reg2["receipt_b64"])
        vr2_receipt = verify_receipt(receipt2, leaf_entry_hex=decide_entry_hash, log_public_key_pem=log_pem)
        print(f"\n  verify_receipt (scitt-cose offline) : ok={vr2_receipt.ok}")
        assert vr2_receipt.ok, f"receipt verify failed: {vr2_receipt.errors}"

        # ── Summary ──────────────────────────────────────────────────────
        _section("Summary")
        print(f"  fyi    capsule_id : {fyi_id}")
        print(f"         leaf_index : {fyi_leaf}   tree_size : {fyi_tree}")
        print(f"         verify().ok: {vr1.ok}   receipt ok: {vr1_receipt.ok}")
        print()
        print(f"  decide capsule_id : {decide_id}")
        print(f"         leaf_index : {decide_leaf}   tree_size : {decide_tree}")
        print(f"         verify().ok: {vr2.ok}   receipt ok: {vr2_receipt.ok}")
        print()
        assert vr1.ok and vr2.ok and vr1_receipt.ok and vr2_receipt.ok
        print("  All checks PASS.")

        return {
            "fyi_capsule_id": fyi_id,
            "fyi_leaf_index": fyi_leaf,
            "fyi_tree_size": fyi_tree,
            "fyi_entry_hash": fyi_entry_hash,
            "fyi_audit_path": ip1["audit_path"],
            "fyi_root_hash": ip1["root_hash"],
            "decide_capsule_id": decide_id,
            "decide_leaf_index": decide_leaf,
            "decide_tree_size": decide_tree,
            "decide_entry_hash": decide_entry_hash,
            "decide_audit_path": ip2["audit_path"],
            "decide_root_hash": ip2["root_hash"],
        }


if __name__ == "__main__":
    run_demo()
