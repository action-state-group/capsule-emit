# SPDX-License-Identifier: Apache-2.0
"""Permanent regression tests for the [verify-authenticates-nothing] PM
escalation (2026-08-24): an adversarial run against ``origin/main``
(``_work/adv-migration-run-2026-08-24.md``) found the offline read/verify
surface authenticated almost nothing -- structure was checked, cryptography
was not. The original attack scripts lived at ``/tmp/atk/*.py`` in that run
and are gone; this file reconstructs the three repros from the run report as
permanent in-tree tests, one per closed finding:

- BLOCKER-1 / [verify-checks-producer-signature] --
  ``attack_forge_sig.py``: a key-less forgery (attacker-authored content,
  invented signature/key_id, ``capsule_id`` recomputed to match) reported
  ``1/1 VALID``.
- HIGH-2 / [bundle-authenticates-receipt-and-stamp] -- ``attack6b.py``: a
  bundle with its receipt BODY tampered but ``capsule_id`` left alone
  verified ``(True, [])``.
- HIGH-3 / [stamp-authenticity-on-read-not-presence] -- ``attack45.py``
  §ATTACK 5: a hand-written checkpoint-stamp entry with a fabricated
  ``witnesses`` array (no TS ever contacted, ``receipt_b64="forged"``)
  graded ``witnessed`` and passed ``verify_bundle``/``status --offline``.
"""
from __future__ import annotations

import hashlib
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
from agent_action_capsule.canonical import compute_capsule_id

from capsule_emit import cli, seal, status, witness
from capsule_emit import ledger as ledger_mod
from capsule_emit.bundle import bundle, verify_bundle
from capsule_emit.checkpoint import emit as checkpoint_emit_mod
from capsule_emit.checkpoint.emit import CheckpointRecord
from capsule_emit.signing import verify_capsule_signature, verify_store_signed

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service -- same shape as tests/test_bundle.py
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
    # Simulate that this hermetic stub IS the pinned default witness
    # ([verify-batch-fastfollow] item D) so fixtures built with it still
    # signature-verify as WITNESSED via the DEFAULT (no-key) read path,
    # instead of correctly-but-inconveniently demoting to "TS identity
    # unverified" for being an unpinned TS. monkeypatch reverts per test.
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
# BLOCKER-1 -- attack_forge_sig.py: key-less forgery reported 1/1 VALID
# ---------------------------------------------------------------------------


def _forge_capsule(genuine: dict) -> dict:
    """The exact forgery from the run report: rewrite content, invent a
    signature and key_id (no private key needed), recompute ``capsule_id``
    over the forged content so it stays internally consistent."""
    forged = dict(genuine)
    forged["operator"] = "ATTACKER-INC"
    forged["signature"] = "00" * 64
    forged["key_id"] = "11" * 32
    forged.pop("capsule_id", None)
    forged["capsule_id"] = compute_capsule_id(forged)
    return forged


