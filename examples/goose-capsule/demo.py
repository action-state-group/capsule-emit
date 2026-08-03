#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Goose capsule demo — 3-capsule chain incl. a real HITL denial, live anchor.

Simulates exactly what Goose does when it calls tools in the po-agent MCP
server: each wrapped function is invoked, capsule-emit seals INPUT + OUTPUT
digests into a ledger row, and agent-action-capsule verifies the capsule
offline.

Shows the full chain with real anchor inclusion evidence:

  Capsule 1 — write_order (execution record)
    Produced by @emitter.tool() as Goose calls submit_order.

  Capsule 2 — decide (HITL denial)
    A larger order requires manager approval; the human REJECTS it.
    Records the REAL rejection: verdict="blocked", effect.status="planned"
    (the action was gated; it never dispatched). Chained to capsule 1.

  Capsule 3 — fyi (escalation, chained past the denial)
    The agent escalates to a manager instead of retrying the blocked
    order. Chained to capsule 2, proving the chain continues past a
    blocked action.

All three are:
  - sealed offline (capsule_id committed)
  - submitted synchronously to the live SCITT anchor (POST /v1/digest)
  - confirmed via GET /v1/inclusion/<capsule_id> -> HTTP 200
  - verified offline (agent_action_capsule.verify + scitt_cose.verify_receipt)
  - inclusion-proven (GET /anchor/inclusion-proof-ct per leaf_index)

"Any Goose tool call → verifiable record, in one decorator."

Run:
    pip install "capsule-emit[dev]"
    python examples/goose-capsule/demo.py             # default: live anchor
    python examples/goose-capsule/demo.py --no-anchor # offline mode only

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

Set AAC_ANCHOR_URL / AAC_VERIFY_URL to override the default endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from agent_action_capsule import verify
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from scitt_cose import verify_receipt

from capsule_emit import read_ledger
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

_ANCHOR = "--no-anchor" not in sys.argv

ANCHOR = os.environ.get("AAC_ANCHOR_URL", "https://anchor.agentactioncapsule.org").rstrip("/")
VERIFY = os.environ.get("AAC_VERIFY_URL", "https://verify.agentactioncapsule.org").rstrip("/")


# ── Live anchor helpers (synchronous — mirrors examples/dapr-agents-capsule) ──


