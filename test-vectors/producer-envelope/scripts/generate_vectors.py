# SPDX-License-Identifier: Apache-2.0
"""Generate capsule-emit's own producer-envelope conformance vectors.

Deterministic: a fixed test seed (never a production signing key) drives
``capsule_emit.signing.LocalKeypairSigner`` end to end through the SAME
code path ``seal()`` uses (``capsule_emit.signing.sign_producer_envelope``,
which reuses ``scitt_cose.cose_sign1.sign_sign1`` for the COSE/CBOR
machinery) -- these are not hand-built envelopes, they are exactly what a
real ``seal()`` call produces, with the key material pinned for
reproducibility.

Cut against ``agent-action-capsule`` main (post #74/#75, draft-04 identity +
COSE_Sign1 producer envelope) as the reference profile: alg=EdDSA(-8),
content_type=application/agent-action-capsule-id, kid=raw 32-byte Ed25519
public key, empty unprotected map, payload=raw 32-byte capsule_id digest.

Regenerate from the repository root:
    python test-vectors/producer-envelope/scripts/generate_vectors.py

Cross-verify against the Go reference verifier (agent-action-capsule's
``go/envelope`` package) with:
    go run test-vectors/producer-envelope/scripts/verify_with_go.go \\
        test-vectors/producer-envelope/valid/capsule_id.txt \\
        test-vectors/producer-envelope/valid/envelope.cose
(see that script's header for the module replace-directive setup it needs).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1]

#: Fixed test seed -- public test material, never a production signing key
#: (same convention as agent-action-capsule's own vector generator).
_KEY_PATH = OUT / "_seed.signing_key.pem"


def _fixed_signer():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    from capsule_emit.signing import LocalKeypairSigner

    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    _KEY_PATH.write_bytes(pem)
    return LocalKeypairSigner(_KEY_PATH)


def main() -> None:
    from capsule_emit.canonicalization import compute_capsule_id
    from capsule_emit.signing import sign_producer_envelope

    OUT.mkdir(parents=True, exist_ok=True)
    signer = _fixed_signer()

    # A minimal, deterministic capsule body -- the exact field set does not
    # matter to the producer-envelope profile (it only ever signs the raw
    # capsule_id digest), so this is a small fixed fixture, not a full
    # seal() call (which mints a fresh UUID/timestamp on every run).
    body = {
        "spec_version": "draft-mih-scitt-agent-action-capsule-04",
        "format_version": "4",
        "action_id": "test_action/00000000-0000-0000-0000-000000000000",
        "action_type": "decide",
        "operator": "test-org",
        "developer": "test-agent@v1",
        "timestamp": "2026-08-24T00:00:00Z",
        "disposition": {
            "decision": "accept", "approver": "policy",
            "human_disposed": False, "verdict_class": "executed",
        },
        "chain": {"parent_capsule_id": "20" * 32, "relation": "confirms"},
        "canonicalization_id": "jcs",
    }
    body["capsule_id"] = compute_capsule_id(body)
    envelope_hex, key_id = sign_producer_envelope(signer, body["capsule_id"])
    body["signature"] = envelope_hex
    body["key_id"] = key_id

    envelope = bytes.fromhex(envelope_hex)
    valid_dir = OUT / "valid"
    valid_dir.mkdir(parents=True, exist_ok=True)
    (valid_dir / "capsule_id.txt").write_text(body["capsule_id"] + "\n", encoding="ascii")
    (valid_dir / "envelope.cose").write_bytes(envelope)
    (valid_dir / "capsule.json").write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected = {"ok": True, "finding_codes": [], "public_key_hex": key_id}
    (valid_dir / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")

    checksums = []
    for f in sorted(valid_dir.iterdir()):
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        checksums.append(f"{digest}  {f.relative_to(OUT)}")
    (OUT / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")

    manifest = {
        "format_version": "1",
        "profile": "draft-mih-scitt-agent-action-capsule-04#producer-envelope",
        "generator": "capsule_emit.signing.sign_producer_envelope (LocalKeypairSigner.sign_envelope, reuses scitt_cose.cose_sign1.sign_sign1)",
        "cases": [{"name": "valid", "description": "seal()'s real code path over a fixed capsule body + fixed test key"}],
    }
    (OUT / "vectors.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"capsule_id: {body['capsule_id']}")
    print(f"key_id:     {key_id}")
    print(f"envelope:   {len(envelope)} bytes -> {valid_dir / 'envelope.cose'}")


if __name__ == "__main__":
    main()
