# SPDX-License-Identifier: Apache-2.0
"""Regenerate test-vectors/permalink-bundle/vectors.json.

Seals a fresh three-capsule chain (so ``capsules`` changes on every run), then
asks the independent Go oracle (``test-vectors/go-oracle/jcs_oracle.go``) for
the §9 fragment and JCS SHA-256 of each Bundle and pointer object. The
fragments and digests are never computed by capsule-emit. Set
``AAC_JCS_ORACLE`` to the oracle's scratch module directory.

    CAPSULE_WITNESS=off AAC_JCS_ORACLE=/tmp/aac-jcs-oracle \\
        python test-vectors/permalink-bundle/scripts/generate_vectors.py
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from capsule_emit import seal
from capsule_emit.permalink import build_bundle

OUT = Path(__file__).resolve().parents[1] / "vectors.json"
LOCATIONS = ["https://bundles.example.org/b/1.json", "ipfs://bafyexample"]


def _chain() -> list[dict]:
    root = seal({"order": "PO-7", "note": "Zürich"}, action="write_order", operator="example-org",
                developer="agent@v1", agent_output={"status": "dispatched"}, verdict="executed", anchor=False)
    denial = seal({"order": "PO-7", "amount": "125000.00"}, action="approve_order", operator="example-org",
                  developer="agent@v1", confirms=root.capsule_id, agent_output={"reason": "over ceiling \U0001F6D1"},
                  verdict="blocked", anchor=False)
    esc = seal({"order": "PO-7"}, action="escalate", operator="example-org", developer="agent@v1",
               confirms=denial.capsule_id, verdict="executed", anchor=False)
    # Drop capsule-emit's ledger bookkeeping (the producer envelope and its
    # key_id): not Capsule members, outside the capsule_id preimage, and a
    # public vector should not pin whichever local key sealed it.
    return [{k: v for k, v in r.capsule.items() if k not in ("signature", "key_id")} for r in (root, denial, esc)]


def main() -> None:
    capsules = _chain()
    disclosures = {
        capsules[0]["capsule_id"]: {"agent_input": {"order": "PO-7", "note": "Zürich"}},
        capsules[1]["capsule_id"]: {"agent_output": {"reason": "over ceiling \U0001F6D1"}},
    }
    cases = [
        {"id": "single", "bundle": False, "capsules": capsules[:1], "disclosures": None},
        {"id": "chain", "bundle": True, "capsules": capsules, "disclosures": None},
        {"id": "chain-disclosed", "bundle": True, "capsules": capsules, "disclosures": disclosures},
        {"id": "partial-chain", "bundle": True, "capsules": capsules[1:], "disclosures": None},
    ]
    for case in cases:
        case["expected_bundle"] = build_bundle(case["capsules"], bundle=case["bundle"],
                                               disclosures=case["disclosures"])
    objects = [c["expected_bundle"] for c in cases]
    oracle = os.environ["AAC_JCS_ORACLE"]

    def run(values):
        raw = subprocess.run(["go", "run", "main.go"], cwd=oracle, check=True, capture_output=True,
                             input=json.dumps(values).encode()).stdout
        return json.loads(raw)

    for case, result in zip(cases, run(objects)):
        case["fragment"] = result["fragment"]
        case["bundle_digest"] = result["sha256"]
        case["pointer"] = {"bundle_ref": {"digest": result["sha256"], "root": case["expected_bundle"]["root"],
                                          "locations": LOCATIONS}}
    for case, result in zip(cases, run([c["pointer"] for c in cases])):
        case["pointer_fragment"] = result["fragment"]
    doc = {
        "description": "Permalink fragments: evidence-bundle/v2 §9 codec (unpadded base64url over UTF-8 JCS) "
        "and the pointer form. fragment, bundle_digest and pointer_fragment were produced by an independent "
        "implementation (agent-action-capsule go/bundle.EncodeFragment and go/canonical.JCS), not by capsule-emit.",
        "locations": LOCATIONS,
        "cases": cases,
    }
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
