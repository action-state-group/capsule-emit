# SPDX-License-Identifier: Apache-2.0
"""[cll-checkpoint-cose-wire]: COSE_Sign1 + CBOR wire form for CLL
checkpoints, and -- the point of this file -- proof that the wire-carried
MMR consistency proof is a REAL extension proof, not field equality.

RED-FIRST adversarial coverage (the task's own gate): a checkpoint whose
``prev_size``/``prev_commitment`` fields match a trusted prior EXACTLY, but
whose claimed continuity is not actually backed by a real MMR extension,
must be REJECTED by :func:`verify_checkpoint_cose_offline` -- even though a
naive field-equality check (``current.prev_root == prior.root``, exactly
what ``checkpoint.emit.verify_checkpoint_consistency`` does, and exactly
what a live-reader-less stranger holding only the wire bytes cannot even
perform) would wrongly accept it. Three concrete adversarial shapes:

  1. a rewritten tail: the checkpoint claims the TRUE ``prev_commitment``
     but its actual tree, beyond ``prev_size``, is unrelated to the one
     that produced that root -- ``test_forged_continuity_with_true_prev_root_is_rejected``.
  2. a truncated/corrupted witness: a genuine proof with a sibling hash
     dropped -- ``test_truncated_consistency_proof_is_rejected``.
  3. a forked chain: a proof minted for one branch's checkpoint, replayed
     against a DIFFERENT branch's checkpoint that shares the same honest
     prior -- ``test_proof_from_one_fork_does_not_verify_another_forks_checkpoint``.
"""
from __future__ import annotations

import dataclasses

import cbor2
import pytest

from capsule_emit.checkpoint import core as mmr_core
from capsule_emit.checkpoint.cose_wire import (
    CLL_CHECKPOINT_CONTENT_TYPE,
    WIRE_KIND,
    checkpoint_to_cose,
    encode_checkpoint_claims,
    verify_checkpoint_cose_offline,
)
from capsule_emit.checkpoint.emit import CheckpointRecord
from capsule_emit.checkpoint.store import MemoryNodeStore
from capsule_emit.signing import LocalKeypairSigner

# ---------------------------------------------------------------------------
# helpers: build raw MMRs + checkpoints without the MmrLedger/LogSource
# machinery, so these tests exercise cose_wire + core directly.
# ---------------------------------------------------------------------------


def _grow(store: mmr_core.MemoryNodeStore, n: int, *, seed: int = 0) -> None:
    """Append ``n`` synthetic leaves (content derived from ``seed`` so two
    stores seeded differently produce genuinely different trees, not
    coincidentally-identical ones)."""
    import hashlib

    for i in range(n):
        body = hashlib.sha256(f"{seed}:{i}".encode()).digest()
        mmr_core.add_leaf(store, mmr_core.leaf_hash(body))


def _root_hex(store: mmr_core.MemoryNodeStore, size: int) -> str:
    return mmr_core.root_from_peaks(
        [store.node(p) for p in mmr_core.peaks(size)]
    ).hex()


def _checkpoint(
    *,
    log_id: str,
    mmr_size: int,
    root_hex: str,
    prev_size: int = 0,
    prev_root_hex: str = "",
    key_id: str,
    timestamp: str = "2026-08-26T00:00:00Z",
) -> CheckpointRecord:
    return CheckpointRecord(
        v=1,
        kind="mmr_checkpoint",
        log_id=log_id,
        mmr_size=mmr_size,
        root=root_hex,
        prev_size=prev_size,
        prev_root=prev_root_hex,
        key_id=key_id,
        timestamp=timestamp,
        signature="",
    )


@pytest.fixture
def signer(tmp_path) -> LocalKeypairSigner:
    return LocalKeypairSigner(tmp_path / "checkpoint_signing_key.pem")


# ---------------------------------------------------------------------------
# round-trip against the JSON internal form
# ---------------------------------------------------------------------------


