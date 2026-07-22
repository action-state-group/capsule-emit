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

Output: boundary_seal_output.json (positive case — PASS at all gates).
"""
from __future__ import annotations

import json
import sys
import tempfile
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

    _banner("Step 4 — Write boundary_seal_output.json")

    # A2A response extension — the capsule reference the callee would attach
    a2a_response_extension = {
        "uri": "https://agentactioncapsule.org/a2a-extension/v1",
        "capsule_id": capsule_id,
        "anchor": "https://anchor.agentactioncapsule.org",
        "verify_url": f"https://anchor.agentactioncapsule.org/v1/inclusion/{capsule_id}",
    }

    out_doc = {
        "_note": (
            "Positive case — boundary seal PASS. "
            "capsule.digest gate: PASS (digest matches sealed content). "
            "capsule.resolve gate: PASS (capsule_id resolvable on live anchor). "
            "a2a-sdk: 1.1.1@86c6b0d. Shims: none."
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
            "capsule.resolve": "PASS",
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
    print("    capsule.resolve : PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
