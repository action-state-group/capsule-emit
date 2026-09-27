# SPDX-License-Identifier: Apache-2.0
"""Regenerate test-vectors/bilateral-payloads/vectors.json.

The expected bytes come from the independent Go oracle
(``test-vectors/go-oracle/jcs_oracle.go``), never from capsule-emit, so the
committed file is a cross-implementation vector. Set ``AAC_JCS_ORACLE`` to the
directory holding the oracle's scratch module (see the oracle's header).

    AAC_JCS_ORACLE=/tmp/aac-jcs-oracle python test-vectors/bilateral-payloads/scripts/generate_vectors.py
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "vectors.json"
D1 = "9f2c4a1b" * 8
D2 = "0123abcd" * 8

CASES = [
    {"id": "request-ascii", "fn": "request_payload", "args": ["org-a", "org-b", D1],
     "object": {"phase": "request", "requester_org": "org-a", "responder_org": "org-b", "action_digest": D1}},
    {"id": "request-no-responder", "fn": "request_payload", "args": ["org-a", None, D1],
     "object": {"phase": "request", "requester_org": "org-a", "responder_org": None, "action_digest": D1}},
    {"id": "request-non-ascii", "fn": "request_payload", "args": ["Zürich Rück AG", "株式会社ビー", D1],
     "object": {"phase": "request", "requester_org": "Zürich Rück AG", "responder_org": "株式会社ビー",
                "action_digest": D1}},
    {"id": "action-astral-and-u2028", "fn": "action_payload", "args": ["hs-\U0001F9AB-1", "org b", D2],
     "object": {"phase": "action", "handshake_id": "hs-\U0001F9AB-1", "responder_org": "org b",
                "request_sig_digest": D2}},
    {"id": "confirm-non-ascii", "fn": "confirm_payload", "args": ["hs-1", "Société Générale é", D2],
     "object": {"phase": "confirm", "handshake_id": "hs-1", "party_org": "Société Générale é",
                "acked_sig_digest": D2}},
]


def main() -> None:
    oracle = os.environ["AAC_JCS_ORACLE"]
    raw = subprocess.run(
        ["go", "run", "main.go"], cwd=oracle, check=True, capture_output=True,
        input=json.dumps([c["object"] for c in CASES]).encode(),
    ).stdout
    for case, result in zip(CASES, json.loads(raw)):
        case["jcs_hex"] = result["jcs_hex"]
        case["sha256"] = result["sha256"]
    doc = {
        "description": "Bilateral attestation signed-payload bytes: RFC 8785 JCS of the phase object, "
        "UTF-8. Expected bytes were produced by an independent implementation "
        "(agent-action-capsule go/canonical.JCS), not by capsule-emit.",
        "canonicalization": "jcs (RFC 8785)",
        "cases": CASES,
    }
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
