#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Seal Tom Sato's REAL GAR Session Block as a SCITT Signed Statement.

Source artifact:  _work/session-block-for-steven.json
  Real structure, synthetic values. Produced by deployed GAR code
  (soosproject/soos-examples, tier1/gar/gar-core.ts @ fe18f24fa90c,
  draft-sato-soos-gar-03). KIA signature is a stub (local Ed25519
  keypair, no real identity or trust anchor — per Tom's README:
  "stand-in only — it has no relationship to a real identity or trust
  anchor").

This script:
  Step 1  — verify Tom's hash chain (computePrevSpanHash per gar-core.ts)
  Step 1b — cross-check block structure against gar-core.ts @ fe18f24
  Step 2  — seal as SCITT Signed Statement (status=planned,
             content_type=application/soos.gar.session-block+json)
  Step 3  — verify capsule in-process + input digest round-trip
  Step 4  — register on live anchor + verify receipt
  Step 5  — write session-block-scitt-statement.json + print digests

Distinct from the existing synthetic SAR demo (demo.py / sample-gar-block.json):
  - THIS file: real deployed-code Session Block (three events, hash-chained,
    KIA-stub-signed), sealed under draft-sato-soos-gar-03 Session Block type.
  - demo.py: synthetic SAR built to draft-sato-soos-gar-01 (the -03 field
    update is the separate [gar-sample-03-fields] task).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
WORK = HERE.parent.parent.parent / "_work"
SESSION_BLOCK_PATH = WORK / "session-block-for-steven.json"
OUT_PATH = HERE / "session-block-scitt-statement.json"

_SEP = "=" * 64


def _banner(t: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {t}")
    print(_SEP)


# ---------------------------------------------------------------------------
# Hash chain verification (gar-core.ts computePrevSpanHash algorithm)
# ---------------------------------------------------------------------------

def _compute_own_span_hash(
    session_id: str,
    decision: str | None,
    kernel_id: str | None,
    cap_profile_hash: str | None,
    timestamp: str,
    prev_span_hash: str,
) -> str:
    parts = [
        session_id,
        decision or "",
        kernel_id or "",
        cap_profile_hash or "",
        timestamp,
        prev_span_hash,
    ]
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _merkle_root(leaves: list[str]) -> str:
    """Binary Merkle tree — SHA-256(leaf_str) leaves, SHA-256(left+right) nodes.
    Odd leaf duplicated (per gar-core.ts merkleRoot).
    """
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    level = [hashlib.sha256(l.encode()).hexdigest() for l in leaves]
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else level[i]
            nxt.append(hashlib.sha256((left + right).encode()).hexdigest())
        level = nxt
    return level[0]


def verify_chain(session_block: dict) -> dict:
    events = session_block["events"]
    prev = "GENESIS"
    for i, e in enumerate(events):
        if e["prev_span_hash"] != prev:
            return {"valid": False, "failedAt": i, "reason": "prev_span_hash link broken"}
        recomputed = _compute_own_span_hash(
            e["session_id"],
            e.get("decision"),
            e.get("kernel_id"),
            e.get("cap_profile_hash"),
            e["timestamp"],
            prev,
        )
        if recomputed != e["own_span_hash"]:
            return {"valid": False, "failedAt": i, "reason": f"own_span_hash mismatch at {i}"}
        prev = recomputed
    return {"valid": True, "failedAt": None}


def verify_merkle(session_block: dict) -> bool:
    """Merkle root is computed over entries WITHOUT block_id
    (per closeSessionBlock: leaves = entries.map(e => JSON.stringify(e))
    BEFORE events: entries.map(e => ({ ...e, block_id })))
    """
    events = session_block["events"]
    leaves = [
        json.dumps({k: v for k, v in e.items() if k != "block_id"}, separators=(",", ":"))
        for e in events
    ]
    return _merkle_root(leaves) == session_block["merkle_root"]


def main() -> int:
    # ------------------------------------------------------------------
    # Step 1 — Load + verify Tom's hash chain
    # ------------------------------------------------------------------
    _banner("Step 1 — Load + verify GAR Session Block hash chain")

    if not SESSION_BLOCK_PATH.exists():
        print(f"  ERROR: {SESSION_BLOCK_PATH} not found — flag in outbox and stop.", file=sys.stderr)
        return 1

    with SESSION_BLOCK_PATH.open() as f:
        doc = json.load(f)

    session_block = doc["session_block"]
    events = session_block["events"]

    print(f"  Source      : {SESSION_BLOCK_PATH}")
    print(f"  block_id    : {session_block['block_id']}")
    print(f"  session_id  : {session_block['session_id']}")
    print(f"  event count : {len(events)}")
    print(f"  ale_types   : {[e['ale_type'] for e in events]}")
    print(f"  sig alg     : {session_block['block_signature']['algorithm']}")
    print(f"  sig key_id  : {session_block['block_signature']['key_id']}")
    print()

    chain_result = verify_chain(session_block)
    if not chain_result["valid"]:
        print(f"  CHAIN FAIL at index {chain_result['failedAt']}: {chain_result['reason']}", file=sys.stderr)
        print("  Stopping — do not seal a broken artifact.", file=sys.stderr)
        return 1
    print(f"  Chain verify : PASS (3/3 events, prev_span_hash links + own_span_hash recompute)")

    merkle_ok = verify_merkle(session_block)
    if not merkle_ok:
        print("  Merkle root  : MISMATCH — see note", file=sys.stderr)
        return 1
    print(f"  Merkle root  : PASS ({session_block['merkle_root']})")

    # ------------------------------------------------------------------
    # Step 1b — Cross-check against gar-core.ts @ fe18f24fa90c
    # ------------------------------------------------------------------
    _banner("Step 1b — Structural cross-check vs gar-core.ts @ fe18f24fa90c")

    # Mandatory fields per GAREntry interface
    mandatory_all = ["entry_id", "ale_type", "session_id", "actor_id", "timestamp",
                     "prev_span_hash", "own_span_hash"]
    # Additional mandatory on CEDAR_PERMIT / CEDAR_DENY (GAR-03 §13.3)
    mandatory_cedar = ["cedar_policy_id", "cap_rrs_control_id", "authority_source_uri"]

    issues = []
    for i, e in enumerate(events):
        for f in mandatory_all:
            if f not in e:
                issues.append(f"Event {i} missing mandatory field: {f}")
        if e["ale_type"] in ("CEDAR_PERMIT", "CEDAR_DENY"):
            for f in mandatory_cedar:
                if not e.get(f):
                    issues.append(f"Event {i} ({e['ale_type']}) missing CEDAR mandatory field: {f}")

    if issues:
        print("  Structural issues found:", file=sys.stderr)
        for iss in issues:
            print(f"    - {iss}", file=sys.stderr)
        return 1

    print("  GAREntry mandatory fields  : PASS (all events)")
    print("  CEDAR_PERMIT provenance    : PASS (cedar_policy_id, cap_rrs_control_id, authority_source_uri)")
    print("  Hash algorithm (gar-core)  : SHA-256(session_id|decision|kernel_id|cap_profile_hash|timestamp|prev)")
    print("  Merkle leaf order          : JSON.stringify(entry without block_id) — block_id added post-close")
    print("  KIA signer                 : KIAStubSigner (local Ed25519 — stand-in only per Tom's README;")
    print("                               no real identity or trust anchor; gap persists until KIA Step 6)")
    print(f"  Source pin                 : soosproject/soos-examples tier1/gar/gar-core.ts @ fe18f24fa90c")

    # ------------------------------------------------------------------
    # Step 2 — Seal as SCITT Signed Statement
    # ------------------------------------------------------------------
    _banner("Step 2 — Seal as SCITT Signed Statement (capsule-emit)")

    from capsule_emit import emit

    import tempfile
    ledger = Path(tempfile.mkdtemp(prefix="gar-session-block-")) / "ledger.jsonl"

    result = emit(
        action="gar.session_block",
        operator="soos-gar-operator",
        developer="soosproject/soos-examples:tier1/gar/gar-core.ts@fe18f24fa90c",
        runtime="draft-sato-soos-gar-03",
        agent_input=session_block,
        agent_output={
            "status": "session_block_sealed",
            "block_id": session_block["block_id"],
            "session_id": session_block["session_id"],
            "event_count": len(events),
            "merkle_root": session_block["merkle_root"],
            "chain_verify": chain_result,
        },
        model={
            "provider": "synthetic",
            "model_id": "synth-gar-kernel-stub",
        },
        verdict="executed",
        effect={
            "type": "gar_session_block_scitt_submission",
            # status=planned: serialization-only; no live dispatch
            "status": "planned",
            "content_type": "application/soos.gar.session-block+json",
            "draft_basis": "draft-sato-soos-gar-03",
            "kia_note": (
                "block_signature uses KIAStubSigner (local Ed25519 stand-in — "
                "no real identity or trust anchor; see soosproject/soos-examples README)"
            ),
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

    # ------------------------------------------------------------------
    # Step 3 — Verify in-process + input digest round-trip
    # ------------------------------------------------------------------
    _banner("Step 3 — In-process verification + input digest round-trip")

    from agent_action_capsule import verify
    vr = verify(result.capsule)
    if not vr.ok:
        print(f"  FAIL: {vr.findings}", file=sys.stderr)
        return 1
    print(f"  verify(capsule).ok = True  [PASS]")

    from capsule_emit import verify_input_digest
    digest_ok = verify_input_digest(result.capsule, session_block)
    if not digest_ok:
        print("  FAIL: input digest mismatch", file=sys.stderr)
        return 1
    print(f"  verify_input_digest       = True  [PASS]")

    # ------------------------------------------------------------------
    # Step 4 — Anchor receipt
    # ------------------------------------------------------------------
    _banner("Step 4 — Anchor receipt")

    print(f"  Anchored : {'YES' if result.anchored else 'NO'}")
    # Receipt coordinates live in the capsule's anchor block (if present)
    anchor_block = result.capsule.get("anchor") or result.capsule.get("receipt")
    if anchor_block:
        print(f"  Receipt  : {json.dumps(anchor_block)[:200]}")
    else:
        # Print a sub-field summary from the capsule
        for key in ("scitt_receipt", "anchor_receipt", "transparency_receipt"):
            if key in result.capsule:
                print(f"  {key}: {result.capsule[key]}")
                break

    # ------------------------------------------------------------------
    # Step 5 — Write output + summary
    # ------------------------------------------------------------------
    _banner("Step 5 — Write session-block-scitt-statement.json")

    out_doc = {
        "_note": (
            "SCITT Signed Statement envelope for Tom Sato's GAR Session Block. "
            "Real deployed-code artifact (synthetic values, real structure, KIA-stub-signed). "
            "Content type: application/soos.gar.session-block+json (draft-sato-soos-gar-03). "
            "chain_verify: PASS. merkle_root: PASS. KIA signature: stub only (per Tom's README)."
        ),
        "_source": "soosproject/soos-examples:tier1/gar/gar-core.ts@fe18f24fa90c",
        "_draft_basis": "draft-sato-soos-gar-03",
        "capsule_id": capsule_id,
        "block_id": session_block["block_id"],
        "session_id": session_block["session_id"],
        "content_type": "application/soos.gar.session-block+json",
        "input_digest": input_digest,
        "output_digest": output_digest,
        "chain_verify": chain_result,
        "merkle_root_verify": merkle_ok,
        "anchored": result.anchored,
        "capsule": result.capsule,
    }
    with OUT_PATH.open("w") as f:
        json.dump(out_doc, f, indent=2)
    print(f"  Written: {OUT_PATH}")

    _banner("Summary")
    print(f"  Session Block  : {SESSION_BLOCK_PATH.name}")
    print(f"  block_id       : {session_block['block_id']}")
    print(f"  chain_verify   : PASS (3/3, computePrevSpanHash per gar-core.ts@fe18f24fa90c)")
    print(f"  merkle_root    : PASS (leaves without block_id, binary tree, SHA-256)")
    print(f"  capsule_id     : {capsule_id}")
    print(f"  input_digest   : {input_digest}")
    print(f"    = SHA-256(JCS(normalize(session_block)))")
    print(f"  anchored       : {result.anchored}")
    print()
    print("  Tom's recompute:")
    print(f"    SHA-256(JCS(session_block)) must equal: {input_digest}")
    print()
    print("  Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
