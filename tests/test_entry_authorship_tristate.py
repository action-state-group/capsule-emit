# SPDX-License-Identifier: Apache-2.0
"""Tests for [verify-entry-authorship-tristate-and-log].

RULING 1/2: per-record authorship is graded THREE states, not two --
claimed-and-verifies (AUTHORED), absent (UNCLAIMED -- a ``log()`` entry,
never fatal), claimed-and-fails (INVALID -- forgery, still fatal). Covers
``verify_capsule_signature_tristate`` directly, its two call sites
(``verify_bundle`` / ``verify_store_signed``), and RULING 3's ``log()`` verb.

Uses the same hermetic stub-TS harness as ``test_bundle.py`` /
``test_checkpoint_signer.py`` so ``bundle()``/``verify_bundle()`` exercise a
genuinely-witnessed checkpoint chain rather than the network egress or the
``CAPSULE_WITNESS=stub`` in-process double (which mints a receipt shape this
suite's own inclusion-proof reconstruction does not accept -- unrelated
pre-existing gap, not exercised here).
"""
from __future__ import annotations

import http.server
import json
import threading
import time

import pytest
from _stub_receipt import (
    TEST_TS_PUBLIC_KEY_PEM,
    build_stub_receipt_b64,
    checkpoint_dict_from_cose,
    checkpoint_entry_hash,
)

from capsule_emit import ledger as ledger_mod
from capsule_emit import log, seal, witness
from capsule_emit.bundle import bundle, verify_bundle
from capsule_emit.checkpoint import emit as checkpoint_emit_mod
from capsule_emit.core import LogEntry
from capsule_emit.signing import (
    AuthorshipVerdict,
    verify_capsule_signature_tristate,
    verify_store_signed,
)

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service -- same shape as test_bundle.py.
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
# verify_capsule_signature_tristate -- pure, no ledger needed.
# ---------------------------------------------------------------------------


