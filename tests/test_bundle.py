# SPDX-License-Identifier: Apache-2.0
"""Tests for O16 audit item 14 — ``capsule_emit.bundle``.

Builds real checkpoint chains through ``seal()`` + the default witness
wiring (same stub-TS harness as ``tests/test_checkpoint_signer.py``) and
checks that ``bundle()`` assembles a standalone-verifiable artifact:
receipt + inclusion proof + covering checkpoint + prior checkpoint +
consistency proof — and that ``verify_bundle()`` genuinely checks each
piece (every negative case below flips exactly one field and confirms the
mutant is caught).
"""
from __future__ import annotations

import http.server
import json
import threading
import time
from dataclasses import replace

import pytest
from _stub_receipt import (
    TEST_TS_PUBLIC_KEY_PEM,
    build_stub_receipt_b64,
    checkpoint_dict_from_cose,
    checkpoint_entry_hash,
)

from capsule_emit import ledger as ledger_mod
from capsule_emit import seal, witness
from capsule_emit.bundle import Bundle, BundleError, bundle, verify_bundle
from capsule_emit.checkpoint import core as mmr_core
from capsule_emit.checkpoint import emit as checkpoint_emit_mod

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service — same shape as test_checkpoint_signer.py
# ---------------------------------------------------------------------------


class _StubWitnessTSHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path == "/checkpoints":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = checkpoint_dict_from_cose(raw)
            except ValueError as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
            self.received.append(body)
            entry_hash = checkpoint_entry_hash(body)
            resp = {
                "entry_hash": entry_hash,
                "receipt_b64": build_stub_receipt_b64(entry_hash),
                "leaf_index": 0,
                "tree_size": 1,
            }
            payload = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()


def _start_stub_ts():
    received: list[dict] = []
    handler_cls = type(
        "_BoundStubWitnessTSHandler", (_StubWitnessTSHandler,), {"received": received}
    )
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", received, srv.shutdown


@pytest.fixture
def stub_ts(monkeypatch):
    # Simulate that this hermetic stub IS the operator's pinned default
    # witness ([verify-batch-fastfollow] item D): the DEFAULT read path only
    # signature-verifies a stamp as WITNESSED when its ts_url matches the
    # pinned DEFAULT_TS_URL and the receipt verifies against
    # DEFAULT_TS_PUBLIC_KEY_PEM. Without this, every stamp this stub mints
    # would correctly demote to "shape valid; TS identity unverified" (an
    # unpinned TS), which is exactly right in production but would make
    # every "genuinely witnessed" fixture in this file fail for the wrong
    # reason. monkeypatch reverts both per test, so ephemeral ports never
    # leak across tests.
    base_url, received, stop = _start_stub_ts()
    monkeypatch.setattr(checkpoint_emit_mod, "DEFAULT_TS_URL", base_url)
    monkeypatch.setattr(checkpoint_emit_mod, "DEFAULT_TS_PUBLIC_KEY_PEM", TEST_TS_PUBLIC_KEY_PEM)
    yield base_url, received
    stop()


@pytest.fixture(autouse=True)
def _clean_witness_state():
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False
    yield
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    ok = predicate()
    while not ok and time.monotonic() < deadline:
        time.sleep(0.01)
        ok = predicate()
    return ok


def _stamp_count(ledger_path) -> int:
    entries = ledger_mod.read_ledger_entries(ledger_path)
    return sum(1 for e in entries if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND)


# ---------------------------------------------------------------------------
# Two-checkpoint fixture — gives us a record covered by the FIRST checkpoint
# (no prior — the bracket's lower-bound-free edge case) and one covered by
# the SECOND (genuine prior_checkpoint + consistency_proof).
# ---------------------------------------------------------------------------


@pytest.fixture
def two_checkpoint_ledger(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    caps = []
    for i in range(2):
        result = seal(
            None, action=f"first-{i}", operator="acme", anchor=False,
            ledger=ledger_path, witness_url=ts_url,
        )
        caps.append(result.capsule)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    for i in range(2):
        result = seal(
            None, action=f"second-{i}", operator="acme", anchor=False,
            ledger=ledger_path, witness_url=ts_url,
        )
        caps.append(result.capsule)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 2)

    return ledger_path, caps


