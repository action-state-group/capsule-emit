# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [o16-14-precond-checkpoint-signer] (O16-14's bundle
precondition): checkpoint/stamp entries must stop carrying the ephemeral
HMAC ``witness._AutoSigner`` output and instead be signed by the SAME
persisted Ed25519 identity ``capsule_emit.signing.LocalKeypairSigner`` (#80)
already signs capsule content with -- so a checkpoint signed in one process
is still verifiable in a later one, which an ephemeral, in-process-only HMAC
secret could never be.

Covers:
- default checkpoints are signed by the persisted Ed25519 key, not
  ``_AutoSigner`` (an ephemeral key would never match a freshly reloaded
  ``LocalKeypairSigner`` at the ledger's default key path)
- a tampered checkpoint signature is rejected (the check is real, not a
  rubber stamp)
- cross-process verify: a checkpoint signed by a subprocess verifies in this
  (different) process, by reloading the persisted key from disk
- ``signing_key_path=`` and ``CAPSULE_SIGNING_KEY_PATH`` -- the same
  overrides ``seal()`` honors for capsule content -- apply to the checkpoint
  signer too
- #78's stamp-leaf-covers-full-entry invariant stays intact under the new
  signer (the stamp's ``capsule_id`` still equals ``entry_digest()``, the
  full persisted entry, not just the signing body)
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from capsule_emit import ledger as ledger_mod
from capsule_emit import seal, witness
from capsule_emit.checkpoint.emit import CheckpointRecord, verify_checkpoint_signature
from capsule_emit.signing import LocalKeypairSigner

_WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service -- same shape used across the witness
# test files, duplicated here so this file has no cross-file dependency.
# ---------------------------------------------------------------------------


class _StubWitnessTSHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path == "/v1/digest":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            body = json.loads(raw)
            self.received.append(body)
            digest = body["capsule_id"]
            entry_hash = hashlib.sha256(bytes.fromhex(digest)).hexdigest()
            resp = {
                "entry_hash": entry_hash,
                "receipt_b64": base64.b64encode(b"stub-receipt-not-a-real-cose-receipt").decode(),
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
def stub_ts():
    base_url, received, stop = _start_stub_ts()
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


def _first_stamp(ledger_path) -> dict:
    entries = ledger_mod.read_ledger_entries(ledger_path)
    stamps = [e for e in entries if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND]
    assert stamps, "no checkpoint stamp was persisted"
    return stamps[0]


# ---------------------------------------------------------------------------
# Default checkpoints are signed by the persisted Ed25519 key
# ---------------------------------------------------------------------------


def test_default_checkpoint_signed_by_persisted_ed25519_key(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND
            for e in ledger_mod.read_ledger_entries(ledger_path)
        )
    )

    stamp = _first_stamp(ledger_path)
    cp = CheckpointRecord.from_dict(stamp["checkpoint"])

    # The persisted key next to the ledger -- the SAME file seal() signs
    # capsule content with -- is what actually produced this checkpoint's
    # signature. A stale ephemeral _AutoSigner key_id would never match a
    # freshly reloaded LocalKeypairSigner at this path.
    key_path = str(ledger_path) + ".signing_key.pem"
    assert os.path.exists(key_path)
    reloaded = LocalKeypairSigner(key_path)
    assert cp.key_id == reloaded.key_id

    checkpoint_signer = witness._PersistedCheckpointSigner(reloaded)
    assert verify_checkpoint_signature(cp, checkpoint_signer)

    # Ed25519 signatures over a 32-byte digest are 64 raw bytes -- 128 hex
    # chars -- unlike _AutoSigner's HMAC-SHA256 (32 bytes / 64 hex chars).
    assert len(cp.signature) == 128
    int(cp.signature, 16)  # must be hex


def test_tampered_checkpoint_signature_is_rejected(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND
            for e in ledger_mod.read_ledger_entries(ledger_path)
        )
    )

    stamp = _first_stamp(ledger_path)
    cp = CheckpointRecord.from_dict(stamp["checkpoint"])
    key_path = str(ledger_path) + ".signing_key.pem"
    checkpoint_signer = witness._PersistedCheckpointSigner(LocalKeypairSigner(key_path))

    assert verify_checkpoint_signature(cp, checkpoint_signer)

    tampered = CheckpointRecord.from_dict(stamp["checkpoint"])
    tampered.root = "00" * 32
    assert not verify_checkpoint_signature(tampered, checkpoint_signer)

    wrong_key_signer = witness._PersistedCheckpointSigner(
        LocalKeypairSigner(tmp_path / "unrelated.pem")
    )
    assert not verify_checkpoint_signature(cp, wrong_key_signer)


# ---------------------------------------------------------------------------
# Cross-process verify: signed by a subprocess, verified in THIS process.
# ---------------------------------------------------------------------------

_SUBPROCESS_SEAL_SCRIPT = """
import sys
sys.path.insert(0, {worktree_root!r})
from capsule_emit import seal

ledger_path, ts_url = sys.argv[1], sys.argv[2]
for i in range(2):
    seal(None, action=f"action-{{i}}", operator="acme", anchor=False,
         ledger=ledger_path, witness_url=ts_url)
"""


def test_checkpoint_signed_in_one_process_verifies_in_another(tmp_path, stub_ts):
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    script = _SUBPROCESS_SEAL_SCRIPT.format(worktree_root=_WORKTREE_ROOT)
    env = dict(os.environ, CAPSULE_WITNESS_CADENCE_ENTRIES="2")
    result = subprocess.run(
        [sys.executable, "-c", script, str(ledger_path), ts_url],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert _wait_for(lambda: len(received) >= 1), (
        "the subprocess never registered a checkpoint with the TS"
    )
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND
            for e in ledger_mod.read_ledger_entries(ledger_path)
        )
    ), "the subprocess's checkpoint was never persisted"

    stamp = _first_stamp(ledger_path)
    cp = CheckpointRecord.from_dict(stamp["checkpoint"])

    # THIS process never signed anything -- it only reloads the key the
    # subprocess persisted to disk and checks the signature against it. If
    # the checkpoint were still signed by an ephemeral, in-process
    # _AutoSigner secret, this reload could never reproduce a matching
    # signature because that secret died with the subprocess.
    key_path = str(ledger_path) + ".signing_key.pem"
    assert os.path.exists(key_path)
    reloaded_in_this_process = LocalKeypairSigner(key_path)
    checkpoint_signer = witness._PersistedCheckpointSigner(reloaded_in_this_process)
    assert verify_checkpoint_signature(cp, checkpoint_signer), (
        "checkpoint signed in a subprocess did not verify against the "
        "persisted key reloaded in this process"
    )


