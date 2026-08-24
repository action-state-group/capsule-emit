# SPDX-License-Identifier: Apache-2.0
"""Signed peaks-checkpoint emission: log_id-scoped signing, monotonicity,
rollback detection, and the mutant-must-fail discipline (QUEUE_PROTOCOL §7).
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from conftest import FakeLogSource, synthetic_capsule

from capsule_emit.checkpoint import CheckpointConfig, MmrLedger
from capsule_emit.checkpoint.emit import (
    DEFAULT_TS_URL,
    EXAMPLE_CONFIG_TOML,
    CheckpointError,
    Grade,
    RollbackError,
    WitnessRecord,
    due_for_checkpoint,
    emit_checkpoint,
    lag_exceeded,
    verify_checkpoint_consistency,
    verify_checkpoint_signature,
    verify_witness_stamp_offline,
)


def _test_ts_private_key_pem() -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    return Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


_TEST_TS_PRIVATE_KEY_PEM = _test_ts_private_key_pem()


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


# -- grade: the self-attested -> witnessed ladder transition (O16 item 11) --


def _genuine_witness_record(cp, ts_url: str = "https://witness.example") -> WitnessRecord:
    """A ``WitnessRecord`` bound to ``cp`` with a real, structurally valid
    COSE Receipt -- what ``grade()``'s stamp-authenticity check
    ([stamp-authenticity-on-read-not-presence]) requires. A hand-fabricated
    ``entry_hash``/``receipt_b64`` (this helper's pre-fix shape) is now
    exactly the file-forger attack ``grade()`` must reject -- see
    ``test_grade_rejects_a_hand_fabricated_witness_record`` below."""
    import base64

    from scitt_cose import build_receipt

    entry_hash = hashlib.sha256(bytes.fromhex(cp.digest())).hexdigest()
    receipt_bytes = build_receipt(
        leaf_entry_hex=entry_hash,
        leaf_index=0,
        tree_entries_hex=[entry_hash],
        alg="EdDSA",
        log_private_key_pem=_TEST_TS_PRIVATE_KEY_PEM,
    )
    return WitnessRecord(
        ts_url=ts_url,
        entry_hash=entry_hash,
        receipt_b64=base64.b64encode(receipt_bytes).decode(),
        leaf_index=0,
        tree_size=1,
    )


def _forged_witness_record(ts_url: str = "https://attacker.example") -> WitnessRecord:
    """A hand-fabricated stamp: no real Transparency Service ever
    contacted, ``entry_hash``/``receipt_b64`` invented -- exactly what a
    file-level forger writes directly into a ledger."""
    return WitnessRecord(
        ts_url=ts_url,
        entry_hash="ab" * 32,
        receipt_b64="c3R1Yg==",
        leaf_index=0,
        tree_size=1,
    )


def test_grade_is_self_attested_with_no_witnesses():
    mmr = _mmr_with(5)
    cp = emit_checkpoint(mmr, HmacSigner("node-a"), log_id="log-a",
                          timestamp="2026-08-21T00:00:00Z")
    assert cp.witnesses == []
    assert cp.grade() == Grade.SELF_ATTESTED


def test_grade_is_witnessed_once_a_single_stamp_lands():
    mmr = _mmr_with(5)
    cp = emit_checkpoint(mmr, HmacSigner("node-a"), log_id="log-a",
                          timestamp="2026-08-21T00:00:00Z")
    cp.witnesses.append(_genuine_witness_record(cp))
    assert cp.grade() == Grade.WITNESSED


def test_grade_is_any_of_not_all_of_across_multiple_witnesses():
    # Multi-witness any-of (frozen surface §2a.3): the first stamp already
    # flips the grade; a second, independently-operated witness compounds
    # independence, it does not gate the grade back down or up further.
    mmr = _mmr_with(5)
    cp = emit_checkpoint(mmr, HmacSigner("node-a"), log_id="log-a",
                          timestamp="2026-08-21T00:00:00Z")
    cp.witnesses.append(_genuine_witness_record(cp, "https://witness-a.example"))
    assert cp.grade() == Grade.WITNESSED
    cp.witnesses.append(_genuine_witness_record(cp, "https://witness-b.example"))
    assert cp.grade() == Grade.WITNESSED


def test_grade_rejects_a_hand_fabricated_witness_record():
    """[stamp-authenticity-on-read-not-presence]: a file-level forger who
    appends a fabricated ``WitnessRecord`` (no real TS ever contacted) does
    NOT launder a checkpoint to WITNESSED -- presence in ``witnesses`` alone
    no longer counts."""
    mmr = _mmr_with(5)
    cp = emit_checkpoint(mmr, HmacSigner("node-a"), log_id="log-a",
                          timestamp="2026-08-21T00:00:00Z")
    cp.witnesses.append(_forged_witness_record())
    assert cp.grade() == Grade.SELF_ATTESTED


def test_grade_any_of_one_genuine_one_forged_still_witnessed():
    """Any-of holds for authenticity too: one genuine stamp is enough even
    alongside a forged one appended into the same list."""
    mmr = _mmr_with(5)
    cp = emit_checkpoint(mmr, HmacSigner("node-a"), log_id="log-a",
                          timestamp="2026-08-21T00:00:00Z")
    cp.witnesses.append(_genuine_witness_record(cp, "https://witness-real.example"))
    cp.witnesses.append(_forged_witness_record())
    assert cp.grade() == Grade.WITNESSED


# -- verify_witness_stamp_offline: each sub-check in isolation --------------


def _cp_for_stamp_tests():
    mmr = _mmr_with(5)
    return emit_checkpoint(mmr, HmacSigner("node-a"), log_id="log-a",
                            timestamp="2026-08-21T00:00:00Z")


def test_verify_witness_stamp_offline_rejects_garbage_receipt_b64():
    # Correct entry_hash (so the binding check passes and the base64 stage
    # is what's actually isolated) but garbage receipt bytes.
    cp = _cp_for_stamp_tests()
    entry_hash = hashlib.sha256(bytes.fromhex(cp.digest())).hexdigest()
    forged = WitnessRecord(
        ts_url="https://attacker.example", entry_hash=entry_hash,
        receipt_b64="forged", leaf_index=0, tree_size=1,
    )
    ok, errors = verify_witness_stamp_offline(cp, forged)
    assert ok is False
    assert any("base64" in e for e in errors)


def test_verify_witness_stamp_offline_rejects_entry_hash_not_bound_to_this_checkpoint():
    """A stamp genuinely built for a DIFFERENT checkpoint (real COSE bytes,
    real entry_hash -- just not THIS checkpoint's) must fail: replay/reuse
    across checkpoints is exactly what the binding check exists to catch."""
    cp = _cp_for_stamp_tests()
    other_cp = _cp_for_stamp_tests()
    other_cp.mmr_size += 1  # force a different digest() from cp
    replayed = _genuine_witness_record(other_cp)
    ok, errors = verify_witness_stamp_offline(cp, replayed)
    assert ok is False
    assert any("not bound to this checkpoint" in e for e in errors)


def test_verify_witness_stamp_offline_accepts_genuine_receipt_without_pubkey():
    cp = _cp_for_stamp_tests()
    genuine = _genuine_witness_record(cp)
    ok, errors = verify_witness_stamp_offline(cp, genuine)
    assert ok is True
    assert errors == []


def test_verify_witness_stamp_offline_pubkey_pinned_accepts_correct_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key

    cp = _cp_for_stamp_tests()
    genuine = _genuine_witness_record(cp)
    correct_pubkey_pem = (
        load_pem_private_key(_TEST_TS_PRIVATE_KEY_PEM, password=None)
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    ok, errors = verify_witness_stamp_offline(cp, genuine, ts_pubkey_pem=correct_pubkey_pem)
    assert ok is True
    assert errors == []

    wrong_pubkey_pem = Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    ok2, errors2 = verify_witness_stamp_offline(cp, genuine, ts_pubkey_pem=wrong_pubkey_pem)
    assert ok2 is False
    assert errors2


# -- config: cadence/max-lag + the commented-out witness default ------------


def test_due_for_checkpoint_and_lag_exceeded():
    cfg = CheckpointConfig(cadence_entries=100, max_lag_entries=200)
    assert not due_for_checkpoint(cfg, 99)
    assert due_for_checkpoint(cfg, 100)
    assert not lag_exceeded(cfg, 200)
    assert lag_exceeded(cfg, 201)


# -- O16 audit item 5: the age-based cadence leg (+ idle-silence guard) -----


def test_checkpoint_config_defaults_cadence_seconds_to_15_minutes():
    cfg = CheckpointConfig()
    assert cfg.cadence_seconds == 900


def test_due_for_checkpoint_age_leg_fires_before_entry_count_cadence():
    """"100 entries or 15 minutes, whichever first" -- the age leg alone,
    with the entry count nowhere near cadence_entries, must still come due."""
    cfg = CheckpointConfig(cadence_entries=100, cadence_seconds=900)
    assert not due_for_checkpoint(cfg, 3, seconds_since_last=899)
    assert due_for_checkpoint(cfg, 3, seconds_since_last=900)


def test_due_for_checkpoint_entry_count_leg_still_fires_before_age_cadence():
    cfg = CheckpointConfig(cadence_entries=100, cadence_seconds=900)
    assert due_for_checkpoint(cfg, 100, seconds_since_last=1)


def test_due_for_checkpoint_age_leg_never_fires_with_zero_unwitnessed_entries():
    """The idle-silence guarantee at the function level: an idle log
    (entries_since_last == 0) must never come due on age alone, no matter
    how much time has elapsed -- never a heartbeat."""
    cfg = CheckpointConfig(cadence_entries=100, cadence_seconds=900)
    assert not due_for_checkpoint(cfg, 0, seconds_since_last=10_000_000)


def test_due_for_checkpoint_omitting_seconds_since_last_is_entry_count_only():
    """Back-compat: a caller that never passes seconds_since_last keeps the
    pre-item-5 entry-count-only behavior exactly."""
    cfg = CheckpointConfig(cadence_entries=100, cadence_seconds=1)
    assert not due_for_checkpoint(cfg, 99)
    assert due_for_checkpoint(cfg, 100)


def test_example_config_documents_cadence_seconds():
    assert "cadence_seconds = 900" in EXAMPLE_CONFIG_TOML


def test_checkpoint_config_to_dict_from_dict_roundtrips_cadence_seconds():
    cfg = CheckpointConfig(cadence_entries=50, cadence_seconds=60, max_lag_entries=75)
    restored = CheckpointConfig.from_dict(cfg.to_dict())
    assert restored == cfg


def test_checkpoint_config_from_dict_defaults_cadence_seconds_when_absent():
    restored = CheckpointConfig.from_dict({"cadence_entries": 50, "max_lag_entries": 75})
    assert restored.cadence_seconds == 900


def test_checkpoint_config_ts_urls_empty_by_default():
    cfg = CheckpointConfig()
    assert cfg.ts_urls == []  # registration is opt-in, never assumed


def test_example_config_ships_the_witness_url_commented_out():
    assert f'# ts_urls = ["{DEFAULT_TS_URL}"]' in EXAMPLE_CONFIG_TOML
    assert f'ts_urls = ["{DEFAULT_TS_URL}"]\n' not in EXAMPLE_CONFIG_TOML.replace(
        f'# ts_urls = ["{DEFAULT_TS_URL}"]\n', ""
    )


# ---------------------------------------------------------------------------
# register_checkpoint: DEFAULT_TS_URL is the semantic witness.
# agentactioncapsule.org endpoint, but its CNAME onto
# anchor.agentactioncapsule.org has not propagated yet -- register_checkpoint
# must still dispatch the actual HTTP request to the anchor host today, while
# recording the semantic (witness.) URL on the returned WitnessRecord. An
# explicit non-default ts_url must never be rewritten.
# ---------------------------------------------------------------------------


class _FakeUrlopenResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_default_ts_url_is_the_witness_host():
    assert DEFAULT_TS_URL == "https://witness.agentactioncapsule.org"


def test_register_checkpoint_default_url_dispatches_to_anchor_host_today(monkeypatch):
    from capsule_emit.checkpoint import emit as emit_mod

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["full_url"] = req.full_url
        body = json.dumps(
            {
                "entry_hash": "a" * 64,
                "receipt_b64": "c3R1Yg==",
                "leaf_index": 0,
                "tree_size": 1,
            }
        ).encode()
        return _FakeUrlopenResponse(body)

    monkeypatch.setattr(emit_mod.urllib.request, "urlopen", fake_urlopen)

    signer = HmacSigner("node-a")
    mmr = MmrLedger(FakeLogSource())
    mmr.append(synthetic_capsule(0))
    cp = emit_checkpoint(mmr, signer, log_id="log-a")

    witness_record = emit_mod.register_checkpoint(cp)  # default ts_url

    assert captured["full_url"] == "https://anchor.agentactioncapsule.org/v1/digest", (
        "the default (CNAME-pending) witness URL must still dispatch to the "
        "anchor host today, or registration would silently start failing"
    )
    assert witness_record.ts_url == DEFAULT_TS_URL == "https://witness.agentactioncapsule.org", (
        "the WitnessRecord must record the semantic witness URL, not the "
        "host the request was actually dispatched to"
    )


def test_register_checkpoint_explicit_non_default_url_is_never_rewritten(monkeypatch):
    from capsule_emit.checkpoint import emit as emit_mod

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["full_url"] = req.full_url
        body = json.dumps(
            {
                "entry_hash": "b" * 64,
                "receipt_b64": "c3R1Yg==",
                "leaf_index": 0,
                "tree_size": 1,
            }
        ).encode()
        return _FakeUrlopenResponse(body)

    monkeypatch.setattr(emit_mod.urllib.request, "urlopen", fake_urlopen)

    signer = HmacSigner("node-a")
    mmr = MmrLedger(FakeLogSource())
    mmr.append(synthetic_capsule(0))
    cp = emit_checkpoint(mmr, signer, log_id="log-a")

    custom_url = "https://my-own-ts.example.org"
    witness_record = emit_mod.register_checkpoint(cp, custom_url)

    assert captured["full_url"] == "https://my-own-ts.example.org/v1/digest"
    assert witness_record.ts_url == custom_url