def test_round_trip_first_checkpoint(signer: LocalKeypairSigner) -> None:
    store = MemoryNodeStore()
    _grow(store, 3, seed=1)
    cp = _checkpoint(
        log_id="log-a", mmr_size=store.size(), root_hex=_root_hex(store, store.size()), key_id=signer.key_id
    )

    cose_bytes = checkpoint_to_cose(cp, signer)
    result = verify_checkpoint_cose_offline(cose_bytes)

    assert result.ok, result.errors
    decoded = result.decoded
    assert decoded.log_id == cp.log_id
    assert decoded.mmr_size == cp.mmr_size
    assert decoded.root == cp.root
    assert decoded.prev_size == cp.prev_size == 0
    assert decoded.prev_root == cp.prev_root == ""
    assert decoded.timestamp == cp.timestamp
    assert decoded.key_id == cp.key_id
    assert decoded.consistency_proof is None

    reconstructed = decoded.to_checkpoint_record()
    assert reconstructed.log_id == cp.log_id
    assert reconstructed.mmr_size == cp.mmr_size
    assert reconstructed.root == cp.root
    assert reconstructed.prev_size == cp.prev_size
    assert reconstructed.prev_root == cp.prev_root
    assert reconstructed.key_id == cp.key_id
    assert reconstructed.timestamp == cp.timestamp
    assert reconstructed.kind == "mmr_checkpoint"  # internal convention, fixed on decode


def test_round_trip_with_real_consistency_proof(signer: LocalKeypairSigner) -> None:
    store = MemoryNodeStore()
    _grow(store, 3, seed=1)
    size_a = store.size()
    root_a = _root_hex(store, size_a)
    _grow(store, 4, seed=1)  # honest extension of the SAME tree
    size_b = store.size()
    root_b = _root_hex(store, size_b)

    proof = mmr_core.consistency_proof(store, size_a, size_b)
    cp = _checkpoint(
        log_id="log-a",
        mmr_size=size_b,
        root_hex=root_b,
        prev_size=size_a,
        prev_root_hex=root_a,
        key_id=signer.key_id,
    )

    cose_bytes = checkpoint_to_cose(cp, signer, consistency_proof=proof)
    result = verify_checkpoint_cose_offline(cose_bytes)

    assert result.ok, result.errors
    decoded = result.decoded
    assert decoded.consistency_proof is not None
    assert decoded.consistency_proof.size_a == size_a
    assert decoded.consistency_proof.size_b == size_b
    assert decoded.consistency_proof.old_peaks == proof.old_peaks
    assert decoded.consistency_proof.new_peaks == proof.new_peaks
    assert decoded.consistency_proof.witness == proof.witness

    # And the decoded proof is independently valid too (not just structurally equal).
    assert mmr_core.verify_consistency(
        bytes.fromhex(root_a), size_a, bytes.fromhex(root_b), size_b, decoded.consistency_proof
    )


# ---------------------------------------------------------------------------
# encode-side guards: refuse to serialize a continuity claim field-equality
# alone would be left to back
# ---------------------------------------------------------------------------


def test_checkpoint_to_cose_refuses_prior_without_consistency_proof(signer: LocalKeypairSigner) -> None:
    cp = _checkpoint(
        log_id="log-a", mmr_size=7, root_hex="ab" * 32, prev_size=3, prev_root_hex="cd" * 32,
        key_id=signer.key_id,
    )
    with pytest.raises(ValueError, match="consistency_proof"):
        checkpoint_to_cose(cp, signer)


def test_checkpoint_to_cose_refuses_consistency_proof_on_first_checkpoint(
    signer: LocalKeypairSigner,
) -> None:
    store = MemoryNodeStore()
    _grow(store, 3, seed=1)
    cp = _checkpoint(
        log_id="log-a", mmr_size=store.size(), root_hex=_root_hex(store, store.size()), key_id=signer.key_id
    )
    bogus_proof = mmr_core.ConsistencyProof(1, "consistency", 0, store.size(), (), (), ())
    with pytest.raises(ValueError, match="prev_size == 0"):
        checkpoint_to_cose(cp, signer, consistency_proof=bogus_proof)


def test_checkpoint_to_cose_requires_a_cose_capable_signer() -> None:
    class _NarrowSigner:
        """Shaped like ``checkpoint.emit.Signer`` -- bare hex sign, no COSE."""

        key_id = "aa" * 32

        def sign(self, digest_hex: str) -> str:
            return "00" * 64

    store = MemoryNodeStore()
    _grow(store, 2, seed=1)
    cp = _checkpoint(
        log_id="log-a", mmr_size=store.size(), root_hex=_root_hex(store, store.size()), key_id="aa" * 32
    )
    with pytest.raises(TypeError, match="sign_cose_statement"):
        checkpoint_to_cose(cp, _NarrowSigner())


# ---------------------------------------------------------------------------
# tamper / malformed-signature rejection
# ---------------------------------------------------------------------------