def test_attack_forge_sig_reports_invalid_naming_the_record(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    result = seal(
        None, action="legit", operator="acme", anchor=False,
        ledger=ledger_path, witness_url="http://127.0.0.1:1",
    )
    genuine = dict(result.capsule)
    forged = _forge_capsule(genuine)

    forged_ledger = tmp_path / "forged_ledger.jsonl"
    forged_ledger.write_text(json.dumps(forged) + "\n")

    results = verify_store_signed([forged])
    assert results[0].ok is False
    assert any(f.code == "producer_signature_invalid" for f in results[0].findings)
    assert any(forged["capsule_id"] in f.detail for f in results[0].findings)

    # The genuine, unforged capsule must still verify -- a positive control
    # so this test would fail if the new check were over-broad.
    assert verify_store_signed([genuine])[0].ok is True

    # CLI exercise, per acceptance: "CLI capsule-emit verify exercises it".
    rc = cli.main(["verify", "--store", str(forged_ledger)])
    assert rc == 1


def test_attack_forge_sig_cli_output_names_the_record(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.jsonl"
    result = seal(
        None, action="legit", operator="acme", anchor=False,
        ledger=ledger_path, witness_url="http://127.0.0.1:1",
    )
    forged = _forge_capsule(dict(result.capsule))
    forged_ledger = tmp_path / "forged_ledger.jsonl"
    forged_ledger.write_text(json.dumps(forged) + "\n")

    rc = cli.main(["verify", "--store", str(forged_ledger)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "INVALID" in out
    assert forged["capsule_id"] in out
    assert "0/1 VALID" in out


def test_verify_capsule_signature_catches_tampered_payload_reusing_old_signature(tmp_path):
    """Mutation test, isolating verify_capsule_signature (§7): an attacker
    who changes content but reuses the ORIGINAL producer's signature/key_id
    verbatim (never even bothers inventing new ones) must fail -- the
    signature no longer matches the new content digest."""
    result = seal(None, action="legit", operator="acme", anchor=False,
                   ledger=tmp_path / "ledger.jsonl", witness_url="http://127.0.0.1:1")
    genuine = dict(result.capsule)
    assert verify_capsule_signature(genuine) is True  # positive control

    tampered_payload = dict(genuine)
    tampered_payload["operator"] = "ATTACKER-INC"
    tampered_payload.pop("capsule_id", None)
    tampered_payload["capsule_id"] = compute_capsule_id(tampered_payload)
    assert verify_capsule_signature(tampered_payload) is False


def test_verify_capsule_signature_catches_tampered_signature_bytes(tmp_path):
    """Mutation test: signature bytes altered (content and key_id left
    alone) must fail."""
    result = seal(None, action="legit", operator="acme", anchor=False,
                   ledger=tmp_path / "ledger.jsonl", witness_url="http://127.0.0.1:1")
    genuine = dict(result.capsule)

    tampered_sig = dict(genuine)
    original = tampered_sig["signature"]
    flipped = ("00" if original[:2] != "00" else "11") + original[2:]
    tampered_sig["signature"] = flipped
    assert verify_capsule_signature(tampered_sig) is False


# ---------------------------------------------------------------------------
# HIGH-2 -- attack6b.py: tampered receipt body, capsule_id kept, verified True
# ---------------------------------------------------------------------------


@pytest.fixture
def two_checkpoint_ledger(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, _received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    caps = []
    for i in range(2):
        result = seal(None, action=f"first-{i}", operator="acme", anchor=False,
                       ledger=ledger_path, witness_url=ts_url)
        caps.append(result.capsule)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    for i in range(2):
        result = seal(None, action=f"second-{i}", operator="acme", anchor=False,
                       ledger=ledger_path, witness_url=ts_url)
        caps.append(result.capsule)
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 2)

    return ledger_path, caps


def test_attack6b_tampered_receipt_body_reports_invalid_naming_the_receipt(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[0]["capsule_id"])

    # The attack: rewrite the receipt body but keep capsule_id unchanged, so
    # a verifier that only compares two labels (not recomputing content)
    # never notices.
    tampered_receipt = dict(b.receipt)
    tampered_receipt["operator"] = "ATTACKER-INC"
    mutant = replace(b, receipt=tampered_receipt)

    ok, errors = verify_bundle(mutant)
    assert ok is False
    assert any("does not hash to its own capsule_id" in e for e in errors)
    assert any(b.capsule_id in e for e in errors)


def test_attack6b_positive_control_untampered_bundle_still_verifies(two_checkpoint_ledger):
    ledger_path, caps = two_checkpoint_ledger
    b = bundle(ledger_path, caps[0]["capsule_id"])
    ok, errors = verify_bundle(b)
    assert ok, errors


# ---------------------------------------------------------------------------
# HIGH-3 -- attack45.py §ATTACK 5: file-forged stamp graded witnessed
# ---------------------------------------------------------------------------


def _rewrite_ledger_line(ledger_path, predicate, mutate) -> None:
    """File-level forger primitive: rewrite the one raw JSONL line matching
    ``predicate`` by applying ``mutate`` to its parsed dict -- exactly what
    an attacker with filesystem access to the ledger, but no producer or TS
    key, can do."""
    lines = ledger_path.read_text().splitlines()
    out = []
    matched = False
    for line in lines:
        entry = json.loads(line)
        if not matched and predicate(entry):
            entry = mutate(entry)
            matched = True
        out.append(json.dumps(entry))
    assert matched, "no ledger line matched the forger's predicate"
    ledger_path.write_text("\n".join(out) + "\n")


def _forge_witness_into_stamp(entry: dict) -> dict:
    entry = json.loads(json.dumps(entry))  # deep copy
    entry["checkpoint"]["witnesses"] = [
        {
            "ts_url": "https://attacker.example",
            "entry_hash": "ab" * 32,
            "receipt_b64": "forged",
            "leaf_index": 0,
            "tree_size": 1,
        }
    ]
    return entry


def test_attack45_file_forged_stamp_grades_self_attested_not_witnessed(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"
    for i in range(2):
        seal(None, action=f"lonely-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url="http://127.0.0.1:1")  # nothing listens -- self-attested
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    entries = ledger_mod.read_ledger_entries(ledger_path)
    stamp = next(e for e in entries if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND)
    cp_before = CheckpointRecord.from_dict(stamp["checkpoint"])
    assert cp_before.witnesses == []  # genuinely self-attested before the forgery

    _rewrite_ledger_line(
        ledger_path,
        predicate=lambda e: e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND,
        mutate=_forge_witness_into_stamp,
    )

    # (a) grade(): the forged checkpoint, re-read fresh, must not launder to
    # WITNESSED.
    entries_after = ledger_mod.read_ledger_entries(ledger_path)
    stamp_after = next(e for e in entries_after if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND)
    cp_after = CheckpointRecord.from_dict(stamp_after["checkpoint"])
    assert len(cp_after.witnesses) == 1  # the forger's witness IS present...
    from capsule_emit.checkpoint import Grade

    assert cp_after.grade() == Grade.SELF_ATTESTED  # ...but presence alone no longer counts

    # (b) status --offline: the headline grade must stay honest, no network.
    result = status.compute_status(str(ledger_path), offline=True)
    assert result["latest_checkpoint"]["grade"] == "self-attested"

    # (c) bundle/verify_bundle: covers a record with this checkpoint and
    # must not report VALID over an all-forged witnesses list.
    record_id = ledger_mod.read_ledger(ledger_path)[0]["capsule_id"]
    b = bundle(ledger_path, record_id)
    ok, errors = verify_bundle(b)
    assert ok is False
    assert any("witness stamp" in e and "INVALID" in e for e in errors)
    assert any("none verify as authentic TS Receipts" in e for e in errors)


def test_attack45_positive_control_genuine_stamp_still_grades_witnessed(two_checkpoint_ledger):
    """A checkpoint that went through the real seal()/witness pipeline (not
    file-forged) must still grade witnessed -- otherwise the fix is
    over-broad, not just closing the hole."""
    ledger_path, caps = two_checkpoint_ledger
    entries = ledger_mod.read_ledger_entries(ledger_path)
    stamp = next(e for e in entries if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND)
    cp = CheckpointRecord.from_dict(stamp["checkpoint"])
    assert len(cp.witnesses) == 1
    from capsule_emit.checkpoint import Grade

    assert cp.grade() == Grade.WITNESSED

    result = status.compute_status(str(ledger_path), offline=True)
    assert result["latest_checkpoint"]["grade"] == "witnessed"


# ---------------------------------------------------------------------------
# [verify-batch-fastfollow] item D -- manager-review finding: the
# sophisticated file-forger one level up from attack45. attack45's forger
# writes garbage receipt_b64 ("forged") and is caught by the structural
# probe. This forger instead runs the PUBLIC scitt_cose.build_receipt (no
# producer/TS key needed), computes the correct entry_hash from the
# checkpoint they are editing, and signs a real, structurally valid,
# checkpoint-bound receipt with a key THEY generated. Before pinning the
# known default witness's key, this passed the (key-independent) structural
# probe and laundered self-attested -> witnessed -- proven by attack45's own
# positive control, which graded WITNESSED over a stub-signed receipt with
# no pinned key for the identical, key-independent reason. Pinning closes it:
# an attacker's key is neither the pinned default nor caller-supplied, so it
# can never satisfy the identity check, only the (now-insufficient) shape one.
# ---------------------------------------------------------------------------


def test_attack_sophisticated_forger_correct_entry_hash_attacker_key_stays_self_attested(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"
    for i in range(2):
        seal(None, action=f"lonely-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url="http://127.0.0.1:1")  # nothing listens -- self-attested
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    entries = ledger_mod.read_ledger_entries(ledger_path)
    stamp = next(e for e in entries if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND)
    cp = CheckpointRecord.from_dict(stamp["checkpoint"])
    assert cp.witnesses == []  # genuinely self-attested before the forgery

    # The sophisticated forger: correct entry_hash (computable from the
    # checkpoint they're editing -- no secret needed), a REAL COSE Receipt
    # (not garbage bytes), signed with a key of the attacker's own choosing.
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    from scitt_cose import build_receipt

    attacker_key_pem = Ed25519PrivateKey.generate().private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    entry_hash = hashlib.sha256(bytes.fromhex(cp.digest())).hexdigest()
    forged_receipt_b64 = base64.b64encode(
        build_receipt(
            leaf_entry_hex=entry_hash, leaf_index=0, tree_entries_hex=[entry_hash],
            alg="EdDSA", log_private_key_pem=attacker_key_pem,
        )
    ).decode()

    def _sophisticated_forge(entry):
        entry = json.loads(json.dumps(entry))  # deep copy
        entry["checkpoint"]["witnesses"] = [{
            "ts_url": "https://attacker.example",  # never contacted -- attacker invents this
            "entry_hash": entry_hash,
            "receipt_b64": forged_receipt_b64,
            "leaf_index": 0,
            "tree_size": 1,
        }]
        return entry

    _rewrite_ledger_line(
        ledger_path,
        predicate=lambda e: e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND,
        mutate=_sophisticated_forge,
    )

    # (a) grade(): a real, structurally valid, checkpoint-bound receipt --
    # not garbage -- must still not launder to WITNESSED without the pinned
    # default witness's key or an identity match.
    entries_after = ledger_mod.read_ledger_entries(ledger_path)
    stamp_after = next(e for e in entries_after if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND)
    cp_after = CheckpointRecord.from_dict(stamp_after["checkpoint"])
    assert len(cp_after.witnesses) == 1  # the forger's witness IS present, and IS a real receipt...
    from capsule_emit.checkpoint import Grade

    assert cp_after.grade() == Grade.SELF_ATTESTED  # ...but an attacker-chosen key never confers WITNESSED

    # (b) status --offline: the headline grade must stay honest, no network.
    result = status.compute_status(str(ledger_path), offline=True)
    assert result["latest_checkpoint"]["grade"] == "self-attested"

    # (c) bundle/verify_bundle -- REVISED by [verify-threestate-trustanchor]
    # (supersedes the two-state assertion this test used to make): with no
    # trust_anchor supplied, an unpinned ts_url is cryptographically
    # indistinguishable from a legitimate self-hosted/zero-egress TS the
    # caller simply hasn't pinned yet -- the bundle must NOT be INVALID over
    # that ambiguity alone (§1a.2 honesty), it must render the stamp as an
    # honest, non-fatal "unverified" notice. A caller who actually wants
    # the identity-bound guarantee for this ts_url passes it via
    # trust_anchor -- proven below, where the SAME attacker-key stamp fails
    # closed once its (invented) ts_url is pinned to a DIFFERENT key.
    record_id = ledger_mod.read_ledger(ledger_path)[0]["capsule_id"]
    b = bundle(ledger_path, record_id)
    ok, errors = verify_bundle(b)
    assert ok is True
    assert any("pin not supplied" in e and "unverified stamp" in e for e in errors)

    # If the caller DOES supply a trust anchor for this ts_url, the same
    # attacker-signed stamp must fail closed as INVALID (forgery under a
    # known pin) -- proving the trust_anchor param actually gates identity.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as _Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding as _Encoding
    from cryptography.hazmat.primitives.serialization import PublicFormat as _PublicFormat

    operators_real_pubkey_pem = _Ed25519PrivateKey.generate().public_key().public_bytes(
        _Encoding.PEM, _PublicFormat.SubjectPublicKeyInfo
    )
    ok_pinned, errors_pinned = verify_bundle(
        b, trust_anchor={"https://attacker.example": operators_real_pubkey_pem}
    )
    assert ok_pinned is False
    assert any("witness stamp" in e and "INVALID" in e for e in errors_pinned)
