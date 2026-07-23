#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Seal an A2A task request as an AAC capsule — boundary seal tuple generator.

Reads task_request.json from this directory, seals the task input/output as a
SCITT Signed Statement via capsule-emit, and writes boundary_seal_output.json.

Usage:
    pip install "capsule-emit" "a2a-sdk==1.1.1"
    python seal_boundary_tuple.py

SDK note (a2a-sdk 1.1.1 @ 86c6b0d):
  Zero process-local shims required. The task input is extracted from the
  task_request.json as a plain dict; no SDK round-trip through protobuf is
  needed to produce the deterministic sealing input. Anton's two documented
  shims address server-side message routing (out of scope for a producer-only
  tuple); they are not applicable here.

Output: boundary_seal_output.json. The capsule.resolve gate is set from a REAL
idempotent round-trip to the live anchor (POST /v1/digest), not assumed; the
process exits non-zero if resolve does not reproduce.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
REQUEST_PATH = HERE / "task_request.json"
OUT_PATH = HERE / "boundary_seal_output.json"

_SEP = "=" * 64


def _banner(t: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {t}")
    print(_SEP)


def main() -> int:
    _banner("Step 1 — Load task_request.json")

    with REQUEST_PATH.open() as f:
        request = json.load(f)

    msg = request["params"]["message"]
    task_id = msg["taskId"]
    message_id = msg["messageId"]
    role = msg["role"]
    text = msg["parts"][0]["text"]

    print(f"  task_id    : {task_id}")
    print(f"  message_id : {message_id}")
    print(f"  role       : {role}")
    print(f"  text       : {text}")

    # ------------------------------------------------------------------
    # agent_input: canonical representation of the A2A task request
    # Fields committed: task_id, message_id, role, text
    # (sessionId absent in this request — not included)
    # ------------------------------------------------------------------
    agent_input = {
        "a2a_request": {
            "method": request["method"],
            "task_id": task_id,
            "message_id": message_id,
            "role": role,
            "text": text,
        }
    }

    # ------------------------------------------------------------------
    # agent_output: synthetic deterministic response
    # ------------------------------------------------------------------
    agent_output = {
        "a2a_response": {
            "task_id": task_id,
            "status": "completed",
            "artifact": {
                "name": "equity-data",
                "parts": [
                    {
                        "text": (
                            "AAPL | ticker: AAPL | sector: Technology | "
                            "exchange: NASDAQ | source: public — "
                            "synthetic deterministic fixture"
                        )
                    }
                ],
            },
        }
    }

    _banner("Step 2 — Seal as SCITT Signed Statement (capsule-emit)")

    from capsule_emit import emit

    ledger = Path(tempfile.mkdtemp(prefix="a2a-boundary-")) / "ledger.jsonl"

    result = emit(
        action="a2a.boundary_seal",
        operator="action-state-group",
        developer="a2a-sdk==1.1.1@86c6b0d",
        runtime="draft-mih-scitt-agent-action-capsule-02",
        agent_input=agent_input,
        agent_output=agent_output,
        model={
            "provider": "synthetic",
            "model_id": "boundary-seal-reference",
        },
        verdict="executed",
        effect={
            "type": "a2a.task_completed",
            "status": "confirmed",
            "task_id": task_id,
        },
        anchor=True,
        ledger=ledger,
    )

    compute = result.capsule["model_attestation"]["compute_attestation"]
    input_digest = compute["agent_input_digest"]
    output_digest = compute["agent_output_digest"]
    capsule_id = result.capsule_id

    print(f"  capsule_id    : {capsule_id}")
    print(f"  anchored      : {result.anchored}")
    print(f"  input_digest  : {input_digest}")
    print(f"  output_digest : {output_digest}")

    _banner("Step 3 — Verify in-process")

    from agent_action_capsule import verify
    vr = verify(result.capsule)
    if not vr.ok:
        print(f"  FAIL: {vr.findings}", file=sys.stderr)
        return 1
    print("  verify(capsule).ok = True  [PASS]")

    from capsule_emit import verify_input_digest
    digest_ok = verify_input_digest(result.capsule, agent_input)
    if not digest_ok:
        print("  FAIL: input digest mismatch", file=sys.stderr)
        return 1
    print("  verify_input_digest       = True  [PASS]")

    _banner("Step 4 — Resolve on the live anchor (capsule.resolve gate)")

    # capsule.resolve is a REAL round-trip to the anchor, not an assumption:
    # POST /v1/digest is idempotent, so re-submitting the just-anchored
    # capsule_id returns its existing CT-log coordinates + COSE Receipt without
    # creating a duplicate. entry_hash MUST equal SHA-256(bytes.fromhex(
    # capsule_id)) — the offline-verify contract. The gate is PASS only if the
    # anchor returns 200 AND that entry_hash matches.
    anchor_base = os.environ.get("AAC_ANCHOR_URL", "https://anchor.agentactioncapsule.org").rstrip("/")
    resolve_endpoint = f"{anchor_base}/v1/digest"
    expected_entry_hash = hashlib.sha256(bytes.fromhex(capsule_id)).hexdigest()

    resolve_ok = False
    entry_hash = leaf_index = tree_size = None
    inclusion_proof_url = None
    try:
        req = urllib.request.Request(
            resolve_endpoint,
            data=json.dumps({"capsule_id": capsule_id}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            body = json.loads(resp.read())
        entry_hash = body.get("entry_hash")
        leaf_index = body.get("leaf_index")
        tree_size = body.get("tree_size")
        resolve_ok = status == 200 and entry_hash == expected_entry_hash
        if leaf_index is not None and tree_size is not None:
            inclusion_proof_url = (
                f"{anchor_base}/anchor/inclusion-proof-ct"
                f"?leaf_index={leaf_index}&tree_size={tree_size}"
            )
        print(f"  resolve HTTP  : {status}")
        print(f"  entry_hash    : {entry_hash}")
        print(f"  expected      : {expected_entry_hash}")
        print(f"  leaf_index    : {leaf_index}   tree_size : {tree_size}")
    except Exception as exc:  # noqa: BLE001 — record the failure, don't fake a PASS
        print(f"  resolve FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)

    resolve_gate = "PASS" if resolve_ok else "DENY"
    print(f"  capsule.resolve gate = {resolve_gate}")

    _banner("Step 5 — Write boundary_seal_output.json")

    # A2A response extension — the capsule reference the callee would attach.
    # Every URL below points at a live anchor route (no phantom endpoints):
    #   resolve_endpoint    POST /v1/digest         (idempotent resolve + Receipt)
    #   inclusion_proof_url GET  /anchor/inclusion-proof-ct?leaf_index=&tree_size=
    #   resolve_by_id_url   GET  /v1/inclusion/{capsule_id}  (read-only; live after
    #                       the anchor deploy that adds the route)
    a2a_response_extension = {
        "uri": "https://agentactioncapsule.org/a2a-extension/v1",
        "capsule_id": capsule_id,
        "anchor": anchor_base,
        "resolve_endpoint": resolve_endpoint,
        "inclusion_proof_url": inclusion_proof_url,
        "resolve_by_id_url": f"{anchor_base}/v1/inclusion/{capsule_id}",
        "entry_hash": entry_hash,
        "leaf_index": leaf_index,
        "tree_size": tree_size,
    }

    out_doc = {
        "_note": (
            f"Positive case — boundary seal. "
            f"capsule.digest gate: PASS (digest matches sealed content). "
            f"capsule.resolve gate: {resolve_gate} "
            f"(reproduced via POST /v1/digest; entry_hash match = {resolve_ok}). "
            f"a2a-sdk: 1.1.1@86c6b0d. Shims: none."
        ),
        "_sdk_version": "a2a-sdk==1.1.1",
        "_sdk_commit": "86c6b0d",
        "_sdk_shims": "none",
        "task_id": task_id,
        "capsule_id": capsule_id,
        "anchored": result.anchored,
        "input_digest": input_digest,
        "output_digest": output_digest,
        "gate_results": {
            "capsule.digest": "PASS",
            "capsule.resolve": resolve_gate,
        },
        "a2a_response_extension": a2a_response_extension,
        "capsule": result.capsule,
    }

    with OUT_PATH.open("w") as f:
        json.dump(out_doc, f, indent=2)
    print(f"  Written: {OUT_PATH}")

    _banner("Summary")
    print(f"  task_id       : {task_id}")
    print(f"  capsule_id    : {capsule_id}")
    print(f"  input_digest  : {input_digest}")
    print(f"  output_digest : {output_digest}")
    print(f"  anchored      : {result.anchored}")
    print()
    print("  Gate results (positive case):")
    print("    capsule.digest  : PASS")
    print(f"    capsule.resolve : {resolve_gate}")
    return 0 if resolve_ok else 1


if __name__ == "__main__":
    sys.exit(main())
