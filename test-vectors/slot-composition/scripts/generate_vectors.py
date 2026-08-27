# SPDX-License-Identifier: Apache-2.0
"""Generate capsule-emit's slot-form / carry-form conformance vectors.

O8 acceptance (`_work/dev-surface-v4-operational-2026-08-24.md`): "the
carry-form and slot-form produce byte-identical records" -- this is the
byte-level proof for capsule-producer-go's (Ethan's repo) cross-language
conformance target, generated through the SAME code paths
``capsule_emit.surface.seal``/``received``/``who``/``can``/``did`` use, with
key material, uuids, and timestamps pinned for reproducibility (never a
production signing key -- same convention as
``test-vectors/producer-envelope/``).

Two things this vector proves:
1. **Standalone carry-form** (`received(bytes, type=...)`) and **the same
   Capsule referenced from inside a slot wrapper**
   (`can(that_same_capsule)`) are the identical object, byte for byte --
   composing never re-mints a member it already holds.
2. **The composition capsule's member refs** carry a `slot` annotation
   alongside the CPB typed digest ref (`{type, digest_alg, digest, slot}`)
   -- new relative to the v3 `compose()` shape (which had no `slot` field).

Regenerate from the repository root:
    python test-vectors/slot-composition/scripts/generate_vectors.py
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from unittest import mock

OUT = Path(__file__).resolve().parents[1]

#: Fixed test seed -- public test material, never a production signing key.
_KEY_PATH = OUT / "_seed.signing_key.pem"
_LEDGER_PATH = OUT / "_seed.ledger.jsonl"

_FIXED_TIMESTAMP = "2026-08-27T00:00:00Z"
#: One fixed uuid4 per _emit_capsule call this script makes, in call order:
#: who-member, can-member (the received() carry), did-member, composition.
_FIXED_UUIDS = [
    uuid.UUID(int=1),
    uuid.UUID(int=2),
    uuid.UUID(int=3),
    uuid.UUID(int=4),
]


def _fixed_signer():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    from capsule_emit.signing import LocalKeypairSigner

    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    _KEY_PATH.write_bytes(pem)
    return LocalKeypairSigner(_KEY_PATH)


def main() -> None:
    import importlib

    from capsule_emit.surface import can, did, received, seal, who

    base_emit_module = importlib.import_module("agent_action_capsule.emit")

    OUT.mkdir(parents=True, exist_ok=True)
    if _LEDGER_PATH.exists():
        _LEDGER_PATH.unlink()
    signer = _fixed_signer()

    kwargs = {
        "operator": "test-org",
        "developer": "test-agent@v1",
        "anchor": False,
        "witness": False,
        "ledger": _LEDGER_PATH,
        "signer": signer,
    }

    mandate_jws = b'{"iss": "acme-mandates", "sub": "po-agent@v1"}'

    uuid_iter = iter(_FIXED_UUIDS)
    with (
        mock.patch.object(base_emit_module.uuid, "uuid4", side_effect=lambda: next(uuid_iter)),
        mock.patch.object(base_emit_module, "_utc_now", return_value=_FIXED_TIMESTAMP),
    ):
        # The carry-form: received() standalone.
        mandate = received(mandate_jws, type="machine-mandate", **kwargs)

        # The slot-form: composes a freshly-minted who()/did() member with
        # the SAME mandate Capsule object referenced (not re-minted) via can().
        action = seal(
            who({"delegate": "po-agent@v1", "scope": "write_order"}),
            can(mandate),
            did({"vendor": "Frobozz Supply", "total": "1240.19"}),
            **kwargs,
        )

    members = action.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    can_ref = next(m for m in members if m["slot"] == "can")
    assert can_ref["digest"] == mandate.capsule_id, "O8 violated: can(mandate) re-minted instead of referencing"

    valid_dir = OUT / "valid"
    valid_dir.mkdir(parents=True, exist_ok=True)
    (valid_dir / "carry_form.json").write_text(
        json.dumps(mandate.capsule, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (valid_dir / "slot_form_composition.json").write_text(
        json.dumps(action.capsule, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    expected = {
        "carry_form_capsule_id": mandate.capsule_id,
        "slot_form_composition_capsule_id": action.capsule_id,
        "composed_members": members,
        "byte_identical_check": "composed_members[slot=can].digest == carry_form_capsule_id",
    }
    (valid_dir / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")

    checksums = []
    for f in sorted(valid_dir.iterdir()):
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        checksums.append(f"{digest}  {f.relative_to(OUT)}")
    (OUT / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")

    manifest = {
        "format_version": "1",
        "profile": "dev-surface-v4-2026-08-24#slot-composition (O8)",
        "generator": "capsule_emit.surface.seal/received/who/can/did over a fixed test key + pinned uuid4/timestamp",
        "cases": [
            {
                "name": "carry_form",
                "description": "received(mandate_jws, type='machine-mandate') standalone",
            },
            {
                "name": "slot_form_composition",
                "description": (
                    "seal(who(...), can(<the carry_form Capsule>), did(...)) -- "
                    "the can-slot member ref must digest-match carry_form's capsule_id exactly"
                ),
            },
        ],
    }
    (OUT / "vectors.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    _KEY_PATH.unlink(missing_ok=True)
    _LEDGER_PATH.unlink(missing_ok=True)
    _LEDGER_PATH.with_name(_LEDGER_PATH.name + ".lock").unlink(missing_ok=True)

    print(f"carry_form capsule_id:            {mandate.capsule_id}")
    print(f"slot_form_composition capsule_id: {action.capsule_id}")
    print(f"can-slot member digest:           {can_ref['digest']}  (byte-identical: {can_ref['digest'] == mandate.capsule_id})")


if __name__ == "__main__":
    main()