def test_bundle_first_checkpoint_record_has_no_prior(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[0]["capsule_id"])

    assert b.capsule_id == caps[0]["capsule_id"]
    assert b.seq == 1
    assert b.receipt["capsule_id"] == caps[0]["capsule_id"]
    assert b.checkpoint.prev_size == 0
    assert b.prior_checkpoint is None
    assert b.consistency_proof is None

    ok, errors = verify_bundle(b)
    assert ok, errors


def test_bundle_second_checkpoint_record_has_prior_and_consistency(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    # caps[2]/caps[3] ("second-*") are only covered once the SECOND checkpoint
    # forms — that checkpoint also covers the first checkpoint's own stamp
    # entry (item 16), so its prev_size/prev_root genuinely point at checkpoint 1.
    b = bundle(ledger_path, caps[2]["capsule_id"])

    assert b.checkpoint.prev_size > 0
    assert b.prior_checkpoint is not None
    assert b.prior_checkpoint.mmr_size == b.checkpoint.prev_size
    assert b.prior_checkpoint.root == b.checkpoint.prev_root
    assert b.consistency_proof is not None

    ok, errors = verify_bundle(b)
    assert ok, errors


# ---------------------------------------------------------------------------
# [verify-batch-fastfollow] item A / Decision 2 — the consistency-proof
# check's PASSING message must label itself honestly: anti-REWRITE only,
# never implying anti-FORK / anti-equivocation (that is the witness's job).
# ---------------------------------------------------------------------------


_FORBIDDEN_OVERCLAIM_PHRASES = ("no fork", "not equivocated", "no equivocation")


def test_verify_bundle_labels_passing_consistency_proof_as_history_intact_not_fork(
    two_checkpoint_ledger,
):
    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[2]["capsule_id"])
    assert b.prior_checkpoint is not None  # exercising the WITH-prior branch

    ok, notices = verify_bundle(b)
    assert ok, notices

    history_notices = [n for n in notices if "history intact between checkpoints" in n]
    assert len(history_notices) == 1, notices
    msg = history_notices[0]
    assert f"{b.prior_checkpoint.mmr_size} and {b.checkpoint.mmr_size}" in msg
    for phrase in _FORBIDDEN_OVERCLAIM_PHRASES:
        assert phrase not in msg.lower(), f"{msg!r} must never say/imply {phrase!r}"


def test_verify_bundle_labels_first_checkpoint_edge_honestly(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[0]["capsule_id"])
    assert b.prior_checkpoint is None  # exercising the first-checkpoint branch

    ok, notices = verify_bundle(b)
    assert ok, notices

    first_notices = [n for n in notices if "no prior checkpoint" in n]
    assert len(first_notices) == 1, notices
    msg = first_notices[0]
    assert "first" in msg
    for phrase in _FORBIDDEN_OVERCLAIM_PHRASES:
        assert phrase not in msg.lower(), f"{msg!r} must never say/imply {phrase!r}"
    # Never confused with the WITH-prior notice.
    assert "history intact between checkpoints" not in msg


