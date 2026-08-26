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


def test_capsule_id_is_signer_independent(tmp_path, monkeypatch):
    """[capsule-cose-sign1] draft-04 reversal: capsule_id is a PURE content
    address again -- computed over the signature-free payload, exactly as
    pre-#94. signature/key_id are added to the dict AFTER capsule_id is
    computed and set, and are permanently excluded from its preimage (see
    capsule_emit.canonicalization), so stripping or swapping either one
    does NOT change capsule_id -- the opposite of the pre-reversal
    fold-in. Two signers over identical content now produce the SAME
    capsule_id (see test_two_signers_over_identical_content_share_one_capsule_id)."""
    from capsule_emit.canonicalization import compute_capsule_id

    monkeypatch.chdir(tmp_path)
    capsule = seal({"amount": 5}, anchor=False, witness=False).capsule
    assert capsule["capsule_id"] == compute_capsule_id(capsule)

    stripped = {k: v for k, v in capsule.items() if k not in ("signature", "key_id")}
    assert compute_capsule_id(stripped) == capsule["capsule_id"]

    swapped = dict(capsule, signature="00" * 32, key_id="11" * 32)
    assert compute_capsule_id(swapped) == capsule["capsule_id"]


def test_two_signers_over_identical_content_share_one_capsule_id(tmp_path, monkeypatch):
    """[capsule-cose-sign1] MANAGER FLAG 4(b): two signers, same content ->
    ONE shared capsule_id, TWO distinct COSE_Sign1 envelopes, order-
    independent (the content-unique-not-record-unique semantic). Within a
    single producer, behavior is identical to before the reversal (Ed25519
    is deterministic, RFC 8032) -- this is the cross-signer case the
    reversal exists for. Build ONE capsule (so content -- including its
    per-event action_id/timestamp uniqueness fields, MANAGER FLAG 4(c) --
    is held fixed) and sign its SAME capsule_id independently with two
    different keys, rather than two separate seal() calls (which would
    mint two distinct events with two distinct action_ids)."""
    from capsule_emit.signing import sign_producer_envelope

    monkeypatch.chdir(tmp_path)
    base = seal({"amount": 5}, anchor=False, witness=False).capsule
    capsule_id = base["capsule_id"]

    alice = LocalKeypairSigner(tmp_path / "alice.pem")
    bob = LocalKeypairSigner(tmp_path / "bob.pem")
    alice_envelope, alice_key_id = sign_producer_envelope(alice, capsule_id)
    bob_envelope, bob_key_id = sign_producer_envelope(bob, capsule_id)

    assert alice_key_id != bob_key_id
    assert alice_envelope != bob_envelope

    from_alice = dict(base, signature=alice_envelope, key_id=alice_key_id)
    from_bob = dict(base, signature=bob_envelope, key_id=bob_key_id)

    # Same capsule_id, unaffected by which signer's envelope is attached.
    assert from_alice["capsule_id"] == from_bob["capsule_id"] == capsule_id
    assert verify_capsule_signature(from_alice)
    assert verify_capsule_signature(from_bob)
    # Order-independent: each envelope verifies on its own, regardless of
    # which was attached/checked first.
    assert verify_capsule_signature(from_bob)
    assert verify_capsule_signature(from_alice)


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
        def sign(self, payload: bytes) -> tuple[str, str]:
            return "stub-signature", "stub-key"

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


def test_rotation_landing_between_sign_and_label_cannot_mismatch(tmp_path, monkeypatch):
    """[O16-13-signer-tuple-fix] Forces the exact race the PR #80 gate review
    flagged: a `rotate()` landing between a capsule's producer envelope being
    built and its `key_id` being read to label it. Under a hypothetical
    `sign_envelope(payload) -> bytes` + separately-read mutable `.key_id`
    attribute shape, `_emit_capsule` would set `capsule["signature"]` then
    `capsule["key_id"]` as two unsynchronized steps, so a rotation racing in
    between would mint a capsule enveloped by the OLD key but labeled with
    the NEW key_id -- an honestly-produced capsule that fails verification.
    The frozen §7d atomic `sign_envelope(bytes) -> (envelope, key_id)` return
    (see `LocalKeypairSigner.sign_envelope`, [capsule-cose-sign1]) closes the
    window the same way `sign()` always has: both the key and its `key_id`
    are read from ONE lock-protected snapshot inside the same call, so
    `_emit_capsule` can only ever see an envelope and key_id pulled from the
    same key.

    This test forces the worst-case timing directly: the signer rotates
    itself immediately after computing the (envelope, key_id) pair
    `sign_envelope()` is about to return, simulating another writer's
    `rotate()` landing in that exact window. If `_emit_capsule` re-read
    `signer.key_id` afterward (the flawed shape), it would observe the NEW
    key_id and the capsule would fail to verify. It doesn't: the capsule is
    labeled with the key_id `sign_envelope()` actually returned, paired with
    the envelope that key produced.
    """
    monkeypatch.chdir(tmp_path)
    signer = LocalKeypairSigner(tmp_path / "ledger.jsonl.signing_key.pem")
    real_sign_envelope = LocalKeypairSigner.sign_envelope

    def sign_envelope_then_rotate_underneath(self, payload):
        envelope_hex, key_id = real_sign_envelope(self, payload)
        self.rotate()  # a concurrent writer's rotation, landing right now
        return envelope_hex, key_id

    monkeypatch.setattr(LocalKeypairSigner, "sign_envelope", sign_envelope_then_rotate_underneath)

    result = seal({"n": 1}, anchor=False, witness=False, signer=signer)

    # The signer has since moved on to a new key (the simulated concurrent
    # rotation actually happened) -- but the minted capsule must still carry
    # the OLD key_id, atomically paired with the envelope the OLD key made.
    assert result.key_id != signer.key_id
    assert result.capsule["key_id"] == result.key_id
    assert verify_capsule_signature(result.capsule)