def _anchor_sync(capsule_id: str) -> dict:
    """POST /v1/digest synchronously; return the full response dict."""
    payload = json.dumps({"capsule_id": capsule_id}).encode()
    req = urllib.request.Request(
        f"{ANCHOR}/v1/digest",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _inclusion_lookup(capsule_id: str) -> dict:
    """GET /v1/inclusion/<capsule_id>; return the response dict."""
    url = f"{ANCHOR}/v1/inclusion/{capsule_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def _inclusion_proof(leaf_index: int, tree_size: int) -> dict:
    """GET /anchor/inclusion-proof-ct; return the proof dict."""
    url = f"{ANCHOR}/anchor/inclusion-proof-ct?leaf_index={leaf_index}&tree_size={tree_size}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def _log_pubkey_pem() -> bytes:
    """Fetch the Ed25519 log public key from the DID document; return PEM bytes."""
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
    """Anchor one capsule synchronously and verify every layer.

    Returns a record dict with leaf_index/tree_size/audit_path for the
    transcript and permalink construction.
    """
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


def _permalink(capsule_id: str, capsule: dict) -> str:
    """Single-capsule verify permalink — capsule JSON lives in the URL fragment."""
    frag = base64.b64encode(json.dumps(capsule).encode()).decode()
    return f"{VERIFY}/v/{capsule_id}#{frag}"


def _bundle_permalink(records: list[dict]) -> str:
    """Array-fragment bundle permalink — renders the Chain Navigation table."""
    capsules = [r["capsule"] for r in records]
    frag = base64.b64encode(json.dumps(capsules).encode()).decode()
    return f"{VERIFY}/v/{records[0]['capsule_id']}#{frag}"


# ── 1. Build the emitter (same config used in server.py) ──────────────────

with tempfile.TemporaryDirectory() as _tmp:
    ledger = Path(_tmp) / "goose-capsules.jsonl"

    # anchor=False on the emitter: we submit synchronously below (when live)
    # so there is no race between a background fire-and-forget thread and
    # the process exiting before it lands.
    emitter = MCPCapsuleEmitter(
        operator="acme-co",
        developer="goose-agent@v1",
        ledger=ledger,
        anchor=False,
        model={"provider": "anthropic", "model_id": "claude-opus-4-8"},
    )

    # ── 2. Wrap tools — decorator order mirrors server.py ─────────────────
    #    @server.tool() would be the outermost; @emitter.tool() is the inner.
    #    In this standalone demo we skip @server.tool() (no MCP server needed).

    @emitter.tool(effect_type="write_order")
    def submit_order(vendor: str, amount: str, po_number: str) -> dict:
        """Submit a purchase order. amount must be an exact decimal string (e.g. '1240.19')."""
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
        # §5.1 requires exact decimal strings for monetary values in digest fields
        prices = {"widget": "42.00", "gadget": "128.50", "doohickey": "9.99"}
        unit_price = prices.get(item.lower(), "0.00")
        return {"vendor": vendor, "item": item, "unit_price_usd": unit_price, "currency": "USD"}

    # ── 3. Simulate Goose tool calls ──────────────────────────────────────

    print("=" * 60)
    print("Goose capsule demo — tool call → sealed capsule → verify")
    print("=" * 60)

    print("\n[step 1] Goose calls get_price (read-only, action_type=fyi)")
    price_result = get_price(vendor="Frobozz Supply", item="widget")
    print(f"  tool returned: {price_result}")

    print("\n[step 2] Goose calls submit_order (consequential, write_order)")
    order_result = submit_order(vendor="Frobozz Supply", amount="1240.19", po_number="PO-7777")
    print(f"  tool returned: {order_result}")
    order1 = emitter.last
    order1_id = order1.capsule_id

    # ── Step 3: decide (HITL DENIAL) — a genuine refusal in the chain ─────
    _section("Step 3 — human approval gate: large order REJECTED")

    approval_request = {
        "po_number": "PO-7778",
        "vendor": "Globex Corp",
        "amount_usd": "125000.00",
        "requested_by": "goose-agent@v1",
    }
    approval_outcome = {
        "reviewed_at": "2026-08-03T00:00:00Z",
        "reason": "order value exceeds vendor's approved PO ceiling",
    }
    decide1 = emitter.emit_capsule(
        "approve_large_order",
        tool_input=approval_request,
        tool_output=approval_outcome,
        verdict="blocked",
        effect={"type": "approve_large_order", "status": "planned"},
        action_type="decide",
        human_disposed=True,
        approver="human",
        decision="reject",
        runtime="mcp",
        prior_capsule_id=order1_id,
        extra_compute={"approver_id": "priya@acme-co.com"},
    )
    decide1_id = decide1.capsule_id
    assert decide1.capsule["disposition"]["verdict_class"] == "blocked"
    assert decide1.capsule["effect"]["status"] == "planned"
    assert decide1.capsule["chain"]["parent_capsule_id"] == order1_id
    print(f"  capsule_id  : {decide1_id}")
    print(f"  verdict     : {decide1.capsule['disposition']['verdict_class']}")
    print("  approver    : human (priya@acme-co.com)")
    print(f"  reason      : {approval_outcome['reason']}")
    print(f"  chained to  : {order1_id}")

    # ── Step 4: fyi (escalation, chained past the denial) ─────────────────
    _section("Step 4 — escalate blocked order to manager")

    escalation_input = {
        "po_number": "PO-7778",
        "reason": "order blocked at approval gate; routing for manager review",
    }
    escalation_output = {
        "po_number": "PO-7778",
        "escalated_to": "ap-manager@acme-co.com",
    }
    escalate1 = emitter.emit_capsule(
        "escalate_to_manager",
        tool_input=escalation_input,
        tool_output=escalation_output,
        verdict="executed",
        effect={"type": "escalate_to_manager", "status": "dispatched"},
        action_type="fyi",
        runtime="mcp",
        prior_capsule_id=decide1_id,
    )
    escalate1_id = escalate1.capsule_id
    assert escalate1.capsule["chain"]["parent_capsule_id"] == decide1_id
    print(f"  capsule_id  : {escalate1_id}")
    print(f"  chained to  : {decide1_id}")

    # ── 4. Inspect the ledger ─────────────────────────────────────────────

    records = read_ledger(ledger)
    print(f"\n[step 5] Ledger: {len(records)} capsule(s) sealed")
    for r in records:
        cid = r.get("capsule_id", "?")[:16]
        action = r.get("action_id", "?").split("/")[0]
        verdict_cls = r.get("disposition", {}).get("verdict_class", "?")
        ca = r.get("model_attestation", {}).get("compute_attestation", {})
        runtime = ca.get("runtime", "?")
        print(f"  {cid}… {action} [{verdict_cls}] runtime={runtime}")

    # ── 5. Verify — should all be ok=True ────────────────────────────────

    print("\n[step 6] Verify all capsules (offline — no network needed)")
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

    print("\n[step 7] Tamper test: flip one byte in output digest → verify fails")
    raw = records[1]  # the submit_order capsule
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

    # ── 7. Live anchor + permalinks (the persuasive part) ──────────────────

    chain_records: list[dict] = []
    if _ANCHOR:
        _section("Step 8 — live anchor the 3-capsule chain")
        log_pem = _log_pubkey_pem()
        chain_records.append(
            _seal_and_anchor("1 write_order/submit_order", order1_id, order1.capsule, log_pem)
        )
        chain_records.append(
            _seal_and_anchor("2 decide/approve_large_order(REJECTED)", decide1_id, decide1.capsule, log_pem)
        )
        chain_records.append(
            _seal_and_anchor("3 fyi/escalate_to_manager", escalate1_id, escalate1.capsule, log_pem)
        )
        assert all(r["verify_ok"] and r["receipt_ok"] for r in chain_records)

        _section("Step 9 — verify permalinks")
        for r in chain_records:
            link = _permalink(r["capsule_id"], r["capsule"])
            print(f"  [{r['label']}] leaf={r['leaf_index']}")
            print(f"    {link}")
        bundle_link = _bundle_permalink(chain_records)
        print("\n  Bundle permalink (Chain Navigation table, VERDICT column executed → blocked → executed):")
        print(f"    {bundle_link}")
    else:
        print("\n[step 8] --no-anchor passed: skipping live anchor + permalinks (offline mode)")

    print("\n" + "=" * 60)
    print("Demo complete.")
    print(f"  ledger path: {ledger}  (temp; deleted on exit)")
    print("  Chain: write_order → decide(BLOCKED) → fyi (escalation).")
    print("  To use with real Goose: see examples/goose-capsule/server.py")
    print("=" * 60)
