#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""GAR Session Audit Record (SAR) -> SCITT Signed Statement example.

SYNTHETIC — produced for IETF 126 Vienna hackathon testability, not a
production artifact. All keys, IDs, and signatures labeled 'synth-' are
non-functional placeholders. See README.md.

Draft basis:
  draft-sato-soos-gar-01  (GAR = Governance Audit Record, SOOS family)
  draft-sato-soos-kia     (KIA = Kernel Identity and Attestation)

Key terms used here:
  kernel-attested — GEC (Governing Enforcement Component) originated;
                    see draft-sato-soos-gar Section 4 / RFC 9334 RATS.
  KIA-signed      — signed by the GEC keypair whose authority is
                    established by the KIA chain (draft-sato-soos-kia).
  content type    — application/soos.gar.sar+json (draft-sato-soos-gar
                    Section 10.1 SCITT submission profile).

What this demo does:
  1. Load the synthetic SAR (sample-gar-block.json) — per draft-sato-soos-gar
     Section 6.2 field set.
  2. Serialize it as an Agent Action Capsule SCITT Signed Statement (the SAR
     becomes the agent_input, its canonical digest sealed in the capsule).
  3. Verify the capsule in-process with agent_action_capsule.verify.
  4. Verify the input digest round-trips via capsule_emit.verify_input_digest.
  5. Optionally anchor the digest to the public log (default: --no-anchor).
  6. Write sample-scitt-statement.json alongside sample-gar-block.json.

Tom's reciprocal step:
  Run your SOOS-side verifier against sample-gar-block.json (standalone JSON,
  no Python needed). The signature field is synthetic — substitute a real
  GEC keypair signature to complete the KIA verification round-trip.

Run:
    pip install capsule-emit agent-action-capsule
    python3 demo.py --no-anchor    # offline / fully synthetic
    python3 demo.py                # with anchor POST to public log
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent

_SEP = "=" * 64


