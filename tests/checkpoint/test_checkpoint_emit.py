# SPDX-License-Identifier: Apache-2.0
"""Signed peaks-checkpoint emission: log_id-scoped signing, monotonicity,
rollback detection, and the mutant-must-fail discipline (QUEUE_PROTOCOL §7).
"""
from __future__ import annotations

import hashlib
import hmac

import pytest
from conftest import FakeLogSource, synthetic_capsule

from capsule_emit.checkpoint import CheckpointConfig, MmrLedger
from capsule_emit.checkpoint.emit import (
    DEFAULT_TS_URL,
    EXAMPLE_CONFIG_TOML,
    CheckpointError,
    RollbackError,
    due_for_checkpoint,
    emit_checkpoint,
    lag_exceeded,
    verify_checkpoint_consistency,
    verify_checkpoint_signature,
)


class HmacSigner:
    """A minimal Signer for tests: HMAC-SHA256 over a fixed secret."""

    def __init__(self, key_id: str, secret: bytes = b"test-secret"):
        self.key_id = key_id
        self._secret = secret

    def sign(self, digest_hex: str) -> str:
        return hmac.new(self._secret, digest_hex.encode("ascii"), hashlib.sha256).hexdigest()


def _mmr_with(n: int) -> MmrLedger:
    mmr = MmrLedger(FakeLogSource())
    for i in range(n):
        mmr.append(synthetic_capsule(i), consequential=False)
    return mmr


