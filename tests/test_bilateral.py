# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.bilateral — OSS bilateral handshake.

Covers:
- payload functions: request_payload, action_payload, confirm_payload are deterministic
- sig_digest: same sig → same digest; different sig → different digest
- dict_verifier / dict_signer: HMAC round-trip
- BilateralHandshake: open → REQUESTED; ONE_SIDED when responder None
- BilateralHandshake: respond REQUESTED → ACTED; illegal from REQUESTED wrong state
- BilateralHandshake: confirm → BILATERAL when both sides confirm
- BilateralHandshake: invalid signature raises InvalidSignature
- BilateralHandshake: wrong party raises UnknownParty
- capsule emission: seal_request writes ledger record with action_digest
- capsule emission: seal_action chains to requester capsule
- end-to-end: full four-move handshake + capsule ledger
"""
from __future__ import annotations

import hashlib
import json

import pytest

from capsule_emit.bilateral import (
    BilateralHandshake,
    BilateralSig,
    BilateralState,
    IllegalTransition,
    InvalidSignature,
    UnknownParty,
    action_payload,
    confirm_payload,
    dict_signer,
    dict_verifier,
    no_op_verifier,
    request_payload,
    seal_action,
    seal_request,
    sig_digest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEYS = {
    "org-a": b"secret-key-for-org-a",
    "org-b": b"secret-key-for-org-b",
}
_sign = dict_signer(_KEYS)
_verify_fn = dict_verifier(_KEYS)


def _action_digest(action: dict) -> str:
    raw = json.dumps(action, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


ACTION = {"type": "book_slot", "amount": 1000, "vendor": "Acme"}
DIGEST = _action_digest(ACTION)


# ---------------------------------------------------------------------------
# Payload functions
# ---------------------------------------------------------------------------


def test_request_payload_deterministic():
    p1 = request_payload("org-a", "org-b", DIGEST)
    p2 = request_payload("org-a", "org-b", DIGEST)
    assert p1 == p2


def test_request_payload_contains_all_fields():
    p = json.loads(request_payload("org-a", "org-b", DIGEST))
    assert p["phase"] == "request"
    assert p["requester_org"] == "org-a"
    assert p["responder_org"] == "org-b"
    assert p["action_digest"] == DIGEST


def test_action_payload_binds_request_sig():
    sig = BilateralSig(alg="hmac-sha256", key_id="org-a", signature="abc")
    p = json.loads(action_payload("hid-1", "org-b", sig_digest(sig)))
    assert p["phase"] == "action"
    assert p["handshake_id"] == "hid-1"
    assert p["responder_org"] == "org-b"
    assert "request_sig_digest" in p


def test_confirm_payload_binds_acked_sig():
    sig = BilateralSig(alg="hmac-sha256", key_id="org-b", signature="xyz")
    p = json.loads(confirm_payload("hid-1", "org-a", sig_digest(sig)))
    assert p["phase"] == "confirm"
    assert p["party_org"] == "org-a"
    assert "acked_sig_digest" in p


def test_sig_digest_deterministic():
    sig = BilateralSig(alg="hmac-sha256", key_id="org-a", signature="sig123")
    assert sig_digest(sig) == sig_digest(sig)


def test_sig_digest_different_for_different_sigs():
    s1 = BilateralSig(alg="hmac-sha256", key_id="org-a", signature="sig1")
    s2 = BilateralSig(alg="hmac-sha256", key_id="org-a", signature="sig2")
    assert sig_digest(s1) != sig_digest(s2)


# ---------------------------------------------------------------------------
# dict_verifier / dict_signer
# ---------------------------------------------------------------------------


def test_dict_verifier_valid():
    payload = b"test payload"
    sig = _sign("org-a", payload)
    assert _verify_fn("org-a", payload, sig)


def test_dict_verifier_wrong_key():
    payload = b"test payload"
    sig = _sign("org-a", payload)
    assert not _verify_fn("org-b", payload, sig)


def test_dict_verifier_unknown_org():
    sig = BilateralSig(alg="hmac-sha256", key_id="unknown", signature="x")
    assert not _verify_fn("unknown", b"payload", sig)


def test_dict_verifier_tampered_payload():
    payload = b"original"
    sig = _sign("org-a", payload)
    assert not _verify_fn("org-a", b"tampered", sig)


# ---------------------------------------------------------------------------
# BilateralHandshake — state machine
# ---------------------------------------------------------------------------


def _hs() -> BilateralHandshake:
    return BilateralHandshake(verify_fn=_verify_fn)


def _open(hs: BilateralHandshake, responder: str | None = "org-b") -> object:
    payload = request_payload("org-a", responder, DIGEST)
    sig = _sign("org-a", payload)
    return hs.open("org-a", responder, DIGEST, sig)


def test_open_requested():
    hs = _hs()
    rec = _open(hs)
    assert rec.state is BilateralState.REQUESTED
    assert rec.requester_org == "org-a"
    assert rec.responder_org == "org-b"
    assert rec.action_digest == DIGEST


def test_open_one_sided_when_no_responder():
    hs = _hs()
    rec = _open(hs, responder=None)
    assert rec.state is BilateralState.ONE_SIDED


def test_open_invalid_sig_raises():
    hs = _hs()
    bad_sig = BilateralSig(alg="hmac-sha256", key_id="org-a", signature="bad")
    with pytest.raises(InvalidSignature):
        hs.open("org-a", "org-b", DIGEST, bad_sig)


def test_respond_acted():
    hs = _hs()
    rec = _open(hs)
    hid = rec.handshake_id
    a_payload = action_payload(hid, "org-b", sig_digest(rec.request_sig))
    action_sig = _sign("org-b", a_payload)
    rec2 = hs.respond(hid, action_sig)
    assert rec2.state is BilateralState.ACTED
    assert rec2.action_sig is not None


def test_respond_wrong_state_raises():
    hs = _hs()
    rec = _open(hs, responder=None)  # ONE_SIDED
    with pytest.raises(IllegalTransition):
        hs.respond(rec.handshake_id, _sign("org-b", b"x"))


def test_confirm_bilateral():
    hs = _hs()
    rec = _open(hs)
    hid = rec.handshake_id

    a_pay = action_payload(hid, "org-b", sig_digest(rec.request_sig))
    action_sig = _sign("org-b", a_pay)
    rec2 = hs.respond(hid, action_sig)

    # Requester confirms B's action sig
    req_pay = confirm_payload(hid, "org-a", sig_digest(rec2.action_sig))
    req_confirm = _sign("org-a", req_pay)
    rec3 = hs.confirm(hid, "org-a", req_confirm)
    assert rec3.state is BilateralState.ACTED  # not yet bilateral — only one side

    # Responder confirms A's request sig
    resp_pay = confirm_payload(hid, "org-b", sig_digest(rec2.request_sig))
    resp_confirm = _sign("org-b", resp_pay)
    rec4 = hs.confirm(hid, "org-b", resp_confirm)
    assert rec4.state is BilateralState.BILATERAL


def test_confirm_unknown_party_raises():
    hs = _hs()
    rec = _open(hs)
    hid = rec.handshake_id
    a_pay = action_payload(hid, "org-b", sig_digest(rec.request_sig))
    hs.respond(hid, _sign("org-b", a_pay))
    with pytest.raises(UnknownParty):
        hs.confirm(hid, "org-c", _sign("org-a", b"x"))


def test_no_op_verifier_accepts_anything():
    hs = BilateralHandshake(verify_fn=no_op_verifier)
    bad_sig = BilateralSig(alg="none", key_id="x", signature="garbage")
    rec = hs.open("org-a", "org-b", DIGEST, bad_sig)
    assert rec.state is BilateralState.REQUESTED


# ---------------------------------------------------------------------------
# Capsule emission helpers
# ---------------------------------------------------------------------------


def test_seal_request_writes_ledger(tmp_path):
    ledger = str(tmp_path / "hs.jsonl")
    result = seal_request(
        "org-a", "agent-a@v1", "book_slot", DIGEST, ledger=ledger, anchor=False
    )
    from capsule_emit.ledger import read_ledger
    records = read_ledger(ledger)
    assert len(records) == 1
    ca = records[0]["model_attestation"]["compute_attestation"]
    assert ca["action_digest"] == DIGEST
    assert ca["role"] == "requester"
    assert result.capsule_id == records[0]["capsule_id"]


def test_seal_action_chains_to_requester(tmp_path):
    ledger = str(tmp_path / "hs.jsonl")
    req = seal_request("org-a", "agent-a@v1", "book_slot", DIGEST, ledger=ledger, anchor=False)
    seal_action(
        "org-b", "agent-b@v1", "book_slot", DIGEST, req.capsule_id,
        ledger=ledger, anchor=False
    )
    from capsule_emit.ledger import read_ledger
    records = read_ledger(ledger)
    assert len(records) == 2
    chain_b = records[1].get("chain", {})
    assert chain_b.get("parent_capsule_id") == req.capsule_id
    assert records[1]["model_attestation"]["compute_attestation"]["role"] == "responder"


# ---------------------------------------------------------------------------
# End-to-end: full handshake + pair capsules
# ---------------------------------------------------------------------------


def test_e2e_full_handshake_and_verify(tmp_path):
    """Full four-move handshake → bilateral state + pair-verify passes."""
    bilateral = pytest.importorskip(
        "agent_action_capsule.bilateral",
        reason="agent_action_capsule.bilateral not installed in this editable target",
    )
    verify_pair = bilateral.verify_pair

    ledger = str(tmp_path / "bilateral.jsonl")
    hs = _hs()

    # 1. Request
    req_pay = request_payload("org-a", "org-b", DIGEST)
    req_sig = _sign("org-a", req_pay)
    rec = hs.open("org-a", "org-b", DIGEST, req_sig)
    hid = rec.handshake_id

    cap_a = seal_request("org-a", "agent-a@v1", "book_slot", DIGEST, ledger=ledger, anchor=False)

    # 2. Action (B evaluates constraints, seals)
    act_pay = action_payload(hid, "org-b", sig_digest(rec.request_sig))
    act_sig = _sign("org-b", act_pay)
    rec2 = hs.respond(hid, act_sig)

    cap_b = seal_action(
        "org-b", "agent-b@v1", "book_slot", DIGEST, cap_a.capsule_id,
        ledger=ledger, anchor=False
    )

    # 3. Both confirm
    rq_c_pay = confirm_payload(hid, "org-a", sig_digest(rec2.action_sig))
    hs.confirm(hid, "org-a", _sign("org-a", rq_c_pay))
    rs_c_pay = confirm_payload(hid, "org-b", sig_digest(rec2.request_sig))
    rec_final = hs.confirm(hid, "org-b", _sign("org-b", rs_c_pay))

    assert rec_final.state is BilateralState.BILATERAL

    # 4. Pair-verify
    pvr = verify_pair(cap_a.capsule, cap_b.capsule)
    assert pvr.ok, pvr.findings
    assert pvr.shared_digest == DIGEST