def test_tristate_authored_for_a_genuinely_signed_capsule(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    capsule = seal({"x": 1}, anchor=False, witness=False).capsule
    verdict, messages = verify_capsule_signature_tristate(capsule)
    assert verdict is AuthorshipVerdict.AUTHORED
    assert messages == []


def test_tristate_unclaimed_when_signature_and_key_id_both_absent():
    capsule = {"capsule_id": "a" * 64, "operator": "acme"}
    verdict, messages = verify_capsule_signature_tristate(capsule)
    assert verdict is AuthorshipVerdict.UNCLAIMED
    assert messages == []


def test_tristate_invalid_when_only_one_of_signature_key_id_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    capsule = dict(seal({"x": 1}, anchor=False, witness=False).capsule)
    del capsule["key_id"]
    verdict, messages = verify_capsule_signature_tristate(capsule)
    assert verdict is AuthorshipVerdict.INVALID
    assert any("only one of signature/key_id" in m for m in messages)


def test_tristate_invalid_when_signature_present_but_wrong(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    capsule = dict(seal({"x": 1}, anchor=False, witness=False).capsule)
    capsule["signature"] = "00" * (len(capsule["signature"]) // 2)
    verdict, messages = verify_capsule_signature_tristate(capsule)
    assert verdict is AuthorshipVerdict.INVALID
    assert any("forged" in m or "tampered" in m for m in messages)


def test_tristate_never_raises_on_non_mapping_input():
    verdict, messages = verify_capsule_signature_tristate(["not", "a", "dict"])
    assert verdict is AuthorshipVerdict.INVALID
    assert messages


# ---------------------------------------------------------------------------
# log() -- RULING 3: the unsigned append verb.
# ---------------------------------------------------------------------------


def test_log_returns_a_log_entry_never_an_emit_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    entry = log(b"hello world", witness=False)
    assert isinstance(entry, LogEntry)
    assert "signature" not in entry.capsule
    assert "key_id" not in entry.capsule
    assert entry.capsule_id


def test_log_accepts_str_and_produces_the_same_digest_as_bytes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from_str = log("hello world", ledger=tmp_path / "a.jsonl", witness=False)
    from_bytes = log(b"hello world", ledger=tmp_path / "b.jsonl", witness=False)
    assert from_str.capsule["model_attestation"]["compute_attestation"]["log_digest"] == (
        from_bytes.capsule["model_attestation"]["compute_attestation"]["log_digest"]
    )


@pytest.mark.parametrize("bad_value", [7, True, [1, 2, 3], None, 3.5])
def test_log_refuses_implicit_buffer_coercion(tmp_path, monkeypatch, bad_value):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match="artifact_bytes must be str"):
        log(bad_value, witness=False)


def test_log_entry_is_a_real_ledger_leaf_alongside_capsules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ledger_path = tmp_path / "ledger.jsonl"
    cap = seal({"x": 1}, ledger=ledger_path, anchor=False, witness=False)
    entry = log(b"raw evidence", ledger=ledger_path, witness=False)

    raw_entries = ledger_mod.read_ledger_entries(ledger_path)
    assert len(raw_entries) == 2
    ids = {e.get("capsule_id") for e in raw_entries}
    assert ids == {cap.capsule_id, entry.capsule_id}


def test_no_sign_kwarg_exists_anywhere_on_seal_or_received(tmp_path, monkeypatch):
    """RULING 3: there must be no ``sign=`` backdoor reachable from ``seal()``
    -- a boolean flag there would be exactly the "hand people the weaker
    guarantee while they think they sealed" outcome RULING 3 forbids."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError):
        seal({"x": 1}, sign=False, anchor=False, witness=False)


# ---------------------------------------------------------------------------
# verify_store_signed -- RULING 2, signing.py call site.
# ---------------------------------------------------------------------------


def test_verify_store_signed_grades_authored_unclaimed_invalid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ledger_path = tmp_path / "ledger.jsonl"
    authored = seal({"x": 1}, ledger=ledger_path, anchor=False, witness=False).capsule
    unclaimed = log(b"raw", ledger=ledger_path, witness=False).capsule
    invalid = dict(seal({"y": 2}, ledger=ledger_path, anchor=False, witness=False).capsule)
    invalid["signature"] = "00" * (len(invalid["signature"]) // 2)

    results = verify_store_signed([authored, unclaimed, invalid])
    r_authored, r_unclaimed, r_invalid = results

    assert r_authored.ok
    assert not any(f.code.startswith("producer_signature") for f in r_authored.findings)

    assert r_unclaimed.ok, r_unclaimed.findings
    assert any(
        f.code == "producer_signature_unclaimed" and f.severity == "warning"
        for f in r_unclaimed.findings
    )

    assert not r_invalid.ok
    assert any(
        f.code == "producer_signature_invalid" and f.severity == "error"
        for f in r_invalid.findings
    )


# ---------------------------------------------------------------------------
# verify_bundle -- RULING 2, bundle.py call site (needs a real checkpoint).
# ---------------------------------------------------------------------------


@pytest.fixture
def witnessed_ledger(tmp_path, stub_ts):
    ts_url, _received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    authored = seal(
        {"x": 1}, action="authored", operator="acme", anchor=False,
        ledger=ledger_path, witness_url=ts_url,
    ).capsule
    unclaimed = log(
        b"raw evidence", action="unclaimed", operator="acme",
        ledger=ledger_path, witness_url=ts_url,
    ).capsule

    from capsule_emit import push

    push(ledger_path, ts_url=ts_url)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)
    return ledger_path, authored, unclaimed


def test_bundle_of_a_signed_capsule_verifies_authored_with_no_authorship_notice(
    witnessed_ledger,
):
    ledger_path, authored, _unclaimed = witnessed_ledger
    b = bundle(ledger_path, authored["capsule_id"])
    ok, errors = verify_bundle(b)
    assert ok, errors
    assert not any("authorship" in e for e in errors)


def test_bundle_of_a_log_entry_verifies_ok_with_unclaimed_notice_not_invalid(
    witnessed_ledger,
):
    ledger_path, _authored, unclaimed = witnessed_ledger
    b = bundle(ledger_path, unclaimed["capsule_id"])
    ok, errors = verify_bundle(b)
    assert ok, errors
    assert any("log-verified, authorship not claimed" in e for e in errors)


def test_bundle_of_a_tampered_log_entry_content_is_still_caught_as_fatal(witnessed_ledger):
    """Mutant: tampering a log() entry's content must still flip verify_bundle
    to fatal (via the capsule_id/hash check) -- UNCLAIMED must never become a
    free pass for content tampering."""
    ledger_path, _authored, unclaimed = witnessed_ledger
    b = bundle(ledger_path, unclaimed["capsule_id"])
    b.receipt["operator"] = "attacker"
    ok, errors = verify_bundle(b)
    assert not ok
    assert any("receipt body was tampered" in e for e in errors)


def test_bundle_of_a_capsule_with_forged_signature_is_invalid_not_unclaimed(witnessed_ledger):
    """Mutant: corrupting ONLY the signature bytes (content + capsule_id
    untouched) on an otherwise-signed capsule must be graded INVALID, never
    silently reclassified as UNCLAIMED."""
    ledger_path, authored, _unclaimed = witnessed_ledger
    b = bundle(ledger_path, authored["capsule_id"])
    good_len = len(b.receipt["signature"])
    b.receipt["signature"] = "00" * (good_len // 2)
    ok, errors = verify_bundle(b)
    assert not ok
    assert any("forged" in e or "tampered" in e for e in errors)
    assert not any("authorship not claimed" in e for e in errors)