def test_emit_checkpoint_first_checkpoint_has_no_prev():
    mmr = _mmr_with(5)
    signer = HmacSigner("node-a")
    cp = emit_checkpoint(mmr, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")

    assert cp.log_id == "log-a"
    assert cp.key_id == "node-a"
    assert cp.mmr_size == mmr.size()
    assert cp.prev_size == 0
    assert cp.prev_root == ""
    assert cp.root == mmr.root().hex()
    assert verify_checkpoint_signature(cp, signer)


def test_emit_checkpoint_refuses_an_empty_mmr():
    mmr = MmrLedger(FakeLogSource())
    with pytest.raises(CheckpointError):
        emit_checkpoint(mmr, HmacSigner("node-a"), log_id="log-a")


def test_second_checkpoint_chains_to_the_first():
    mmr = _mmr_with(5)
    signer = HmacSigner("node-a")
    cp1 = emit_checkpoint(mmr, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")

    for i in range(5, 9):
        mmr.append(synthetic_capsule(i), consequential=False)
    cp2 = emit_checkpoint(mmr, signer, log_id="log-a", prev=cp1, timestamp="2026-08-21T01:00:00Z")

    assert cp2.prev_size == cp1.mmr_size
    assert cp2.prev_root == cp1.root
    assert verify_checkpoint_signature(cp2, signer)
    assert verify_checkpoint_consistency(cp1, cp2, mmr)


def test_emit_checkpoint_rejects_a_prev_from_a_different_log():
    mmr = _mmr_with(5)
    signer = HmacSigner("node-a")
    cp1 = emit_checkpoint(mmr, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")

    for i in range(5, 8):
        mmr.append(synthetic_capsule(i), consequential=False)

    with pytest.raises(CheckpointError):
        emit_checkpoint(mmr, signer, log_id="log-b", prev=cp1, timestamp="2026-08-21T01:00:00Z")


def test_emit_checkpoint_rejects_non_monotonic_size():
    mmr = _mmr_with(9)
    signer = HmacSigner("node-a")
    # cp1 pinned at the current (larger) size...
    cp1 = emit_checkpoint(mmr, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")

    # ...a second MMR that never grew past 5 leaves cannot checkpoint "after" cp1.
    stalled = _mmr_with(5)
    with pytest.raises(RollbackError):
        emit_checkpoint(stalled, signer, log_id="log-a", prev=cp1, timestamp="2026-08-21T01:00:00Z")


# -- mutant: tampered/rolled-back log must fail consistency, not just signature --


def test_verify_checkpoint_consistency_mutant_rolled_back_log_fails():
    """RED case per QUEUE_PROTOCOL §7: cp2 CLAIMS to extend cp1 (its
    prev_size/prev_root fields say so), but the log backing cp2 actually has
    DIFFERENT content at that size -- a rollback-and-rewrite. The live root
    recomputed at prev_size must not match, so verify_checkpoint_consistency
    must flip to False rather than trusting the claimed prev_root blindly."""
    mmr = _mmr_with(5)
    signer = HmacSigner("node-a")
    cp1 = emit_checkpoint(mmr, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")

    # A different log: different leaf 0..4 content (so a different root at
    # size 5), then grown past cp1's size the same way.
    diverged = MmrLedger(FakeLogSource())
    for i in range(5):
        diverged.append(synthetic_capsule(i + 1000), consequential=False)
    for i in range(5, 9):
        diverged.append(synthetic_capsule(i), consequential=False)

    cp2 = emit_checkpoint(diverged, signer, log_id="log-a", timestamp="2026-08-21T01:00:00Z")
    # Forge cp2 into falsely claiming continuity from cp1, as if the log had
    # never diverged (an attacker or a corrupted operator would do exactly
    # this to hide a rollback).
    cp2.prev_size = cp1.mmr_size
    cp2.prev_root = cp1.root

    assert not verify_checkpoint_consistency(cp1, cp2, diverged)

    # sanity: cp2 against its own true predecessor (no forged claim) passes.
    diverged_first_five = MmrLedger(FakeLogSource())
    for i in range(5):
        diverged_first_five.append(synthetic_capsule(i + 1000), consequential=False)
    real_cp1 = emit_checkpoint(diverged_first_five, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")
    real_cp2 = emit_checkpoint(diverged, signer, log_id="log-a", prev=real_cp1, timestamp="2026-08-21T01:00:00Z")
    assert verify_checkpoint_consistency(real_cp1, real_cp2, diverged)


def test_verify_checkpoint_signature_mutant_tampered_root_fails():
    mmr = _mmr_with(5)
    signer = HmacSigner("node-a")
    cp = emit_checkpoint(mmr, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")
    assert verify_checkpoint_signature(cp, signer)

    cp.root = "00" * 32
    assert not verify_checkpoint_signature(cp, signer)

    cp.root = mmr.root().hex()
    assert verify_checkpoint_signature(cp, signer)  # restored -- confirms the mutant, not a broken check

    wrong_signer = HmacSigner("node-a", secret=b"wrong-secret")
    assert not verify_checkpoint_signature(cp, wrong_signer)


def test_verify_checkpoint_consistency_mutant_wrong_log_id_fails():
    mmr = _mmr_with(5)
    signer = HmacSigner("node-a")
    cp1 = emit_checkpoint(mmr, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")
    for i in range(5, 9):
        mmr.append(synthetic_capsule(i), consequential=False)
    cp2 = emit_checkpoint(mmr, signer, log_id="log-a", prev=cp1, timestamp="2026-08-21T01:00:00Z")
    assert verify_checkpoint_consistency(cp1, cp2, mmr)

    cp2.log_id = "log-b"
    assert not verify_checkpoint_consistency(cp1, cp2, mmr)


# -- digest determinism: log_id is part of the signed/registered digest ------


def test_digest_changes_with_log_id():
    mmr = _mmr_with(5)
    signer = HmacSigner("node-a")
    cp_a = emit_checkpoint(mmr, signer, log_id="log-a", timestamp="2026-08-21T00:00:00Z")
    cp_b = emit_checkpoint(mmr, signer, log_id="log-b", timestamp="2026-08-21T00:00:00Z")
    assert cp_a.digest() != cp_b.digest()
    assert cp_a.signature != cp_b.signature  # signer covers the digest, so this must differ too


# -- config: cadence/max-lag + the commented-out witness default ------------


def test_due_for_checkpoint_and_lag_exceeded():
    cfg = CheckpointConfig(cadence_entries=100, max_lag_entries=200)
    assert not due_for_checkpoint(cfg, 99)
    assert due_for_checkpoint(cfg, 100)
    assert not lag_exceeded(cfg, 200)
    assert lag_exceeded(cfg, 201)


def test_checkpoint_config_ts_urls_empty_by_default():
    cfg = CheckpointConfig()
    assert cfg.ts_urls == []  # registration is opt-in, never assumed


def test_example_config_ships_the_witness_url_commented_out():
    assert f'# ts_urls = ["{DEFAULT_TS_URL}"]' in EXAMPLE_CONFIG_TOML
    assert f'ts_urls = ["{DEFAULT_TS_URL}"]\n' not in EXAMPLE_CONFIG_TOML.replace(
        f'# ts_urls = ["{DEFAULT_TS_URL}"]\n', ""
    )
