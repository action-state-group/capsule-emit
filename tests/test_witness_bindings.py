# SPDX-License-Identifier: Apache-2.0
"""Witness bindings (``cll`` / ``rekor`` / ``scrapi``) and the plurality
policy -- [witness-plurality-bindings-and-directory-p2].

Rekor is mocked here with a local server that checks what the real one
checks for a ``dsse`` entry (the Ed25519 signature over the DSSE PAE) and
answers in the real response shape, with a Signed Entry Timestamp from a
test P-256 key. One golden test verifies a REAL public-Rekor entry's SET
under the shipped key, so the canonicalization is pinned against production
bytes, not only against our own mock. The live test against
``rekor.sigstore.dev`` is opt-in (``CAPSULE_EMIT_TEST_LIVE_REKOR=1``).
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import threading
from pathlib import Path

import pytest
from _stub_receipt import (
    TEST_TS_PRIVATE_KEY_PEM,
    TEST_TS_PUBLIC_KEY_PEM,
    build_stub_receipt_b64,
    checkpoint_dict_from_cose,
    checkpoint_entry_hash,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_public_key,
)
from scitt_cose import build_receipt

import capsule_emit.core as core
from capsule_emit import seal, witness
from capsule_emit import witness_bindings as wb

FIXTURES = Path(__file__).parent / "fixtures"

# -- a Rekor double -----------------------------------------------------------

_REKOR_KEY = ec.generate_private_key(ec.SECP256R1())
REKOR_TEST_PUBLIC_KEY_PEM = _REKOR_KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
)
_REKOR_LOG_ID = hashlib.sha256(
    _REKOR_KEY.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
).hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def rekor_entry_for(envelope: dict, verifier_pem_b64: str, log_index: int, *, sign_key=_REKOR_KEY):
    """What Rekor returns for an accepted dsse entry: body = the canonical
    stored entry (hashes only), plus a SET over {body, integratedTime, logID,
    logIndex}."""
    payload = base64.b64decode(envelope["payload"])
    stored = {
        "apiVersion": "0.0.1",
        "kind": "dsse",
        "spec": {
            "envelopeHash": {
                "algorithm": "sha256",
                "value": hashlib.sha256(_canonical(envelope)).hexdigest(),
            },
            "payloadHash": {"algorithm": "sha256", "value": hashlib.sha256(payload).hexdigest()},
            "signatures": [
                {"signature": s["sig"], "verifier": verifier_pem_b64} for s in envelope["signatures"]
            ],
        },
    }
    body_b64 = base64.b64encode(_canonical(stored)).decode()
    signed = {"body": body_b64, "integratedTime": 1790000000, "logID": _REKOR_LOG_ID, "logIndex": log_index}
    set_sig = sign_key.sign(_canonical(signed), ec.ECDSA(hashes.SHA256()))
    return {
        **signed,
        "verification": {
            "signedEntryTimestamp": base64.b64encode(set_sig).decode(),
            "inclusionProof": {"treeSize": log_index + 1, "logIndex": log_index, "hashes": []},
        },
    }


class _RekorHandler(http.server.BaseHTTPRequestHandler):
    entries: list = []
    fail_with: list = []

    def log_message(self, *_a):
        pass

    def do_POST(self):
        if self.path != "/api/v1/log/entries":
            self.send_response(404)
            self.end_headers()
            return
        if self.fail_with:
            self.send_response(self.fail_with[0])
            self.end_headers()
            self.wfile.write(b'{"code":400,"message":"refused"}')
            return
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert req["kind"] == "dsse" and req["apiVersion"] == "0.0.1"
        pc = req["spec"]["proposedContent"]
        envelope = json.loads(pc["envelope"])
        key = load_pem_public_key(base64.b64decode(pc["verifiers"][0]))
        payload = base64.b64decode(envelope["payload"])
        # What Rekor does: verify every signature over the DSSE PAE.
        for s in envelope["signatures"]:
            key.verify(base64.b64decode(s["sig"]), wb.dsse_pae(envelope["payloadType"], payload))
        entry = rekor_entry_for(envelope, pc["verifiers"][0], len(self.entries))
        uuid = hashlib.sha256(payload).hexdigest()
        self.entries.append((uuid, entry, payload))
        out = json.dumps({uuid: entry}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class _CllHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        body = checkpoint_dict_from_cose(raw)
        entry_hash = checkpoint_entry_hash(body)
        out = json.dumps(
            {"entry_hash": entry_hash, "receipt_b64": build_stub_receipt_b64(entry_hash), "leaf_index": 0, "tree_size": 1}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class _ScrapiHandler(http.server.BaseHTTPRequestHandler):
    """SCRAPI double: 202 + Location, then the receipt on the poll."""

    pending: dict = {}

    def log_message(self, *_a):
        pass

    def do_POST(self):
        statement = self.rfile.read(int(self.headers["Content-Length"]))
        assert self.headers["Content-Type"] == "application/cose"
        leaf = hashlib.sha256(statement).hexdigest()
        self.pending["op1"] = build_receipt(
            leaf_entry_hex=leaf,
            leaf_index=0,
            tree_entries_hex=[leaf],
            alg="EdDSA",
            log_private_key_pem=TEST_TS_PRIVATE_KEY_PEM,
        )
        self.send_response(202)
        self.send_header("Location", "/operations/op1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        receipt = self.pending.get(self.path.rsplit("/", 1)[-1])
        if receipt is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/cose")
        self.send_header("Content-Length", str(len(receipt)))
        self.end_headers()
        self.wfile.write(receipt)


def _serve(handler, **attrs):
    cls = type("_Bound" + handler.__name__, (handler,), {k: v for k, v in attrs.items()})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


@pytest.fixture
def servers():
    started = []

    def start(handler, **attrs):
        url, srv = _serve(handler, **attrs)
        started.append(srv)
        return url

    yield start
    for srv in started:
        srv.shutdown()


@pytest.fixture(autouse=True)
def _clean_witness_state():
    for d in (witness._counts, witness._armed_at, witness._states, witness._dispatch_locks):
        d.clear()
    witness._notice_printed = False
    core._disclosure_printed = False
    yield
    for d in (witness._counts, witness._armed_at, witness._states, witness._dispatch_locks):
        d.clear()


def _pushed(tmp_path, urls):
    ledger = str(tmp_path / "ledger.jsonl")
    for i in range(3):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness=False)
    cp = witness.push(ledger, ts_url=urls, witness=True)
    states = witness.checkpoint_witness_states(ledger)
    return ledger, cp, states[-1]


# -- binding selection --------------------------------------------------------


def test_binding_is_named_by_the_url_scheme():
    assert wb.binding_of("https://witness.example") == "cll"
    assert wb.binding_of("rekor+https://rekor.sigstore.dev") == "rekor"
    assert wb.binding_of("SCRAPI+https://ts.example/") == "scrapi"
    assert wb.endpoint_of("rekor+https://rekor.sigstore.dev/") == "https://rekor.sigstore.dev"
    assert wb.endpoint_of("https://witness.example/") == "https://witness.example"


def test_operator_defaults_to_host_and_directory_overrides():
    assert wb.operator_of("rekor+https://rekor.sigstore.dev") == "rekor.sigstore.dev"
    assert wb.operator_of("https://a.example:8443") == "a.example"
    assert wb.operator_of("https://a.example", {"https://a.example": "Operator A"}) == "Operator A"


def test_the_shipped_rekor_log_id_is_the_shipped_key():
    key = load_pem_public_key(wb.PUBLIC_REKOR_PUBLIC_KEY_PEM)
    der = key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    assert hashlib.sha256(der).hexdigest() == wb.PUBLIC_REKOR_LOG_ID


# -- golden: a real public-Rekor entry ---------------------------------------


def test_set_canonicalization_matches_a_real_public_rekor_entry():
    """logIndex 100000000 on rekor.sigstore.dev, fetched 2026-09-27. Its SET
    verifies under the shipped key; the entry is not ours, so the binding
    check (step 2) is what refuses it -- proving step 1 passed."""
    raw = json.loads((FIXTURES / "rekor_public_entry_100000000.json").read_text())
    uuid, entry = next(iter(raw.items()))
    ok, reason = wb.verify_rekor_receipt(
        {"uuid": uuid, **entry}, checkpoint_cose=b"not this entry", checkpoint_key_id="00" * 32
    )
    assert not ok
    assert "signedEntryTimestamp" not in reason and "logID" not in reason, reason


def test_a_tampered_real_entry_fails_the_set():
    raw = json.loads((FIXTURES / "rekor_public_entry_100000000.json").read_text())
    uuid, entry = next(iter(raw.items()))
    entry = {**entry, "integratedTime": entry["integratedTime"] + 1}
    ok, reason = wb.verify_rekor_receipt(
        {"uuid": uuid, **entry}, checkpoint_cose=b"x", checkpoint_key_id="00" * 32
    )
    assert not ok and "signedEntryTimestamp does not verify" in reason


# -- rekor end to end through push() -----------------------------------------


def test_push_to_cll_and_rekor_gives_two_receipts_two_operators(tmp_path, servers):
    cll_url = servers(_CllHandler)
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _ledger, cp, state = _pushed(tmp_path, [cll_url, rekor_url])

    assert {w.ts_url for w in cp.witnesses} == {cll_url, rekor_url}
    result = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        policy=wb.WitnessPolicy(min_receipts=2, distinct_operators=True),
        keys={cll_url: TEST_TS_PUBLIC_KEY_PEM, rekor_url: REKOR_TEST_PUBLIC_KEY_PEM},
        directory={cll_url: "Witness Operator A", rekor_url: "Rekor Operator B"},
    )
    by_binding = {v.binding: v for v in result.receipts}
    assert by_binding["rekor"].verified, by_binding["rekor"].reason
    assert by_binding["rekor"].grade == "countersigned-observed"
    assert by_binding["cll"].verified, by_binding["cll"].reason
    assert result.counted == 2 and result.operators == 2 and result.policy_met
    assert result.summary() == "2 witnesses · 2 operators"


def test_rekor_receipt_is_never_mmr_verified(tmp_path, servers):
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _l, _cp, state = _pushed(tmp_path, [rekor_url])
    result = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        keys={rekor_url: REKOR_TEST_PUBLIC_KEY_PEM},
    )
    assert [v.grade for v in result.receipts] == ["countersigned-observed"]


def test_rekor_receipt_under_an_unpinned_key_does_not_count(tmp_path, servers):
    """Without the test key pinned, the verifier uses the public-instance
    key -- our mock's SET is not Rekor's, so it must not count."""
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _l, _cp, state = _pushed(tmp_path, [rekor_url])
    result = wb.verify_witnesses(
        state.checkpoint, state.effective_witnesses, checkpoint_cose_hex=state.checkpoint_cose_hex
    )
    assert result.counted == 0 and not result.policy_met
    assert "logID" in result.receipts[0].reason


def test_rekor_receipt_replayed_onto_another_checkpoint_fails(tmp_path, servers):
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _l, _cp, state = _pushed(tmp_path, [rekor_url])
    receipt = json.loads(base64.b64decode(state.effective_witnesses[rekor_url].receipt_b64))
    other_cose = bytes.fromhex(state.checkpoint_cose_hex) + b"\x00"
    ok, reason = wb.verify_rekor_receipt(
        receipt,
        checkpoint_cose=other_cose,
        checkpoint_key_id=state.checkpoint.key_id,
        rekor_public_key_pem=REKOR_TEST_PUBLIC_KEY_PEM,
    )
    assert not ok and "payloadHash" in reason


def test_our_bytes_logged_under_someone_elses_key_do_not_count(tmp_path, servers):
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _l, _cp, state = _pushed(tmp_path, [rekor_url])
    cose = bytes.fromhex(state.checkpoint_cose_hex)
    stranger = Ed25519PrivateKey.generate()
    from capsule_emit.checkpoint.cose_wire import CLL_CHECKPOINT_CONTENT_TYPE

    envelope = {
        "payloadType": CLL_CHECKPOINT_CONTENT_TYPE,
        "payload": base64.b64encode(cose).decode(),
        "signatures": [
            {"keyid": "", "sig": base64.b64encode(stranger.sign(wb.dsse_pae(CLL_CHECKPOINT_CONTENT_TYPE, cose))).decode()}
        ],
    }
    pem_b64 = base64.b64encode(
        stranger.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    ).decode()
    receipt = {"uuid": "x", **rekor_entry_for(envelope, pem_b64, 7)}
    ok, reason = wb.verify_rekor_receipt(
        receipt,
        checkpoint_cose=cose,
        checkpoint_key_id=state.checkpoint.key_id,
        rekor_public_key_pem=REKOR_TEST_PUBLIC_KEY_PEM,
    )
    assert not ok and "checkpoint's key" in reason


def test_forged_set_fails(tmp_path, servers):
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _l, _cp, state = _pushed(tmp_path, [rekor_url])
    receipt = json.loads(base64.b64decode(state.effective_witnesses[rekor_url].receipt_b64))
    forger = ec.generate_private_key(ec.SECP256R1())
    signed = {k: receipt[k] for k in ("body", "integratedTime", "logID", "logIndex")}
    receipt["verification"]["signedEntryTimestamp"] = base64.b64encode(
        forger.sign(_canonical(signed), ec.ECDSA(hashes.SHA256()))
    ).decode()
    ok, reason = wb.verify_rekor_receipt(
        receipt,
        checkpoint_cose=bytes.fromhex(state.checkpoint_cose_hex),
        checkpoint_key_id=state.checkpoint.key_id,
        rekor_public_key_pem=REKOR_TEST_PUBLIC_KEY_PEM,
    )
    assert not ok and "signedEntryTimestamp" in reason


def test_rekor_refusal_never_blocks_the_other_witness_and_is_retried(tmp_path, servers):
    cll_url = servers(_CllHandler)
    entries: list = []
    fail = [400]
    rekor_url = "rekor+" + servers(_RekorHandler, entries=entries, fail_with=fail)
    with pytest.warns(RuntimeWarning, match="did not complete"):
        ledger, cp, _state = _pushed(tmp_path, [cll_url, rekor_url])
    assert [w.ts_url for w in cp.witnesses] == [cll_url]
    assert len(witness.checkpoint_witness_backlog(ledger, [rekor_url])[rekor_url]) == 1

    fail.clear()
    signer = witness._states[witness._resolve_key(ledger)].signer
    # Without the signer, a rekor backlog is left alone (not a failure).
    assert witness.retry_pending_witness_stamps(ledger, ts_url=[rekor_url], enabled=True) == {rekor_url: 0}
    assert witness.retry_pending_witness_stamps(
        ledger, ts_url=[rekor_url], enabled=True, sign=signer.sign_bytes
    ) == {rekor_url: 1}
    assert witness.checkpoint_witness_backlog(ledger, [rekor_url])[rekor_url] == []
    assert len(entries) == 1


# -- scrapi -------------------------------------------------------------------


def test_scrapi_registration_polls_and_verifies_under_the_pinned_key(tmp_path, servers):
    scrapi_url = "scrapi+" + servers(_ScrapiHandler, pending={})
    _l, cp, state = _pushed(tmp_path, [scrapi_url])
    assert [w.ts_url for w in cp.witnesses] == [scrapi_url]

    pinned = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        keys={scrapi_url: TEST_TS_PUBLIC_KEY_PEM},
    )
    assert pinned.counted == 1, pinned.receipts
    assert pinned.receipts[0].grade == "countersigned-observed"

    unpinned = wb.verify_witnesses(
        state.checkpoint, state.effective_witnesses, checkpoint_cose_hex=state.checkpoint_cose_hex
    )
    assert unpinned.counted == 0
    assert unpinned.receipts[0].reason.startswith("not checked")


def test_scrapi_receipt_under_a_different_key_does_not_count(tmp_path, servers):
    scrapi_url = "scrapi+" + servers(_ScrapiHandler, pending={})
    _l, _cp, state = _pushed(tmp_path, [scrapi_url])
    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    result = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        keys={scrapi_url: other},
    )
    assert result.counted == 0


# -- policy -------------------------------------------------------------------


def _three_witness_state(tmp_path, servers):
    cll_a = servers(_CllHandler)
    cll_b = servers(_CllHandler)
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _l, _cp, state = _pushed(tmp_path, [cll_a, cll_b, rekor_url])
    keys = {cll_a: TEST_TS_PUBLIC_KEY_PEM, cll_b: TEST_TS_PUBLIC_KEY_PEM, rekor_url: REKOR_TEST_PUBLIC_KEY_PEM}
    return state, (cll_a, cll_b, rekor_url), keys


def test_k_of_n(tmp_path, servers):
    state, _urls, keys = _three_witness_state(tmp_path, servers)
    directory = {u: f"op{i}" for i, u in enumerate(state.effective_witnesses)}
    for k, met in ((1, True), (3, True), (4, False)):
        r = wb.verify_witnesses(
            state.checkpoint,
            state.effective_witnesses,
            checkpoint_cose_hex=state.checkpoint_cose_hex,
            policy=wb.WitnessPolicy(min_receipts=k),
            keys=keys,
            directory=directory,
        )
        assert r.counted == 3 and r.policy_met is met, (k, r)


def test_distinct_operators_counts_operators_not_receipts(tmp_path, servers):
    state, (cll_a, cll_b, rekor_url), keys = _three_witness_state(tmp_path, servers)
    same_op = {cll_a: "Operator A", cll_b: "Operator A", rekor_url: "Operator B"}
    strict = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        policy=wb.WitnessPolicy(min_receipts=3, distinct_operators=True),
        keys=keys,
        directory=same_op,
    )
    assert strict.counted == 3 and strict.operators == 2 and not strict.policy_met
    loose = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        policy=wb.WitnessPolicy(min_receipts=3, distinct_operators=False),
        keys=keys,
        directory=same_op,
    )
    assert loose.policy_met


def test_one_bad_receipt_is_reported_and_not_counted(tmp_path, servers):
    state, (cll_a, _cll_b, _rekor_url), keys = _three_witness_state(tmp_path, servers)
    from dataclasses import replace

    tampered = dict(state.effective_witnesses)
    good = tampered[cll_a]
    tampered[cll_a] = replace(good, receipt_b64=base64.b64encode(b"\x00" * 40).decode())
    directory = {u: f"op{i}" for i, u in enumerate(tampered)}
    r = wb.verify_witnesses(
        state.checkpoint,
        tampered,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        policy=wb.WitnessPolicy(min_receipts=2),
        keys=keys,
        directory=directory,
    )
    bad = [v for v in r.receipts if v.ts_url == cll_a][0]
    assert not bad.verified and bad.grade is None and bad.reason
    assert r.counted == 2 and r.policy_met


def test_stub_receipts_never_count(tmp_path):
    from capsule_emit.checkpoint import register_checkpoint_stub

    ledger = str(tmp_path / "l.jsonl")
    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness=False)
    cp = witness.push(ledger, ts_url=["https://unused.example"], witness=False)
    assert cp is None  # witnessing off -> nothing built; use a stub record directly
    state = witness._get_state(ledger)
    state.mmr.sync()
    from capsule_emit.checkpoint import emit_checkpoint

    record = emit_checkpoint(state.mmr, state.signer, log_id=state.log_id)
    stub = register_checkpoint_stub(record)
    r = wb.verify_witnesses(record, [stub], policy=wb.WitnessPolicy(min_receipts=1))
    assert r.counted == 0 and not r.policy_met and "stub" in r.receipts[0].reason


def test_summary_is_counts_never_names():
    r = wb.PluralityResult(policy=wb.WitnessPolicy(), counted=1, operators=1, policy_met=True)
    assert r.summary() == "1 witness · 1 operator"


# -- live (opt-in, nightly) ---------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("CAPSULE_EMIT_TEST_LIVE_REKOR") != "1",
    reason="opt-in live test against rekor.sigstore.dev -- set CAPSULE_EMIT_TEST_LIVE_REKOR=1",
)
def test_live_public_rekor_accepts_and_verifies_a_checkpoint(tmp_path):
    """Writes ONE content-free entry (a throwaway log's checkpoint: size,
    root, time, key id) to the public Rekor log, then verifies it offline
    under the shipped key."""
    ledger, cp, state = _pushed(tmp_path, [wb.PUBLIC_REKOR_URL])
    assert [w.ts_url for w in cp.witnesses] == [wb.PUBLIC_REKOR_URL]
    r = wb.verify_witnesses(
        state.checkpoint, state.effective_witnesses, checkpoint_cose_hex=state.checkpoint_cose_hex
    )
    assert r.counted == 1, r.receipts