def test_bundle_accepts_unambiguous_prefix(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    prefix = caps[0]["capsule_id"][:10]
    b = bundle(ledger_path, prefix)
    assert b.capsule_id == caps[0]["capsule_id"]


def test_bundle_unknown_capsule_id_raises(two_checkpoint_ledger):
    ledger_path, _caps = two_checkpoint_ledger
    with pytest.raises(BundleError, match="no record matches"):
        bundle(ledger_path, "f" * 64)


def test_bundle_ambiguous_prefix_raises(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    # An 8-char (or longer) prefix common to two DIFFERENT full ids should be
    # rejected rather than silently picking one — construct that collision
    # directly against the resolver so the test doesn't depend on hash luck.
    from capsule_emit.bundle import _find_record

    entries = [
        {"capsule_id": "aaaaaaaa1111"},
        {"capsule_id": "aaaaaaaa2222"},
    ]
    with pytest.raises(BundleError, match="matches 2 records"):
        _find_record(entries, "aaaaaaaa")


def test_bundle_uncovered_record_raises(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "100")
    ts_url, _received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    result = seal(None, action="lonely", operator="acme", anchor=False,
                   ledger=ledger_path, witness_url=ts_url)

    with pytest.raises(BundleError, match="not yet covered"):
        bundle(ledger_path, result.capsule["capsule_id"])


def test_bundle_empty_ledger_raises(tmp_path):
    with pytest.raises(BundleError, match="empty or not found"):
        bundle(tmp_path / "nope.jsonl", "f" * 64)


def test_bundle_cannot_target_a_checkpoint_stamp_entry(two_checkpoint_ledger):
    ledger_path, _caps = two_checkpoint_ledger
    entries = ledger_mod.read_ledger_entries(ledger_path)
    stamp = next(e for e in entries if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND)
    with pytest.raises(BundleError, match="no record matches"):
        bundle(ledger_path, stamp["capsule_id"])


# ---------------------------------------------------------------------------
# Self-attested (no witness) checkpoints still bundle — witnessing is
# orthogonal to whether a record can be bundled.
# ---------------------------------------------------------------------------


def test_bundle_self_attested_checkpoint_still_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"

    caps = []
    for i in range(2):
        result = seal(
            None, action=f"solo-{i}", operator="acme", anchor=False,
            ledger=ledger_path, witness_url="http://127.0.0.1:1",  # nothing listens — registration fails
        )
        caps.append(result.capsule)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    b = bundle(ledger_path, caps[0]["capsule_id"])
    assert b.checkpoint.witnesses == []
    ok, errors = verify_bundle(b)
    assert ok, errors


# ---------------------------------------------------------------------------
# [verify-threestate-trustanchor] -- a well-formed stamp from an UNPINNED
# witness (no caller-supplied pin, and not the built-in default) must NOT
# make the bundle INVALID: it is exactly what a self-hosted/zero-egress TS
# a caller hasn't pinned yet looks like, and frozen §1a.2 promises that
# deployment shape works. Three states: unpinned -> unverified (bundle OK);
# pinned + genuine -> witnessed; pinned + forged -> INVALID.
# ---------------------------------------------------------------------------


@pytest.fixture
def self_hosted_stub_ts():
    """A stub TS whose URL is deliberately NOT ``DEFAULT_TS_URL`` (never
    monkeypatched) -- simulating a self-hosted/zero-egress deployment's own
    Transparency Service, which has no built-in pin."""
    base_url, received, stop = _start_stub_ts()
    yield base_url, received
    stop()


def test_bundle_self_hosted_unpinned_witness_is_not_invalid(
    tmp_path, self_hosted_stub_ts, monkeypatch
):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1")
    ts_url, _received = self_hosted_stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    result = seal(None, action="solo", operator="acme", anchor=False,
                   ledger=ledger_path, witness_url=ts_url)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    b = bundle(ledger_path, result.capsule["capsule_id"])
    assert len(b.checkpoint.witnesses) == 1
    assert b.checkpoint.witnesses[0].ts_url == ts_url
    assert ts_url != checkpoint_emit_mod.DEFAULT_TS_URL

    ok, notices = verify_bundle(b)  # no trust_anchor supplied
    assert ok is True, notices
    assert any("pin not supplied" in n and "unverified stamp" in n for n in notices)
    assert any(ts_url in n for n in notices)


def test_bundle_self_hosted_witness_pinned_via_trust_anchor_is_witnessed(
    tmp_path, self_hosted_stub_ts, monkeypatch
):
    from _stub_receipt import TEST_TS_PUBLIC_KEY_PEM

    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1")
    ts_url, _received = self_hosted_stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    result = seal(None, action="solo", operator="acme", anchor=False,
                   ledger=ledger_path, witness_url=ts_url)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    b = bundle(ledger_path, result.capsule["capsule_id"])
    ok, errors = verify_bundle(b, trust_anchor={ts_url: TEST_TS_PUBLIC_KEY_PEM})
    assert ok is True, errors
    assert not any("unverified" in e for e in errors)


def test_bundle_self_hosted_witness_pinned_but_forged_signature_is_invalid(
    tmp_path, self_hosted_stub_ts, monkeypatch
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1")
    ts_url, _received = self_hosted_stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    result = seal(None, action="solo", operator="acme", anchor=False,
                   ledger=ledger_path, witness_url=ts_url)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    b = bundle(ledger_path, result.capsule["capsule_id"])
    wrong_pubkey_pem = Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    ok, errors = verify_bundle(b, trust_anchor={ts_url: wrong_pubkey_pem})
    assert ok is False
    assert any("witness stamp" in e and "INVALID" in e for e in errors)


# ---------------------------------------------------------------------------
# Serialization round-trip — the whole point of "standalone": it must
# survive being written to a file and read back by a different process.
# ---------------------------------------------------------------------------


def test_bundle_json_roundtrip_still_verifies(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[2]["capsule_id"])

    raw = json.dumps(b.to_dict())
    restored = Bundle.from_dict(json.loads(raw))

    ok, errors = verify_bundle(restored)
    assert ok, errors
    assert restored.to_dict() == b.to_dict()


# ---------------------------------------------------------------------------
# [cll-checkpoint-cose-wire] Decision 1's moment (b): the covering
# checkpoint's COSE_Sign1 wire form, carried through from production
# (witness._build_checkpoint_cose_hex) into the bundle, independently
# checkable by a generic COSE/SCITT verifier.
# ---------------------------------------------------------------------------


def test_bundle_carries_checkpoint_cose_and_it_independently_verifies(two_checkpoint_ledger):
    from capsule_emit.checkpoint.cose_wire import verify_checkpoint_cose_offline

    ledger_path, caps = two_checkpoint_ledger

    first = bundle(ledger_path, caps[0]["capsule_id"])
    assert first.checkpoint_cose is not None
    result = verify_checkpoint_cose_offline(first.checkpoint_cose)
    assert result.ok, result.errors
    assert result.decoded.consistency_proof is None  # first checkpoint, no prior

    second = bundle(ledger_path, caps[2]["capsule_id"])
    assert second.checkpoint_cose is not None
    result = verify_checkpoint_cose_offline(second.checkpoint_cose)
    assert result.ok, result.errors
    assert result.decoded.consistency_proof is not None  # carries the real extension proof

    ok, notices = verify_bundle(second)
    assert ok, notices
    assert any("COSE-wire statement independently verified" in n for n in notices)


def test_bundle_checkpoint_cose_roundtrips_through_json(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[2]["capsule_id"])
    assert b.checkpoint_cose is not None

    raw = json.dumps(b.to_dict())
    restored = Bundle.from_dict(json.loads(raw))
    assert restored.checkpoint_cose == b.checkpoint_cose

    ok, errors = verify_bundle(restored)
    assert ok, errors


def test_verify_bundle_catches_tampered_checkpoint_cose(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    assert b.checkpoint_cose is not None
    tampered = bytearray(b.checkpoint_cose)
    tampered[-1] ^= 0xFF
    mutant = replace(b, checkpoint_cose=bytes(tampered))
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("COSE-wire statement failed to verify" in e for e in errors)


def test_verify_bundle_catches_checkpoint_cose_from_a_different_checkpoint(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    first = bundle(ledger_path, caps[0]["capsule_id"])
    second = bundle(ledger_path, caps[2]["capsule_id"])
    # Swap in the FIRST checkpoint's genuinely-valid COSE statement onto the
    # SECOND checkpoint's bundle -- a well-formed, correctly-signed
    # statement, just for the wrong checkpoint.
    mutant = replace(second, checkpoint_cose=first.checkpoint_cose)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("do not match the bundle's" in e for e in errors)


def test_verify_bundle_tolerates_missing_checkpoint_cose(valid_bundle_with_prior):
    """Backward compatibility: a bundle from a ledger predating this field
    (or whose COSE serialization failed at production time) must still
    verify -- absence is not evidence of anything."""
    b = valid_bundle_with_prior
    mutant = replace(b, checkpoint_cose=None)
    ok, errors = verify_bundle(mutant)
    assert ok, errors


# ---------------------------------------------------------------------------
# Mutation tests — every negative check in verify_bundle must actually catch
# its mutant, one field at a time.
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_bundle_with_prior(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    return bundle(ledger_path, caps[2]["capsule_id"])


def test_verify_bundle_catches_receipt_capsule_id_mismatch(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    tampered_receipt = dict(b.receipt)
    tampered_receipt["capsule_id"] = "0" * 64
    mutant = replace(b, receipt=tampered_receipt)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("receipt.capsule_id" in e for e in errors)


def test_verify_bundle_catches_tampered_inclusion_witness(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    assert b.inclusion_proof.witness, "test needs a non-trivial inclusion path"
    bad_witness = ("00" * 32,) + b.inclusion_proof.witness[1:]
    mutant_proof = replace(b.inclusion_proof, witness=bad_witness)
    mutant = replace(b, inclusion_proof=mutant_proof)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("inclusion proof" in e for e in errors)


def test_verify_bundle_catches_tampered_checkpoint_signature(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    mutant_cp = replace(b.checkpoint, signature="00" * 64)
    mutant = replace(b, checkpoint=mutant_cp)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("covering checkpoint signature" in e for e in errors)


def test_verify_bundle_catches_tampered_checkpoint_root(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    mutant_cp = replace(b.checkpoint, root="ff" * 32)
    mutant = replace(b, checkpoint=mutant_cp)
    ok, errors = verify_bundle(mutant)
    assert not ok
    # Breaks both inclusion (wrong root) and the signature (root is signed).
    assert any("inclusion proof" in e for e in errors)
    assert any("covering checkpoint signature" in e for e in errors)


def test_verify_bundle_catches_tampered_prior_checkpoint_signature(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    mutant_prior = replace(b.prior_checkpoint, signature="11" * 64)
    mutant = replace(b, prior_checkpoint=mutant_prior)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("prior checkpoint signature" in e for e in errors)


def test_verify_bundle_catches_prev_size_mismatch(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    mutant_cp = replace(b.checkpoint, prev_size=b.checkpoint.prev_size + 999)
    mutant = replace(b, checkpoint=mutant_cp)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("prev_size" in e for e in errors)


def test_verify_bundle_catches_prev_root_mismatch(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    mutant_cp = replace(b.checkpoint, prev_root="ab" * 32)
    mutant = replace(b, checkpoint=mutant_cp)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("prev_root" in e for e in errors)


def test_verify_bundle_catches_tampered_consistency_proof(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    assert b.consistency_proof.old_peaks, "test needs at least one old peak"
    bad_peaks = ("00" * 32,) + b.consistency_proof.old_peaks[1:]
    mutant_proof = replace(b.consistency_proof, old_peaks=bad_peaks)
    mutant = replace(b, consistency_proof=mutant_proof)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("consistency proof" in e for e in errors)


def test_verify_bundle_catches_missing_prior_when_prev_size_nonzero(valid_bundle_with_prior):
    b = valid_bundle_with_prior
    mutant = replace(b, prior_checkpoint=None, consistency_proof=None)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("prev_size != 0" in e for e in errors)


def test_verify_bundle_catches_consistency_proof_without_prior(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    first = bundle(ledger_path, caps[0]["capsule_id"])
    assert first.prior_checkpoint is None
    borrowed = bundle(ledger_path, caps[2]["capsule_id"]).consistency_proof
    mutant = replace(first, consistency_proof=borrowed)
    ok, errors = verify_bundle(mutant)
    assert not ok
    assert any("without a prior_checkpoint" in e for e in errors)


# ---------------------------------------------------------------------------
# verify_checkpoint_signature_offline — the pure Ed25519 public-key check
# a stranger uses (no Signer, no private key).
# ---------------------------------------------------------------------------


def test_verify_checkpoint_signature_offline(two_checkpoint_ledger):
    from capsule_emit.checkpoint.emit import verify_checkpoint_signature_offline

    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[0]["capsule_id"])
    assert verify_checkpoint_signature_offline(b.checkpoint)

    tampered = replace(b.checkpoint, signature="ff" * 64)
    assert not verify_checkpoint_signature_offline(tampered)


def test_verify_checkpoint_signature_offline_rejects_non_ed25519_key_id():
    from capsule_emit.checkpoint.emit import CheckpointRecord, verify_checkpoint_signature_offline

    # An HMAC-style key_id (arbitrary label, not a 32-byte public key) must
    # fail closed, never raise, and never false-pass.
    cp = CheckpointRecord(
        v=1, kind="mmr_checkpoint", log_id="x", mmr_size=1, root="00" * 32,
        prev_size=0, prev_root="", key_id="not-a-public-key", timestamp="t",
        signature="ab" * 32,
    )
    assert not verify_checkpoint_signature_offline(cp)


# ---------------------------------------------------------------------------
# core.leaf_count / inclusion sanity: bundle()'s reconstructed MMR size must
# match the checkpoint it claims to cover.
# ---------------------------------------------------------------------------


def test_bundle_reconstructed_mmr_matches_checkpoint(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[2]["capsule_id"])
    assert b.inclusion_proof.size == b.checkpoint.mmr_size
    assert mmr_core.leaf_count(b.checkpoint.mmr_size) >= b.seq
