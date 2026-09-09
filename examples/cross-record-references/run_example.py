#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Cross-record references example (draft-04 §5.5.5).

Demonstrates seal(..., references=...) plumbing:

  - A plain seal(payload, references=...) adds a top-level Capsule
    references[] committed to capsule_id before signing.
  - Slot-composition seal(who(...), did(...), references=...) puts
    references[] on the COMPOSITION capsule only, not on member capsules.
  - Payload content with a 'references' key does NOT leak into Capsule
    references[] — only an explicit references= kwarg does.

Run:
    pip install "capsule-emit>=0.8.0"
    python examples/cross-record-references/run_example.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from capsule_emit import ReferenceEntry, did, seal, who  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"

        # --- 1. Plain seal with references= -----------------------------------
        # Seal an action and cite an external capsule by its content address.
        prior_id = "a" * 64  # a real capsule_id from another ledger or agent
        ref = ReferenceEntry(
            type="agent-action-capsule",
            digest_alg="SHA-256",
            digest=prior_id,
            citation_purpose="corroborates",
        )
        cap = seal(
            {"action": "approve_invoice", "amount": "2500.00"},
            references=(ref,),
            anchor=False,
            witness=False,
            ledger=ledger,
        )
        assert "references" in cap.capsule
        assert cap.capsule["references"][0]["digest"] == prior_id
        print(f"[1] plain seal with references: capsule_id={cap.capsule_id[:16]}…")
        print(f"    references[0].digest={cap.capsule['references'][0]['digest'][:16]}…")

        # --- 2. Payload 'references' key does NOT promote ---------------------
        # A payload dict that has a 'references' key is NOT the same as
        # passing references= to seal() — the key stays inside the digest.
        cap2 = seal(
            {"action": "retrieve", "references": ["some-id"]},
            anchor=False,
            witness=False,
            ledger=ledger,
        )
        assert "references" not in cap2.capsule
        print("[2] payload with 'references' key — no top-level references[]: OK")

        # --- 3. Slot-composition: references on the composition only ----------
        # On the slot-form, references= routes to the COMPOSITION capsule,
        # never to the individually-minted member capsules.
        ext_id = "b" * 64
        ext_ref = ReferenceEntry(
            type="agent-action-capsule",
            digest_alg="SHA-256",
            digest=ext_id,
        )
        composition = seal(
            who({"delegate": "po-agent@v1", "scope": "write_order"}),
            did({"vendor": "Frobozz Supply", "total": "1240.19"}),
            references=(ext_ref,),
            anchor=False,
            witness=False,
            ledger=ledger,
        )
        assert "references" in composition.capsule
        assert composition.capsule["references"][0]["digest"] == ext_id
        print("[3] slot-composition with references on composition: OK")
        print(f"    composition capsule_id={composition.capsule_id[:16]}…")
        print(f"    references[0].digest={composition.capsule['references'][0]['digest'][:16]}…")

        # Confirm member capsules have no references[].
        members = composition.capsule["model_attestation"]["compute_attestation"]["composed_members"]
        from capsule_emit import read_ledger
        records = {r["capsule_id"]: r for r in read_ledger(ledger)}
        for m in members:
            member_capsule = records[m["digest"]]
            assert "references" not in member_capsule
        print("    member capsules carry no references[]: OK")

    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