def _banner(t: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {t}")
    print(_SEP)


def run_demo(anchor: bool) -> int:
    # ------------------------------------------------------------------
    # Step 1 — Load the synthetic GAR Session Audit Record (SAR)
    # ------------------------------------------------------------------
    _banner("Step 1 — Load synthetic GAR Session Audit Record (SAR)")

    sar_path = HERE / "sample-gar-block.json"
    with sar_path.open() as f:
        gar_block: dict = json.load(f)

    print(f"  Source : {sar_path}")
    print(f"  Version: {gar_block.get('gar_version')}")
    print(f"  SAR ID : {gar_block.get('sar_id')}")
    print(f"  Session: {gar_block.get('session_id')}")
    print(f"  Close  : {gar_block.get('close_reason')}")
    print(f"  Note   : {gar_block.get('_note', '')[:72]}...")
    print()
    print("  Full SAR (canonical excerpt):")
    for k, v in gar_block.items():
        if k.startswith("_"):
            continue
        vstr = json.dumps(v) if not isinstance(v, str) else v
        print(f"    {k}: {vstr[:80]}")

    # ------------------------------------------------------------------
    # Step 2 — Serialize as SCITT Signed Statement (via capsule-emit)
    # ------------------------------------------------------------------
    _banner("Step 2 — Serialize as SCITT Signed Statement (capsule-emit)")

    from capsule_emit import emit

    ledger = Path(tempfile.mkdtemp(prefix="gar-capsule-demo-")) / "ledger.jsonl"

    # The SAR block is the sealed action input (agent_input). capsule-emit
    # commits it by canonical-JSON SHA-256 digest (content stays local).
    # The content_type that draft-sato-soos-gar Section 10.1 specifies for
    # SCITT submission is application/soos.gar.sar+json; we record it in
    # the effect metadata so the capsule carries the MIME attribution.
    result = emit(
        action="gar.session_audit_record",
        operator="synth-soos-gec-operator",
        developer="synth-gec-kernel@draft-sato-soos-gar-01",
        runtime="draft-sato-soos-gar-01",
        agent_input=gar_block,
        agent_output={
            "status": "sar_serialized",
            "gar_version": gar_block.get("gar_version"),
            "sar_id": gar_block.get("sar_id"),
            "session_id": gar_block.get("session_id"),
        },
        model={
            "provider": "synthetic",
            "model_id": "synth-gec-kernel-001",
        },
        verdict="executed",
        effect={
            # status="planned": serialization-only record; no live dispatch.
            # This yields effect_mode="not_applicable" in Class-1 verify (§5.2
            # planned carve), which is correct here — we are sealing the SAR as
            # a verifiable record, not dispatching it to a live SCITT endpoint.
            "type": "gar_sar_scitt_submission",
            "status": "planned",
            "content_type": "application/soos.gar.sar+json",
            "draft_basis": "draft-sato-soos-gar-01 Section 10.1",
            "note": "SYNTHETIC — testability artifact for IETF 126 hackathon",
        },
        anchor=anchor,
        ledger=ledger,
    )

    print(f"  capsule_id : {result.capsule_id}")
    print(f"  anchored   : {result.anchored}")

    compute = result.capsule["model_attestation"]["compute_attestation"]
    input_digest = compute["agent_input_digest"]
    output_digest = compute["agent_output_digest"]
    print(f"  input_digest  (SAR):  {input_digest}")
    print(f"  output_digest (meta): {output_digest}")
    print()
    print("  The input_digest is the canonical SHA-256 of the SAR JSON.")
    print("  Tom's SOOS-side verifier can recompute it and compare.")

    # ------------------------------------------------------------------
    # Step 3 — Write sample-scitt-statement.json
    # ------------------------------------------------------------------
    _banner("Step 3 — Write sample-scitt-statement.json")

    scitt_out = HERE / "sample-scitt-statement.json"
    scitt_doc = {
        "_note": (
            "SYNTHETIC — SCITT Signed Statement envelope (capsule-emit format). "
            "The payload is the GAR SAR committed by canonical-JSON SHA-256 digest. "
            "IETF 126 Vienna hackathon testability artifact."
        ),
        "_draft_basis": "draft-sato-soos-gar-01 Section 10.1 SCITT submission profile",
        "capsule_id": result.capsule_id,
        "sar_id": gar_block.get("sar_id"),
        "gar_version": gar_block.get("gar_version"),
        "content_type": "application/soos.gar.sar+json",
        "agent_input_digest": input_digest,
        "agent_output_digest": output_digest,
        "capsule": result.capsule,
    }
    with scitt_out.open("w") as f:
        json.dump(scitt_doc, f, indent=2)
    print(f"  Written: {scitt_out}")

    # ------------------------------------------------------------------
    # Step 4 — Verify in-process (agent_action_capsule.verify)
    # ------------------------------------------------------------------
    _banner("Step 4 — In-process verification (agent_action_capsule.verify)")

    from agent_action_capsule import verify

    vr = verify(result.capsule)
    status = "PASS" if vr.ok else "FAIL"
    print(f"  verify(capsule).ok = {vr.ok}  [{status}]")
    if not vr.ok:
        print(f"  findings: {vr.findings}", file=sys.stderr)
        return 1
    print("  Tamper any byte in the capsule and this step fails.")

    # ------------------------------------------------------------------
    # Step 5 — Verify input digest round-trip
    # ------------------------------------------------------------------
    _banner("Step 5 — Input digest round-trip (verify_input_digest)")

    from capsule_emit import verify_input_digest

    digest_ok = verify_input_digest(result.capsule, gar_block)
    print(f"  verify_input_digest(capsule, gar_block) = {digest_ok}")
    if not digest_ok:
        print("  FAIL — input digest does not match!", file=sys.stderr)
        return 1
    print("  The digest sealed in the capsule matches the SAR JSON.")
    print("  Tom can recompute: SHA-256(JCS(sar_block)) and compare to agent_input_digest.")

    # ------------------------------------------------------------------
    # Step 6 — Summary
    # ------------------------------------------------------------------
    _banner("Summary")
    print(f"  SAR source   : {sar_path.name}")
    print(f"  SCITT output : {scitt_out.name}")
    print(f"  capsule_id   : {result.capsule_id}")
    print(f"  input_digest : {input_digest}")
    print(f"  anchored     : {result.anchored}")
    print()
    print("  Tom's reciprocal step:")
    print("    1. Load sample-gar-block.json (standalone JSON, no Python needed).")
    print("    2. Compute SHA-256(JCS(sar_block)) — must match the input_digest above.")
    print("    3. Run your SOOS-side KIA verifier against kia_signed.signature")
    print("       (signature field is synthetic here — substitute a real GEC keypair).")
    print()
    print("  Offline auditor verify:")
    print(f"    agent-action-capsule verify --store {ledger}")
    print()
    print("  Done.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "GAR SAR -> SCITT Signed Statement demo "
            "(draft-sato-soos-gar-01, synthetic, IETF 126 hackathon)"
        )
    )
    parser.add_argument(
        "--no-anchor",
        action="store_true",
        help="skip async anchor POST (run fully offline; default for synthetic demo)",
    )
    args = parser.parse_args()
    sys.exit(run_demo(anchor=not args.no_anchor))
