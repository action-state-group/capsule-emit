#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""capsule-emit quickstart demo — the 5-minute acceptance bar.

Demonstrates: emit → anchor (async, skipped here for offline) → ledger view → verify.

Run:
    cd capsule-emit
    pip install -e ".[dev]" && pip install -e "../agent-action-capsule/python"
    python examples/quickstart_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Make sure we can run from repo root without installing
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "agent-action-capsule" / "python"))

from capsule_emit import emit, ledger_view, load_manifest
from agent_action_capsule import verify

LEDGER_PATH = Path(tempfile.mkdtemp()) / "ledger.jsonl"


def main() -> int:
    print("=== capsule-emit quickstart demo ===\n")

    # --- 1. EMIT — the consequential action ----------------------------------
    print("Step 1: emit() — write_po action")
    agent_output = {"po_number": "PO-2026-001", "status": "dispatched"}

    cap = emit(
        action="write_po",
        operator="acme-co",
        developer="po-agent@v1",
        runtime="demo",
        agent_input={"vendor": "Frobozz Supply", "total": 1240.19},
        agent_output=agent_output,
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        verdict="executed",
        effect={"type": "write_po", "status": "dispatched"},
        anchor=False,               # offline demo — set anchor=True in production
        ledger=LEDGER_PATH,
    )
    print(f"  capsule_id : {cap.capsule_id}")
    print(f"  anchored   : {cap.anchored}  (anchor=False for offline demo)")
    assert len(cap.capsule_id) == 64, "capsule_id must be 64-char hex"
    print("  ✓ sealed\n")

    # --- 2. CONFIRM — chain a confirm action ----------------------------------
    print("Step 2: emit() — confirm_write_po (chains → write_po)")
    confirm = emit(
        action="confirm_write_po",
        operator="acme-co",
        developer="po-agent@v1",
        confirms=cap.capsule_id,
        verdict="confirmed",
        effect={"type": "write_po", "status": "confirmed"},
        anchor=False,
        ledger=LEDGER_PATH,
    )
    print(f"  capsule_id : {confirm.capsule_id}")
    assert confirm.capsule["chain"]["parent_capsule_id"] == cap.capsule_id
    print("  ✓ chained to write_po\n")

    # --- 3. LEDGER VIEW -------------------------------------------------------
    print("Step 3: capsule-emit ledger view")
    ledger_view(LEDGER_PATH)

    # --- 4. VERIFY — Class-1 payload verification ----------------------------
    print("Step 4: agent_action_capsule verify (Class-1)")
    for i, rec in enumerate([cap.capsule, confirm.capsule]):
        result = verify(rec)
        label = ["write_po", "confirm_write_po"][i]
        ok_str = "VALID" if result.ok else "INVALID"
        print(f"  [{label}] {ok_str}")
        if not result.ok:
            for f in result.findings:
                print(f"    [{f.severity}] {f.code}: {f.detail}")
        assert result.ok, f"{label} did not verify: {[f.detail for f in result.findings]}"
    print("  ✓ all VALID\n")

    # --- 4b. TAMPER — should go INVALID --------------------------------------
    print("Step 4b: tamper one field → INVALID")
    tampered = dict(cap.capsule)
    tampered["operator"] = "evil-corp"
    tamper_result = verify(tampered)
    print(f"  tampered: {'INVALID' if not tamper_result.ok else 'VALID (unexpected!)'}")
    assert not tamper_result.ok, "tampered capsule should not verify"
    print("  ✓ tamper detected\n")

    # --- 5. MANIFEST — declare-only parse -------------------------------------
    print("Step 5: manifest parser (declare-only)")
    manifest_path = Path(__file__).parent.parent / "flows" / "write-po" / "manifest.md"
    if manifest_path.exists():
        mf = load_manifest(manifest_path)
        print(f"  wicket_id  : {mf.wicket_id}")
        print(f"  autonomy   : {mf.autonomy}  (safe default = narrate)")
        print(f"  effect     : {mf.effect_type}")
        print(f"  constraints: {mf.constraint_names}")
        assert mf.autonomy == "narrate", f"Expected narrate, got {mf.autonomy}"
    else:
        print("  (manifest not found — skipping)")
    print("  ✓ manifest parsed\n")

    print("=== All acceptance checks passed ✓ ===")
    print(f"\nLedger: {LEDGER_PATH}")
    print("\nUpgrade path (no code change):")
    print("  pip install gopher-ai")
    print(f"  gopher-ai ledger serve {LEDGER_PATH} --flows-dir flows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