def test_tampered_signature_bytes_are_rejected(signer: LocalKeypairSigner) -> None:
    store = MemoryNodeStore()
    _grow(store, 2, seed=1)
    cp = _checkpoint(
        log_id="log-a", mmr_size=store.size(), root_hex=_root_hex(store, store.size()), key_id=signer.key_id
    )
    cose_bytes = bytearray(checkpoint_to_cose(cp, signer))
    cose_bytes[-1] ^= 0xFF  # last byte of the CBOR array is inside the signature bstr

    result = verify_checkpoint_cose_offline(bytes(cose_bytes))
    assert not result.ok
    assert result.decoded is None


# ---------------------------------------------------------------------------
# RED-FIRST: real consistency proof rejects what field equality would accept
# ---------------------------------------------------------------------------


def test_forged_continuity_with_true_prev_root_is_rejected(signer: LocalKeypairSigner) -> None:
    """The core adversarial case: an attacker who holds the real signing key
    presents a checkpoint whose ``prev_commitment`` is the TRUE, honestly-
    copied prior root -- so field equality (``current.prev_root ==
    prior.root``) passes trivially -- but whose actual tail (everything
    from ``prev_size`` onward) belongs to a wholly different, unrelated
    tree. A consistency proof honestly built from the attacker's OWN
    (unrelated) tree must fail to verify against the true prior root, and
    :func:`verify_checkpoint_cose_offline` must reject it.
    """
    honest = MemoryNodeStore()
    _grow(honest, 3, seed="honest")
    size_a = honest.size()
    root_a = _root_hex(honest, size_a)  # the TRUE prior root -- what a verifier trusts

    # Attacker's tree: same size_a, but a DIFFERENT prefix (different seed)
    # -- not an extension of `honest`, a wholesale rewrite from position 0.
    forged = MemoryNodeStore()
    _grow(forged, size_a, seed="forged-prefix")
    assert _root_hex(forged, size_a) != root_a  # confirms this really is a different tree
    _grow(forged, 4, seed="forged-tail")
    size_b = forged.size()
    root_b = _root_hex(forged, size_b)

    # The attacker mints a structurally well-formed consistency proof from
    # THEIR OWN tree -- consistency_proof() never checks the old peaks
    # against any externally-trusted root, it just reads back whatever is
    # stored at those positions in the reader it's given.
    forged_proof = mmr_core.consistency_proof(forged, size_a, size_b)

    # Sanity: naive field equality (no live reader) would accept this --
    # it's just comparing two strings the attacker fully controls.
    cp = _checkpoint(
        log_id="log-a",
        mmr_size=size_b,
        root_hex=root_b,
        prev_size=size_a,
        prev_root_hex=root_a,  # the TRUE root, honestly copied
        key_id=signer.key_id,
    )
    assert cp.prev_root == root_a  # field-equality check would pass

    cose_bytes = checkpoint_to_cose(cp, signer, consistency_proof=forged_proof)
    result = verify_checkpoint_cose_offline(cose_bytes)

    assert not result.ok
    assert result.decoded is None
    assert any("consistency proof" in e for e in result.errors)


def test_truncated_consistency_proof_is_rejected(signer: LocalKeypairSigner) -> None:
    """A genuine proof with one sibling hash dropped from its witness path
    -- a truncated/corrupted proof -- must be rejected, not silently
    accepted with a shorter path."""
    store = MemoryNodeStore()
    _grow(store, 3, seed=1)
    size_a = store.size()
    root_a = _root_hex(store, size_a)
    _grow(store, 5, seed=1)
    size_b = store.size()
    root_b = _root_hex(store, size_b)

    proof = mmr_core.consistency_proof(store, size_a, size_b)
    assert any(len(w) > 0 for w in proof.witness), "test needs a peak with a non-empty witness path"
    truncated_witness = tuple(w[:-1] if w else w for w in proof.witness)
    truncated_proof = dataclasses.replace(proof, witness=truncated_witness)
    assert truncated_proof != proof

    cp = _checkpoint(
        log_id="log-a",
        mmr_size=size_b,
        root_hex=root_b,
        prev_size=size_a,
        prev_root_hex=root_a,
        key_id=signer.key_id,
    )
    cose_bytes = checkpoint_to_cose(cp, signer, consistency_proof=truncated_proof)
    result = verify_checkpoint_cose_offline(cose_bytes)

    assert not result.ok
    assert result.decoded is None


