# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [O16-13]: seal() cryptographically signs every capsule.

O16 audit item 13 ("Signer protocol seam") found that ``seal()`` never
touched a ``Signer`` at all -- no cryptographic signature existed over sealed
capsule content outside the opt-in checkpoint layer, and that layer's own
default signer (``witness._AutoSigner``) is ephemeral HMAC, not a persisted
asymmetric keypair. This file is the "planned" test named in the audit's
coverage table: "every sealed capsule verifiably signed by a persisted key."

Covers:
- every ``seal()``/``carry()``/``compose()`` result carries a ``signature`` +
  ``key_id`` on both the ``EmitResult`` and ``capsule`` dict
- the signature verifies against the capsule and rejects tampering
- the default key is a persisted Ed25519 keypair (survives a process
  restart -- same key_id on a second, independent signer over the same path)
- the persisted key file has restrictive permissions
- a caller-supplied ``Signer`` overrides the default
- rotation produces a binding record that verifies against the OLD key
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

from capsule_emit import LocalKeypairSigner, carry, compose, seal, verify_capsule_signature
from capsule_emit.signing import RotationRecord, resolve_signer


def test_seal_result_is_signed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    capsule = seal({"vendor": "Frobozz Supply"}, anchor=False, witness=False)

    assert capsule.signature
    assert capsule.key_id
    assert capsule.capsule["signature"] == capsule.signature
    assert capsule.capsule["key_id"] == capsule.key_id
    assert verify_capsule_signature(capsule.capsule)


def test_carry_and_compose_results_are_signed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sealed = seal({"a": 1}, anchor=False, witness=False)
    carried = carry(b'{"foreign": true}', anchor=False, witness=False)
    composed = compose([sealed, carried], anchor=False, witness=False)

    for capsule in (sealed, carried, composed):
        assert verify_capsule_signature(capsule.capsule)


def test_tampering_with_signed_content_invalidates_signature(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    capsule = seal({"amount": 5}, operator="acme-co", anchor=False, witness=False).capsule
    assert verify_capsule_signature(capsule)

    # Mutating any field the signature actually covers (everything except
    # capsule_id/chain/signature/key_id -- see verify_capsule_signature's
    # docstring) must break it.
    tampered = dict(capsule)
    tampered["operator"] = "evil-co"
    assert not verify_capsule_signature(tampered)

    wrong_sig = dict(capsule)
    wrong_sig["signature"] = "00" * 64
    assert not verify_capsule_signature(wrong_sig)

    wrong_key = dict(capsule)
    wrong_key["key_id"] = LocalKeypairSigner(tmp_path / "other.pem").key_id
    assert not verify_capsule_signature(wrong_key)


def test_capsule_id_commits_to_signature_and_key_id(tmp_path, monkeypatch):
    """capsule_id is computed AFTER signature/key_id are added, so it commits
    to them too -- stripping or swapping either changes capsule_id, which is
    exactly what lets agent_action_capsule.verify()'s digest-integrity check
    (a separate, unmodified layer) catch tampering with the signature itself."""
    from agent_action_capsule.canonical import compute_capsule_id

    monkeypatch.chdir(tmp_path)
    capsule = seal({"amount": 5}, anchor=False, witness=False).capsule
    assert capsule["capsule_id"] == compute_capsule_id(capsule)

    stripped = {k: v for k, v in capsule.items() if k not in ("signature", "key_id")}
    assert compute_capsule_id(stripped) != capsule["capsule_id"]


def test_verify_capsule_signature_never_raises_on_malformed_input():
    assert not verify_capsule_signature({})
    assert not verify_capsule_signature({"capsule_id": "abc"})
    assert not verify_capsule_signature({"capsule_id": "abc", "signature": "zz", "key_id": "zz"})


def test_default_key_is_persisted_across_process_restarts(tmp_path, monkeypatch):
    """A persisted key means the SAME identity signs across process
    restarts -- not just within one process's lifetime (the flaw named in
    the audit for the checkpoint layer's ephemeral _AutoSigner)."""
    monkeypatch.chdir(tmp_path)
    first = seal({"n": 1}, anchor=False, witness=False)

    # Simulate a fresh process: a brand-new LocalKeypairSigner instance
    # pointed at the same default key path must load the identical key,
    # not generate a new one.
    key_path = tmp_path / "ledger.jsonl.signing_key.pem"
    assert key_path.exists()
    reloaded = LocalKeypairSigner(key_path)
    assert reloaded.key_id == first.key_id

    second = seal({"n": 2}, anchor=False, witness=False)
    assert second.key_id == first.key_id


def test_default_key_file_has_restrictive_permissions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seal({"n": 1}, anchor=False, witness=False)
    key_path = tmp_path / "ledger.jsonl.signing_key.pem"
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_resolve_signer_caches_per_ledger_path(tmp_path):
    ledger = str(tmp_path / "ledger.jsonl")
    a = resolve_signer(ledger)
    b = resolve_signer(ledger)
    assert a is b


def test_custom_signer_overrides_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class StubSigner:
        key_id = "stub-key"

        def sign(self, payload: bytes) -> str:
            return "stub-signature"

    capsule = seal({"n": 1}, anchor=False, witness=False, signer=StubSigner())
    assert capsule.key_id == "stub-key"
    assert capsule.signature == "stub-signature"
    # A non-cryptographic stub signature does not verify against Ed25519 --
    # confirms verify_capsule_signature actually checks, rather than trusting
    # whatever key_id/signature strings happen to be present.
    assert not verify_capsule_signature(capsule.capsule)


def test_signing_key_path_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    custom_path = tmp_path / "custom-producer-key.pem"
    capsule = seal({"n": 1}, anchor=False, witness=False, signing_key_path=custom_path)
    assert custom_path.exists()
    assert verify_capsule_signature(capsule.capsule)


def test_signing_key_path_env_var(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_path = tmp_path / "env-producer-key.pem"
    monkeypatch.setenv("CAPSULE_SIGNING_KEY_PATH", str(env_path))
    capsule = seal({"n": 1}, anchor=False, witness=False)
    assert env_path.exists()
    assert verify_capsule_signature(capsule.capsule)


def test_rotation_binds_old_key_to_new(tmp_path):
    signer = LocalKeypairSigner(tmp_path / "rotating.pem")
    old_key_id = signer.key_id

    record = signer.rotate()

    assert isinstance(record, RotationRecord)
    assert record.old_key_id == old_key_id
    assert record.new_key_id == signer.key_id
    assert record.new_key_id != record.old_key_id

    # The binding signature is the OLD key signing the NEW key_id -- a
    # verifier who already trusts the old key can check the succession
    # without ever needing the old private key again.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    old_public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(old_key_id))
    old_public_key.verify(
        bytes.fromhex(record.binding_signature), record.new_key_id.encode("ascii")
    )


def test_rotation_persists_the_new_key(tmp_path):
    key_path = tmp_path / "rotating.pem"
    signer = LocalKeypairSigner(key_path)
    signer.rotate()
    new_key_id = signer.key_id

    reloaded = LocalKeypairSigner(key_path)
    assert reloaded.key_id == new_key_id
