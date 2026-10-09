# SPDX-License-Identifier: Apache-2.0
"""Witness bindings (``cll`` / ``rekor`` / ``scrapi``) and the plurality
policy.

Rekor is mocked here with a local server that checks what the real one
checks for a ``dsse`` entry (the Ed25519 signature over the DSSE PAE) and
answers in the real response shape, with a Signed Entry Timestamp from a
test P-256 key. One golden test verifies a REAL public-Rekor entry's SET
under the key the committed ``witnesses.json`` lists for that log, so the
canonicalization is pinned against production bytes, not only against our
own mock. The live test against ``rekor.sigstore.dev`` is opt-in
(``CAPSULE_EMIT_TEST_LIVE_REKOR=1``).

Every verification reads its keys from a witness directory; no test pins a
key any other way, because ``verify_witnesses`` has no other way.
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
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_public_key,
)
from scitt_cose import build_receipt

import capsule_emit.core as core
from capsule_emit import seal, witness
from capsule_emit import witness_bindings as wb
from capsule_emit import witness_directory as wd

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


# -- directory helpers: every test builds rows the same way -------------------

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = json.loads((ROOT / "witnesses.json").read_text())


def _row(name, url, pem, *, binding=None):
    """A directory row for witness ``url`` whose key is ``pem`` -- the shape a
    real row has: raw Ed25519 key as key_id, or SHA-256(DER) + public_keys."""
    key = load_pem_public_key(pem)
    row = {"name": name, "endpoint": wb.endpoint_of(url), "since": "2026-01-01",
           "binding": binding or wb.binding_of(url)}
    if isinstance(key, Ed25519PublicKey):
        row["key_ids"] = [key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()]
    else:
        der = key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        row["key_ids"] = [hashlib.sha256(der).hexdigest()]
        row["public_keys"] = [base64.b64encode(der).decode()]
    return row


def _dir(*rows):
    return {"directory_version": "1", "witnesses": sorted(rows, key=lambda r: (r["name"].casefold(), r["endpoint"]))}


def _committed_pem(name):
    row = next(r for r in COMMITTED["witnesses"] if r["name"] == name)
    return wd.row_public_keys_pem(row)[0]


# -- binding selection --------------------------------------------------------


def test_binding_is_named_by_the_url_scheme():
    assert wb.binding_of("https://witness.example") == "cll"
    assert wb.binding_of("rekor+https://rekor.sigstore.dev") == "rekor"
    assert wb.binding_of("SCRAPI+https://ts.example/") == "scrapi"
    assert wb.endpoint_of("rekor+https://rekor.sigstore.dev/") == "https://rekor.sigstore.dev"
    assert wb.endpoint_of("https://witness.example/") == "https://witness.example"


def test_operator_is_the_row_name_else_the_host():
    directory = _dir(_row("Operator A", "https://a.example", TEST_TS_PUBLIC_KEY_PEM))
    assert wb.operator_of("https://a.example", directory) == "Operator A"
    assert wb.operator_of("https://a.example/", directory) == "Operator A"
    # same endpoint, different binding: not that row
    assert wb.operator_of("scrapi+https://a.example", directory) == "a.example"
    assert wb.operator_of("rekor+https://rekor.sigstore.dev") == "rekor.sigstore.dev"
    assert wb.operator_of("https://b.example:8443", directory) == "b.example"


def test_committed_rekor_row_key_id_is_its_log_id():
    key = load_pem_public_key(_committed_pem("rekor.sigstore.dev"))
    der = key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    raw = json.loads((FIXTURES / "rekor_public_entry_100000000.json").read_text())
    assert hashlib.sha256(der).hexdigest() == next(iter(raw.values()))["logID"]


# -- golden: a real public-Rekor entry ---------------------------------------


def test_set_canonicalization_matches_a_real_public_rekor_entry():
    """logIndex 100000000 on rekor.sigstore.dev, fetched 2026-09-27. Its SET
    verifies under the committed directory's key for that log; the entry is
    not ours, so the binding check (step 2) is what refuses it -- proving
    step 1 passed."""
    raw = json.loads((FIXTURES / "rekor_public_entry_100000000.json").read_text())
    uuid, entry = next(iter(raw.items()))
    ok, reason = wb.verify_rekor_receipt(
        {"uuid": uuid, **entry},
        checkpoint_cose=b"not this entry",
        checkpoint_key_id="00" * 32,
        rekor_public_key_pem=_committed_pem("rekor.sigstore.dev"),
    )
    assert not ok
    assert "signedEntryTimestamp" not in reason and "logID" not in reason, reason


def test_a_tampered_real_entry_fails_the_set():
    raw = json.loads((FIXTURES / "rekor_public_entry_100000000.json").read_text())
    uuid, entry = next(iter(raw.items()))
    entry = {**entry, "integratedTime": entry["integratedTime"] + 1}
    ok, reason = wb.verify_rekor_receipt(
        {"uuid": uuid, **entry},
        checkpoint_cose=b"x",
        checkpoint_key_id="00" * 32,
        rekor_public_key_pem=_committed_pem("rekor.sigstore.dev"),
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
        directory=_dir(
            _row("Witness Operator A", cll_url, TEST_TS_PUBLIC_KEY_PEM),
            _row("Rekor Operator B", rekor_url, REKOR_TEST_PUBLIC_KEY_PEM),
        ),
    )
    by_binding = {v.binding: v for v in result.receipts}
    assert by_binding["rekor"].verified, by_binding["rekor"].reason
    assert by_binding["rekor"].grade == "observed-only"
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
        directory=_dir(_row("r", rekor_url, REKOR_TEST_PUBLIC_KEY_PEM)),
    )
    assert [v.grade for v in result.receipts] == ["observed-only"]


def test_rekor_receipt_under_another_rows_key_does_not_count(tmp_path, servers):
    """The row for this endpoint lists public Rekor's key (from the committed
    directory); our mock's SET is not Rekor's, so it must not count."""
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _l, _cp, state = _pushed(tmp_path, [rekor_url])
    result = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        directory=_dir(_row("r", rekor_url, _committed_pem("rekor.sigstore.dev"))),
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


@pytest.mark.parametrize(
    ("label", "grade"),
    [
        ("observed-only", "observed-only"),
        # Issued before the rename: the same meaning, reported under its current name.
        ("countersigned-observed", "observed-only"),
        ("mmr-verified", "mmr-verified"),
        (None, "observed-only"),
        ("fully-verified", "observed-only"),
    ],
)
def test_scrapi_receipt_grade_label(label, grade):
    checkpoint_cose = b"checkpoint statement bytes"
    leaf = hashlib.sha256(checkpoint_cose).hexdigest()
    receipt = build_receipt(
        leaf_entry_hex=leaf,
        leaf_index=0,
        tree_entries_hex=[leaf],
        alg="EdDSA",
        log_private_key_pem=TEST_TS_PRIVATE_KEY_PEM,
        grade=label,
    )
    ok, _reason, got = wb.verify_scrapi_receipt(
        receipt, checkpoint_cose=checkpoint_cose, service_public_key_pem=TEST_TS_PUBLIC_KEY_PEM
    )
    assert ok
    assert got == grade


def test_scrapi_registration_polls_and_verifies_under_the_rows_key(tmp_path, servers):
    scrapi_url = "scrapi+" + servers(_ScrapiHandler, pending={})
    _l, cp, state = _pushed(tmp_path, [scrapi_url])
    assert [w.ts_url for w in cp.witnesses] == [scrapi_url]

    listed = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        directory=_dir(_row("s", scrapi_url, TEST_TS_PUBLIC_KEY_PEM)),
    )
    assert listed.counted == 1, listed.receipts
    assert listed.receipts[0].grade == "observed-only"

    unlisted = wb.verify_witnesses(
        state.checkpoint, state.effective_witnesses, checkpoint_cose_hex=state.checkpoint_cose_hex, directory=_dir()
    )
    assert unlisted.counted == 0
    assert unlisted.receipts[0].reason.startswith("not checked")


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
        directory=_dir(_row("s", scrapi_url, other)),
    )
    assert result.counted == 0


# -- no row is privileged -----------------------------------------------------


def _cll_receipt_at(url, tmp_path, servers):
    """A real cll receipt (signed by the test witness key), relabelled as if
    it came from ``url``. The receipt binds to the checkpoint, not the URL, so
    it verifies under the right key whatever URL it is filed under."""
    from dataclasses import replace

    local = servers(_CllHandler)
    _l, _cp, state = _pushed(tmp_path, [local])
    return state, replace(state.effective_witnesses[local], ts_url=url)


def test_the_default_witness_has_no_built_in_key(tmp_path, servers):
    """A receipt filed under the library's default witness URL, with no
    directory row for it, is not checked -- exactly like an unlisted third
    party. Before the directory was the only key source, this URL was
    auto-pinned to a built-in key."""
    from cll.checkpoint.emit import DEFAULT_TS_URL

    for url in (DEFAULT_TS_URL, "https://third.example"):
        state, rec = _cll_receipt_at(url, tmp_path / url.split("//")[1], servers)
        r = wb.verify_witnesses(state.checkpoint, [rec], directory=_dir())
        assert r.counted == 0 and r.receipts[0].reason == "not checked: no directory row for this witness", url


def test_our_row_is_treated_exactly_like_a_third_row(tmp_path, servers):
    """The same receipt, under the same key, filed once under our committed
    row's endpoint and once under a third party's: identical verdict, grade
    and reason. With the committed key (which did not sign it) both fail the
    same way."""
    ours = next(r for r in COMMITTED["witnesses"] if r["binding"] == "cll")
    our_url = ours["endpoint"]
    third_url = "https://third.example"
    outcomes = {}
    for key_pem, label in ((TEST_TS_PUBLIC_KEY_PEM, "test-key"), (wd.row_public_keys_pem(ours)[0], "committed-key")):
        for url in (our_url, third_url):
            state, rec = _cll_receipt_at(url, tmp_path / f"{label}-{url.split('//')[1]}", servers)
            directory = _dir(_row(ours["name"], our_url, key_pem), _row("third.example", third_url, key_pem))
            v = wb.verify_witnesses(state.checkpoint, [rec], directory=directory).receipts[0]
            outcomes[(label, url)] = (v.verified, v.grade, v.reason)
    assert outcomes[("test-key", our_url)] == outcomes[("test-key", third_url)]
    assert outcomes[("committed-key", our_url)][:2] == outcomes[("committed-key", third_url)][:2] == (False, None)
    assert outcomes[("test-key", our_url)][0] is True


@pytest.mark.parametrize("valid_first", [False, True], ids=["new-key-last", "old-key-first"])
def test_every_key_in_the_row_is_tried(tmp_path, servers, valid_first):
    """The signing key may sit anywhere in ``key_ids``: last (a receipt from
    the newest key, listed after the old one) or first (an old receipt,
    signed before a rotation added a newer key after it)."""
    state, rec = _cll_receipt_at("https://w.example", tmp_path, servers)
    row = _row("w", "https://w.example", TEST_TS_PUBLIC_KEY_PEM)
    other = "11" * 32
    row["key_ids"] = [*row["key_ids"], other] if valid_first else [other, *row["key_ids"]]
    assert wb.verify_witnesses(state.checkpoint, [rec], directory=_dir(row)).counted == 1


def test_our_row_is_not_rescued_by_the_built_in_key(tmp_path, servers, monkeypatch):
    """Make the library's built-in default-witness key the key that actually
    signed the receipt, and give our row a DIFFERENT key. The directory row is
    the only key source, so the receipt must not verify: if anything in the
    plurality path still reached for the built-in key, it would."""
    import cll.checkpoint
    import cll.checkpoint.emit
    from cll.checkpoint.emit import DEFAULT_TS_URL

    import capsule_emit.checkpoint

    for mod in (cll.checkpoint.emit, cll.checkpoint, capsule_emit.checkpoint):
        monkeypatch.setattr(mod, "DEFAULT_TS_PUBLIC_KEY_PEM", TEST_TS_PUBLIC_KEY_PEM, raising=False)
    state, rec = _cll_receipt_at(DEFAULT_TS_URL, tmp_path, servers)
    wrong = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    ours = next(r for r in COMMITTED["witnesses"] if r["binding"] == "cll")
    r = wb.verify_witnesses(state.checkpoint, [rec], directory=_dir(_row(ours["name"], DEFAULT_TS_URL, wrong)))
    assert r.counted == 0 and not r.receipts[0].verified, r.receipts


# -- policy -------------------------------------------------------------------


def _three_witness_state(tmp_path, servers):
    cll_a = servers(_CllHandler)
    cll_b = servers(_CllHandler)
    rekor_url = "rekor+" + servers(_RekorHandler, entries=[], fail_with=[])
    _l, _cp, state = _pushed(tmp_path, [cll_a, cll_b, rekor_url])
    return state, (cll_a, cll_b, rekor_url)


def _three_dir(urls, names):
    pems = (TEST_TS_PUBLIC_KEY_PEM, TEST_TS_PUBLIC_KEY_PEM, REKOR_TEST_PUBLIC_KEY_PEM)
    rows = [_row(n, u, p) for n, u, p in zip(names, urls, pems)]
    # two cll rows share the test witness key; key_ids are unique per file in
    # a committed directory, but verify_witnesses does not require that.
    return _dir(*rows)


def test_k_of_n(tmp_path, servers):
    state, urls = _three_witness_state(tmp_path, servers)
    directory = _three_dir(urls, ("op0", "op1", "op2"))
    for k, met in ((1, True), (3, True), (4, False)):
        r = wb.verify_witnesses(
            state.checkpoint,
            state.effective_witnesses,
            checkpoint_cose_hex=state.checkpoint_cose_hex,
            policy=wb.WitnessPolicy(min_receipts=k),
            directory=directory,
        )
        assert r.counted == 3 and r.policy_met is met, (k, r)


def test_distinct_operators_counts_operators_not_receipts(tmp_path, servers):
    state, urls = _three_witness_state(tmp_path, servers)
    same_op = _three_dir(urls, ("Operator A", "Operator A", "Operator B"))
    strict = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        policy=wb.WitnessPolicy(min_receipts=3, distinct_operators=True),
        directory=same_op,
    )
    assert strict.counted == 3 and strict.operators == 2 and not strict.policy_met
    loose = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        policy=wb.WitnessPolicy(min_receipts=3, distinct_operators=False),
        directory=same_op,
    )
    assert loose.policy_met


def test_one_bad_receipt_is_reported_and_not_counted(tmp_path, servers):
    state, urls = _three_witness_state(tmp_path, servers)
    from dataclasses import replace

    tampered = dict(state.effective_witnesses)
    tampered[urls[0]] = replace(tampered[urls[0]], receipt_b64=base64.b64encode(b"\x00" * 40).decode())
    r = wb.verify_witnesses(
        state.checkpoint,
        tampered,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        policy=wb.WitnessPolicy(min_receipts=2),
        directory=_three_dir(urls, ("op0", "op1", "op2")),
    )
    bad = [v for v in r.receipts if v.ts_url == urls[0]][0]
    assert not bad.verified and bad.grade is None and bad.reason
    assert r.counted == 2 and r.policy_met


def test_stub_receipts_never_count(tmp_path):
    from capsule_emit.checkpoint import emit_checkpoint, register_checkpoint_stub

    ledger = str(tmp_path / "l.jsonl")
    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness=False)
    state = witness._get_state(ledger)
    state.mmr.sync()
    record = emit_checkpoint(state.mmr, state.signer, log_id=state.log_id)
    stub = register_checkpoint_stub(record)
    r = wb.verify_witnesses(record, [stub], directory=COMMITTED, policy=wb.WitnessPolicy(min_receipts=1))
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
    against the committed witnesses.json."""
    ledger, cp, state = _pushed(tmp_path, [wb.PUBLIC_REKOR_URL])
    assert [w.ts_url for w in cp.witnesses] == [wb.PUBLIC_REKOR_URL]
    r = wb.verify_witnesses(
        state.checkpoint,
        state.effective_witnesses,
        checkpoint_cose_hex=state.checkpoint_cose_hex,
        directory=COMMITTED,
    )
    assert r.counted == 1, r.receipts