def test_proof_from_one_fork_does_not_verify_another_forks_checkpoint(
    signer: LocalKeypairSigner,
) -> None:
    """Two genuinely divergent continuations of the SAME true prior (an
    honest fork -- both branches individually produce a valid consistency
    proof against their own tail). A proof minted for branch A must NOT
    verify against branch B's checkpoint, even though both branches share
    the identical, true ``prev_commitment``.

    This is deliberately NOT a claim that the wire form detects forks in
    general (verify_checkpoint_cose_offline's docstring is explicit: anti-
    REWRITE, not anti-FORK/equivocation -- that needs an online witness).
    It only shows that a proof cannot be replayed across branches it
    wasn't built for -- pairing integrity, not fork detection.
    """
    common = MemoryNodeStore()
    _grow(common, 3, seed="common")
    size_a = common.size()
    root_a = _root_hex(common, size_a)

    branch_a = MemoryNodeStore()
    branch_a._nodes = list(common._nodes)
    _grow(branch_a, 3, seed="branch-a")
    size_b = branch_a.size()
    root_b_a = _root_hex(branch_a, size_b)

    branch_b = MemoryNodeStore()
    branch_b._nodes = list(common._nodes)
    _grow(branch_b, 3, seed="branch-b")
    assert branch_b.size() == size_b
    root_b_b = _root_hex(branch_b, size_b)
    assert root_b_a != root_b_b  # genuinely divergent branches

    proof_a = mmr_core.consistency_proof(branch_a, size_a, size_b)
    proof_b = mmr_core.consistency_proof(branch_b, size_a, size_b)

    # Each branch's own proof verifies against its own checkpoint (honest fork).
    assert mmr_core.verify_consistency(
        bytes.fromhex(root_a), size_a, bytes.fromhex(root_b_a), size_b, proof_a
    )
    assert mmr_core.verify_consistency(
        bytes.fromhex(root_a), size_a, bytes.fromhex(root_b_b), size_b, proof_b
    )

    # branch B's checkpoint, paired with branch A's proof -- mismatched pairing.
    cp_b_with_proof_a = _checkpoint(
        log_id="log-a",
        mmr_size=size_b,
        root_hex=root_b_b,
        prev_size=size_a,
        prev_root_hex=root_a,
        key_id=signer.key_id,
    )
    cose_bytes = checkpoint_to_cose(cp_b_with_proof_a, signer, consistency_proof=proof_a)
    result = verify_checkpoint_cose_offline(cose_bytes)

    assert not result.ok
    assert result.decoded is None


# ---------------------------------------------------------------------------
# claims-map hygiene
# ---------------------------------------------------------------------------


def test_encode_checkpoint_claims_uses_id_spec_field_names(signer: LocalKeypairSigner) -> None:
    store = MemoryNodeStore()
    _grow(store, 2, seed=1)
    cp = _checkpoint(
        log_id="log-a", mmr_size=store.size(), root_hex=_root_hex(store, store.size()), key_id=signer.key_id
    )
    claims = encode_checkpoint_claims(cp)
    assert claims["kind"] == WIRE_KIND == "cll-checkpoint"
    assert claims["log_size"] == cp.mmr_size
    assert claims["commitment"] == bytes.fromhex(cp.root)
    assert claims["prev_size"] == cp.prev_size
    assert claims["prev_commitment"] == b""
    assert claims["issued_at"] == cp.timestamp
    assert "log_id" not in claims  # moved to the signed CWT `iss` header, not a claim
    assert "key_id" not in claims  # moved to the COSE `kid` header, not a claim
    assert "signature" not in claims  # superseded by the envelope's own signature


def test_content_type_is_cbor_shaped(signer: LocalKeypairSigner) -> None:
    store = MemoryNodeStore()
    _grow(store, 2, seed=1)
    cp = _checkpoint(
        log_id="log-a", mmr_size=store.size(), root_hex=_root_hex(store, store.size()), key_id=signer.key_id
    )
    cose_bytes = checkpoint_to_cose(cp, signer)
    assert CLL_CHECKPOINT_CONTENT_TYPE == "application/cll-checkpoint+cbor"
    # payload really is CBOR encoding the claims map, not JSON or anything else
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from scitt_cose.statement import parse_signed_statement

    pubkey_pem = Ed25519PublicKey.from_public_bytes(bytes.fromhex(signer.key_id)).public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    parsed = parse_signed_statement(cose_bytes, public_key_pem=pubkey_pem)
    assert parsed["signature_verified"]
    assert parsed["content_type"] == CLL_CHECKPOINT_CONTENT_TYPE
    assert parsed["issuer"] == cp.log_id
    assert parsed["subject"] == f"{cp.log_id}#{cp.mmr_size}"
    claims = cbor2.loads(parsed["payload"])
    assert claims["kind"] == "cll-checkpoint"