# ---------------------------------------------------------------------------
# The checkpoint signer follows the SAME resolution overrides as seal()
# ---------------------------------------------------------------------------


def test_signing_key_path_override_applies_to_checkpoint_signer_too(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    custom_key_path = tmp_path / "custom-producer-key.pem"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url, signing_key_path=custom_key_path)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND
            for e in ledger_mod.read_ledger_entries(ledger_path)
        )
    )

    stamp = _first_stamp(ledger_path)
    cp = CheckpointRecord.from_dict(stamp["checkpoint"])

    assert custom_key_path.exists()
    reloaded = LocalKeypairSigner(custom_key_path)
    assert cp.key_id == reloaded.key_id
    checkpoint_signer = witness._PersistedCheckpointSigner(reloaded)
    assert verify_checkpoint_signature(cp, checkpoint_signer)

    # The default per-ledger key path must NOT have been used.
    assert not (str(ledger_path) + ".signing_key.pem" == str(custom_key_path))
    assert not os.path.exists(str(ledger_path) + ".signing_key.pem")


def test_custom_signer_overrides_default_for_checkpoint_too(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    custom_signer = LocalKeypairSigner(tmp_path / "byo.pem")

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url, signer=custom_signer)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND
            for e in ledger_mod.read_ledger_entries(ledger_path)
        )
    )

    stamp = _first_stamp(ledger_path)
    cp = CheckpointRecord.from_dict(stamp["checkpoint"])
    assert cp.key_id == custom_signer.key_id
    checkpoint_signer = witness._PersistedCheckpointSigner(custom_signer)
    assert verify_checkpoint_signature(cp, checkpoint_signer)


# ---------------------------------------------------------------------------
# #78's stamp-leaf-covers-full-entry invariant stays intact under the new
# signer (regression guard -- the new signer must not change what the
# persisted stamp's own leaf digest commits to).
# ---------------------------------------------------------------------------


def test_stamp_leaf_still_covers_the_full_persisted_entry(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND
            for e in ledger_mod.read_ledger_entries(ledger_path)
        )
    )

    stamp = _first_stamp(ledger_path)
    cp = CheckpointRecord.from_dict(stamp["checkpoint"])
    assert stamp["capsule_id"] == cp.entry_digest()
    assert stamp["capsule_id"] != cp.digest(), (
        "leaf must cover signature + witnesses too, not just the signing body"
    )
