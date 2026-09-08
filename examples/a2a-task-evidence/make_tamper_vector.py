#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the tamper-negative vector from the committed positive vector.

Flips one byte in the presented Task Evidence payload while leaving the
claimed ``evidenceId``, producer envelope, checkpoint, inclusion proof, and
witness receipt exactly as originally produced -- simulating a party that
substitutes a different payload behind an otherwise-untouched reference,
the way a dishonest disclosure or a compromised storage layer might. Per
the task brief: ``contentBinding`` must FAIL while
``producerSignature``/``localInclusion``/``checkpointSignature``/
``externalRegistration`` are unaffected, because none of those checks
recompute anything from the tampered payload -- they check the signature,
checkpoint, and receipt against the UNCHANGED claimed ``evidenceId`` and
capsule.

Run after ``run_demo.py`` (or against the committed
``vectors/positive_vector.json``):

    python examples/a2a-task-evidence/make_tamper_vector.py
"""
from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from verifier import ObservedTaskContext, TrustConfig, verify_evidence_bundle  # noqa: E402


def _sep(title: str) -> None:
    print("\n" + "=" * 72 + f"\n  {title}\n" + "=" * 72)


def main() -> None:
    positive_path = _ROOT / "vectors" / "positive_vector.json"
    positive = json.loads(positive_path.read_text())

    tampered_bundle = copy.deepcopy(positive["evidenceBundle"])
    original_commitment = tampered_bundle["taskEvidencePayload"]["transcript"][0]["partCommitment"]
    # Flip the first hex character -- a one-byte-equivalent mutation of the
    # committed digest field itself, so it is unambiguous which bit moved.
    flipped_char = "1" if original_commitment[0] == "0" else "0"
    tampered_commitment = flipped_char + original_commitment[1:]
    tampered_bundle["taskEvidencePayload"]["transcript"][0]["partCommitment"] = tampered_commitment
    assert tampered_commitment != original_commitment

    # evidenceId, capsule, and manifest are DELIBERATELY left byte-for-byte
    # as originally produced -- the tamper is only in the presented payload.
    trust_cfg = positive["trustConfig"]
    trust = TrustConfig(
        ts_pubkeys={url: base64.b64decode(b64) for url, b64 in trust_cfg["tsPubkeysB64"].items()},
        authorized_signers={k: frozenset(v) for k, v in trust_cfg["authorizedSigners"].items()},
    )
    final_task = positive["finalTask"]
    payload = positive["evidenceBundle"]["taskEvidencePayload"]
    observed = ObservedTaskContext(
        authority=payload["authority"],
        task_id=final_task["id"],
        context_id=final_task["contextId"],
        terminal_state=final_task["status"]["state"],
        non_evidence_artifact_ids=frozenset(a["artifactId"] for a in payload["nonEvidenceArtifacts"]),
    )

    _sep("Verifying the TAMPERED bundle (§13 properties)")
    verification = verify_evidence_bundle(tampered_bundle, observed_task=observed, trust=trust)
    result_dict = verification.to_dict()
    for name, prop in result_dict.items():
        print(f"  {name:26s} {prop['state']:12s} {prop['detail']}")

    assert result_dict["contentBinding"]["state"] == "FAIL", "tamper vector must FAIL contentBinding"
    for unaffected in ("producerSignature", "localInclusion", "checkpointSignature", "externalRegistration"):
        original_state = positive["verification"][unaffected]["state"]
        assert result_dict[unaffected]["state"] == original_state, (
            f"{unaffected} changed from {original_state!r} to {result_dict[unaffected]['state']!r} "
            "-- the tamper must be isolated to contentBinding"
        )

    vector = {
        "classification": "tamper-negative (derived from vectors/positive_vector.json)",
        "tamper": {
            "field": "evidenceBundle.taskEvidencePayload.transcript[0].partCommitment",
            "original": original_commitment,
            "tampered": tampered_commitment,
            "evidenceIdAndManifestUnchanged": True,
        },
        "evidenceBundle": tampered_bundle,
        "verification": result_dict,
    }
    out_path = _ROOT / "vectors" / "tamper_negative_vector.json"
    out_path.write_text(json.dumps(vector, indent=2, sort_keys=True) + "\n")
    _sep(f"Wrote tamper-negative vector -> {out_path}")


if __name__ == "__main__":
    main()
