#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""LOOPBACK REHEARSAL — our A2A client against our own served agent.

Classified as a REHEARSAL (single host, loopback) — NOT the bilateral close.
Sends the deterministic boundary-seal task over A2A JSON-RPC, then verifies the
returned capsule reference OFFLINE via scitt-cose (receipt + Merkle inclusion).
Its coordinates are recorded separately from any close.
"""
import asyncio
import base64
import hashlib
import json

import httpx
from a2a.client import ClientConfig, create_client
from a2a.types import a2a_pb2 as pb
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from google.protobuf.json_format import MessageToDict
from scitt_cose.merkle import verify_inclusion
from scitt_cose.receipt import verify_receipt

ENDPOINT = "http://127.0.0.1:8080"
ANCHOR = "https://anchor.agentactioncapsule.org"


def _sep(t):
    print("\n" + "=" * 64 + f"\n  {t}\n" + "=" * 64)


async def main() -> int:
    _sep("REHEARSAL — A2A loopback (our client -> our endpoint)")
    async with httpx.AsyncClient(timeout=60) as hc:
        cfg = ClientConfig(
            streaming=False,
            polling=True,
            httpx_client=hc,
            supported_protocol_bindings=["JSONRPC"],
        )
        client = await create_client(ENDPOINT, client_config=cfg)
        # A new task must NOT carry a pre-existing task_id; the server assigns it.
        msg = pb.Message(
            message_id="msg-boundary-seal-001",
            role=pb.Role.ROLE_USER,
            parts=[pb.Part(text="Retrieve current public equity data for: AAPL (Apple Inc.) — boundary seal test vector")],
        )
        req = pb.SendMessageRequest(message=msg)

        task_id = None
        async for resp in client.send_message(req):
            t = getattr(resp, "task", None)
            if t is not None and t.id:
                task_id = t.id
        assert task_id, "no task id returned from send_message"

        # Poll get_task until terminal (executor seals+anchors then completes).
        final_task = None
        for _ in range(30):
            ft = await client.get_task(pb.GetTaskRequest(id=task_id))
            state = ft.status.state
            if state in (pb.TaskState.TASK_STATE_COMPLETED, pb.TaskState.TASK_STATE_FAILED):
                final_task = ft
                break
            await asyncio.sleep(0.3)
        assert final_task is not None, "task did not reach a terminal state"
        assert final_task.status.state == pb.TaskState.TASK_STATE_COMPLETED, (
            f"task ended in {pb.TaskState.Name(final_task.status.state)}"
        )
        task = MessageToDict(final_task)
        print("  task id     :", task.get("id"))
        print("  state       :", task.get("status", {}).get("state"))
        arts = task.get("artifacts", [])
        assert arts, "no artifacts on task"
        meta = arts[0].get("metadata", {})
        binding = meta.get("https://agentactioncapsule.org/a2a-extension/v1", meta)
        print("  capsule_id  :", binding.get("capsule_id"))
        print("  leaf_index  :", binding.get("leaf_index"), " tree_size:", binding.get("tree_size"))

    # --- OFFLINE verification of the returned capsule reference ---
    _sep("REHEARSAL — offline verify of the returned capsule reference")
    cid = binding["capsule_id"]
    entry_hash = hashlib.sha256(bytes.fromhex(cid)).hexdigest()
    assert entry_hash == binding["entry_hash"], "entry_hash mismatch vs returned binding"

    # fetch the frozen receipt (idempotent resolve) + authority key, verify offline
    with httpx.Client(timeout=30) as c:
        reg = c.post(f"{ANCHOR}/v1/digest", json={"capsule_id": cid}).json()
        receipt = base64.b64decode(reg["receipt_b64"])
        pk = c.get(f"{ANCHOR}/anchor/authority-pubkey").json()
        ip = c.get(f"{ANCHOR}/anchor/inclusion-proof-ct",
                   params={"leaf_index": reg["leaf_index"], "tree_size": reg["tree_size"]}).json()
    pem = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pk["pubkey_hex"])).public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    r = verify_receipt(receipt, leaf_entry_hex=entry_hash, log_public_key_pem=pem)
    print(f"  verify_receipt: ok={r.ok} root={r.root}")
    assert r.ok
    fold = verify_inclusion(entry_hash, ip["leaf_index"], ip["tree_size"], ip["audit_path"], root_hex=ip["root_hash"])
    print(f"  verify_inclusion (independent RFC6962 fold): {fold}")
    assert fold and ip["root_hash"] == r.root

    print("\nREHEARSAL PASS — A2A loopback exchange produced a capsule that")
    print("resolves + verifies offline. Coordinates (record separately from any close):")
    print(json.dumps({
        "classification": "loopback-rehearsal (single host)",
        "endpoint": f"{ENDPOINT}/a2a",
        "capsule_id": cid,
        "entry_hash": entry_hash,
        "leaf_index": reg["leaf_index"],
        "tree_size": reg["tree_size"],
        "root_hash": r.root,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
