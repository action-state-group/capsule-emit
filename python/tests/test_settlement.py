# SPDX-License-Identifier: Apache-2.0
"""capsule_emit.settlement: building, sealing and verifying two-party
settlement records (draft-mih-agent-settlement-records-00).

The draft's own conformance vectors run in test_settlement_records_vectors.py;
these tests cover the producer side and the rules case by case."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from capsule_emit.canonicalization import compute_capsule_id
from capsule_emit.core import _emit_capsule
from capsule_emit.settlement import (
    SETTLEMENT_MEMBER,
    SettlementError,
    amount,
    build_leg,
    counterparty_reference,
    seal_leg,
    structure_failures,
    verify_settlements,
    wrap,
)
from capsule_emit.verification import verify_capsule

NETWORK = "eip155:84532"
TX = "0x" + "ab" * 32
REF = {"type": "x402.transaction", "value": TX, "network": NETWORK}
USDC = "eip155:84532/erc20:0x036cbd53842c5426634e7929541ec2318f3dcf7e"
PRICE = amount(10000, USDC, 6)
ZERO = amount(0, USDC, 6)
AT = "2026-10-02T01:08:30Z"
DIGEST = "d" * 64
EXACT_PAYLOAD = json.dumps({"x402Version": 2, "accepted": {"scheme": "exact", "network": NETWORK}}).encode()


class Party:
    def __init__(self, tmp_path: Path, name: str):
        self.name = name
        self.ledger = tmp_path / name / "ledger.jsonl"

    def seal(self, member: dict, **kw) -> dict:
        return seal_leg(member, operator=self.name, developer=f"{self.name}-agent@1", ledger=self.ledger,
                        witness=False, **kw).capsule

    @property
    def key(self) -> str:
        from capsule_emit.signing import resolve_signer

        return resolve_signer(self.ledger).key_id


def _terms(party: Party, **kw) -> dict:
    return party.seal(build_leg("terms", "payee", amount=kw.pop("amount", PRICE), **kw))


def _payer_leg(terms_id: str, **kw) -> dict:
    base = dict(terms_ref=terms_id, amount=PRICE, routing_fee=ZERO, payment_ref=REF, status="settled",
                observed_at=AT, wrapped=[wrap(EXACT_PAYLOAD, type="x402.payment-payload")])
    base.update(kw)
    return build_leg("payer_observed", "payer", **base)


def _payee_leg(terms_id: str, **kw) -> dict:
    base = dict(terms_ref=terms_id, received=PRICE, receive_fee=ZERO, payment_ref=REF, status="settled",
                observed_at=AT)
    base.update(kw)
    return build_leg("payee_observed", "payee", **base)


def _delivered(terms_id: str, role: str, digest: str = DIGEST, **kw) -> dict:
    direction = "sent" if role == "payee" else "received"
    delivery = kw.pop("delivery", {"direction": direction, "content_digest": digest})
    return build_leg("delivered", role, terms_ref=terms_id, observed_at=AT, delivery=delivery, **kw)


def _two_sided(tmp_path, payer_kw=None, payee_kw=None, payee_party="payee"):
    payer, payee = Party(tmp_path, "payer"), Party(tmp_path, payee_party)
    terms = _terms(Party(tmp_path, "payee"))
    p = payer.seal(_payer_leg(terms["capsule_id"], **(payer_kw or {})))
    q = payee.seal(_payee_leg(terms["capsule_id"], **(payee_kw or {})))
    return terms, p, q, payer, payee


def _policy(payer: Party, payee: Party) -> dict:
    return {"payer": payer.key, "payee": payee.key}


def _state(records, **kw) -> dict:
    report = verify_settlements(records, wrapped_objects=[EXACT_PAYLOAD], **kw)
    return report.settlements[0] if report.settlements else {}


# --- building -----------------------------------------------------------------


def test_amounts_are_exact_integers():
    for bad in (0.01, "0.01", "01", True, -1):
        with pytest.raises(SettlementError) as e:
            amount(bad, USDC, 6)
        assert e.value.codes == ["amount_not_exact"]
    with pytest.raises(SettlementError):
        amount(1, USDC, 256)
    assert amount(1000, "BTC", 11) == {"value": "1000", "assetCode": "BTC", "assetScale": 11}


def test_a_leg_carries_only_its_own_members():
    with pytest.raises(SettlementError) as e:
        build_leg("payee_observed", "payee", terms_ref="a" * 64, amount=PRICE, received=PRICE,
                  payment_ref=REF, status="settled", observed_at=AT)
    assert e.value.codes == ["settlement_malformed"]
    with pytest.raises(SettlementError) as e:
        build_leg("payer_observed", "payee", terms_ref="a" * 64, amount=PRICE, payment_ref=REF,
                  status="settled", observed_at=AT)
    assert e.value.codes == ["leg_role_mismatch"]
    with pytest.raises(SettlementError):
        _payee_leg("a" * 64, status="received")  # not one of pending/settled/failed/reversed
    with pytest.raises(SettlementError):
        build_leg("terms", "payee", amount=PRICE, receive_fee_max=ZERO)  # the draft defines no fee bounds


def test_delivered_needs_a_content_digest_unless_a_carrier_is_present():
    with pytest.raises(SettlementError):
        _delivered("a" * 64, "payee", delivery={"direction": "sent"})
    with pytest.raises(SettlementError):
        _delivered("a" * 64, "payee", delivery={"direction": "received", "content_digest": DIGEST})
    leg = _delivered("a" * 64, "payee", delivery={"direction": "sent", "carrier": "example-carrier"})
    assert "content_digest" not in leg["delivery"]
    assert structure_failures(_delivered("a" * 64, "payer")) == []


def test_evm_transaction_hash_takes_its_normal_form():
    leg = _payer_leg("a" * 64, payment_ref={**REF, "value": TX.upper().replace("0X", "0x")})
    assert leg["payment_ref"]["value"] == TX


def test_wrap_digests_exact_octets_or_the_rfc8785_form():
    import hashlib

    raw = b'{"b":1,"a":2}'
    assert wrap(raw, type="x402.payment-payload")["digest"] == hashlib.sha256(raw).hexdigest()
    assert wrap({"b": 1, "a": 2}, type="x402.offer")["digest"] == hashlib.sha256(b'{"a":2,"b":1}').hexdigest()
    with pytest.raises(SettlementError):
        wrap(raw, type="x402.unknown")


def test_a_sealed_leg_is_a_capsule_with_a_top_level_settlement_member(tmp_path):
    terms, p, _, _, _ = _two_sided(tmp_path)
    assert p[SETTLEMENT_MEMBER]["terms_ref"] == terms["capsule_id"]
    assert "x-settlement-v0" not in json.dumps(p)
    assert p["action_type"] == "fyi"
    assert verify_capsule(p).ok
    edited = copy.deepcopy(p)
    edited[SETTLEMENT_MEMBER]["amount"]["value"] = "1"
    assert compute_capsule_id(edited) != p["capsule_id"]


def test_payload_members_never_replace_a_capsule_member(tmp_path):
    with pytest.raises(ValueError, match="would replace"):
        _emit_capsule("x", ledger=tmp_path / "l.jsonl", witness=False, payload_members={"operator": "x"})


# --- the payment state --------------------------------------------------------


def test_two_sided_with_a_key_policy_is_agreed(tmp_path):
    terms, p, q, payer, payee = _two_sided(tmp_path)
    s = _state([terms, p, q], key_policy=_policy(payer, payee))
    assert (s["payment_state"], s["agreed_status"], s["terms_amount"]) == ("agreed", "settled", "equal")
    assert s["iso20022"] == {"payer_observed": "ACSC", "payee_observed": "ACCC"}


def test_without_a_key_policy_agreed_says_so_in_the_separate_key_result(tmp_path):
    terms, p, q, payer, payee = _two_sided(tmp_path)
    report = verify_settlements([terms, p, q], wrapped_objects=[EXACT_PAYLOAD])
    assert report.settlements[0]["payment_state"] == "agreed"
    assert report.key_policy_applied is False
    assert set(report.keys.values()) == {payer.key, payee.key}
    with_policy = verify_settlements([terms, p, q], key_policy=_policy(payer, payee), wrapped_objects=[EXACT_PAYLOAD])
    assert with_policy.key_policy_applied is True


def test_one_side_is_a_stated_claim(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path)
    assert _state([terms, p])["payment_state"] == "payer_stated"
    assert _state([terms, q])["payment_state"] == "payee_stated"
    assert _state([terms])["payment_state"] == "terms_only"


def _msat(v: int) -> dict:
    return amount(v, "BTC", 11)


LN = {"type": "ln.payment_hash", "value": "11" * 32}


def test_lightning_receive_fee_reconciles_and_naive_equality_would_not(tmp_path):
    terms, p, q, _, _ = _two_sided(
        tmp_path, payer_kw={"amount": _msat(1000), "routing_fee": _msat(0), "payment_ref": LN, "wrapped": None},
        payee_kw={"received": _msat(995), "receive_fee": _msat(5), "payment_ref": LN})
    assert _state([terms, p, q])["payment_state"] == "agreed"
    short = Party(tmp_path, "payee").seal(_payee_leg(terms["capsule_id"], received=_msat(994),
                                                     receive_fee=_msat(5), payment_ref=LN))
    s = _state([terms, p, short])
    assert (s["payment_state"], s["differs"]) == ("mismatch", ["amount"])


def test_lightning_without_receive_fee_is_unjoined_fee_unstated(tmp_path):
    terms, p, q, _, _ = _two_sided(
        tmp_path, payer_kw={"amount": _msat(1000), "routing_fee": _msat(0), "payment_ref": LN, "wrapped": None},
        payee_kw={"received": _msat(1000), "receive_fee": None, "payment_ref": LN})
    report = verify_settlements([terms, p, q])
    assert report.settlements[0]["payment_state"] == "unjoined"
    assert report.findings == [{"records": [q["capsule_id"]], "code": "fee_unstated"}]


def test_x402_exact_reads_an_absent_receive_fee_as_zero(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path, payee_kw={"receive_fee": None})
    assert _state([terms, p, q])["payment_state"] == "agreed"


def test_x402_scheme_other_than_exact_means_a_receive_fee_may_apply(tmp_path):
    upto = json.dumps({"x402Version": 2, "accepted": {"scheme": "upto", "network": NETWORK}}).encode()
    terms, p, q, _, _ = _two_sided(tmp_path, payer_kw={"wrapped": [wrap(upto, type="x402.payment-payload")]},
                                   payee_kw={"receive_fee": None})
    report = verify_settlements([terms, p, q], wrapped_objects=[upto])
    assert report.settlements[0]["payment_state"] == "unjoined"
    assert [f["code"] for f in report.findings] == ["fee_unstated"]


def test_x402_scheme_that_cannot_be_established_means_a_receive_fee_may_apply(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path, payee_kw={"receive_fee": None})
    report = verify_settlements([terms, p, q])  # the payment payload's octets are not held
    assert report.settlements[0]["payment_state"] == "unjoined"
    terms2, p2, q2, _, _ = _two_sided(tmp_path / "b", payer_kw={"wrapped": None}, payee_kw={"receive_fee": None})
    assert _state([terms2, p2, q2])["payment_state"] == "unjoined"


def test_lightning_btc_and_on_chain_btc_are_different_assets(tmp_path):
    ln = {"type": "ln.payment_hash", "value": "22" * 32}
    onchain = "bip122:000000000019d6689c085ae165831e93/slip44:0"
    terms, p, q, _, _ = _two_sided(
        tmp_path,
        payer_kw={"amount": _msat(1000), "routing_fee": _msat(0), "payment_ref": ln, "wrapped": None},
        payee_kw={"received": amount(1000, onchain, 11), "receive_fee": amount(0, onchain, 11), "payment_ref": ln})
    s = _state([terms, p, q])
    assert (s["payment_state"], s["differs"]) == ("mismatch", ["amount"])


def test_a_fee_in_another_asset_is_unjoined(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path, payee_kw={"receive_fee": amount(0, "USD", 2)})
    report = verify_settlements([terms, p, q], wrapped_objects=[EXACT_PAYLOAD])
    assert report.settlements[0]["payment_state"] == "unjoined"
    assert [f["code"] for f in report.findings] == ["fee_asset_differs"]


def test_status_and_reference_differences_are_named(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path, payee_kw={"status": "failed"})
    s = _state([terms, p, q])
    assert (s["payment_state"], s["differs"]) == ("mismatch", ["status"])
    assert s["iso20022"]["payee_observed"] == "RJCT"
    terms, p, q, _, _ = _two_sided(tmp_path / "b", payee_kw={"payment_ref": {**REF, "value": "0x" + "cd" * 32}})
    assert _state([terms, p, q])["differs"] == ["payment_ref"]


def test_the_same_amount_at_another_scale_is_equal(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path, payee_kw={"received": amount(10000000, USDC, 9),
                                                       "receive_fee": amount(0, USDC, 9)})
    assert _state([terms, p, q])["payment_state"] == "agreed"


def test_terms_amount_is_reported_separately(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path, payer_kw={"amount": amount(9000, USDC, 6)},
                                   payee_kw={"received": amount(9000, USDC, 6)})
    s = _state([terms, p, q])
    assert (s["payment_state"], s["terms_amount"]) == ("agreed", "differs")


def test_terms_in_another_asset_is_terms_amount_differs(tmp_path):
    payee = Party(tmp_path, "payee")
    terms = _terms(payee, amount=amount(10000, "USD", 6))
    p = Party(tmp_path, "payer").seal(_payer_leg(terms["capsule_id"]))
    q = payee.seal(_payee_leg(terms["capsule_id"]))
    s = _state([terms, p, q])
    assert (s["payment_state"], s["terms_amount"]) == ("agreed", "differs")


def test_pending_superseded_by_settled_uses_the_head(tmp_path):
    payer, payee = Party(tmp_path, "payer"), Party(tmp_path, "payee")
    terms = _terms(payee)
    pending = payer.seal(_payer_leg(terms["capsule_id"], status="pending"))
    settled = payer.seal(_payer_leg(terms["capsule_id"]), prior=pending["capsule_id"], relation="supersedes")
    q = payee.seal(_payee_leg(terms["capsule_id"]))
    assert _state([terms, pending, settled, q])["payment_state"] == "agreed"
    assert _state([terms, pending, q])["differs"] == ["status"]


def test_a_supersedes_link_from_another_key_is_ignored_same_values(tmp_path):
    terms, p, q, payer, _ = _two_sided(tmp_path)
    attacker = Party(tmp_path, "attacker")
    spoof = attacker.seal(_payer_leg(terms["capsule_id"]), prior=p["capsule_id"], relation="supersedes")
    report = verify_settlements([terms, p, spoof, q], wrapped_objects=[EXACT_PAYLOAD])
    assert report.settlements[0]["payment_state"] == "agreed"
    codes = [d["code"] for d in report.diagnostics]
    assert codes == ["supersedes_ignored", "several_keys_for_role"]
    ignored = report.diagnostics[0]
    assert (ignored["record"], ignored["key"], ignored["parent"]) == (spoof["capsule_id"], attacker.key,
                                                                      p["capsule_id"])
    assert report.diagnostics[1]["keys"] == sorted([payer.key, attacker.key])  # the payer's key is not gone


def test_a_supersedes_link_from_another_key_is_ignored_different_values(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path)
    spoof = Party(tmp_path, "attacker").seal(_payer_leg(terms["capsule_id"], status="failed"),
                                             prior=p["capsule_id"], relation="supersedes")
    report = verify_settlements([terms, p, spoof, q], wrapped_objects=[EXACT_PAYLOAD])
    s = report.settlements[0]
    assert (s["payment_state"], s["differs"]) == ("mismatch", ["status"])  # the genuine leg still counts
    assert "supersedes_ignored" in [d["code"] for d in report.diagnostics]


def test_a_sealer_superseding_its_own_leg_is_honoured(tmp_path):
    terms, p, q, payer, _ = _two_sided(tmp_path, payer_kw={"status": "pending"})
    later = payer.seal(_payer_leg(terms["capsule_id"]), prior=p["capsule_id"], relation="supersedes")
    report = verify_settlements([terms, p, later, q], wrapped_objects=[EXACT_PAYLOAD])
    assert report.settlements[0]["payment_state"] == "agreed" and report.diagnostics == []


def test_two_unchained_observations_that_disagree_are_a_mismatch(tmp_path):
    terms, p, q, payer, _ = _two_sided(tmp_path)
    other = payer.seal(_payer_leg(terms["capsule_id"], amount=amount(1, USDC, 6)))
    assert "amount" in _state([terms, p, other, q])["differs"]


# --- keys and records ---------------------------------------------------------


def test_one_key_on_both_observed_legs_is_sealer_conflation(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path, payee_party="payer")
    report = verify_settlements([terms, p, q], wrapped_objects=[EXACT_PAYLOAD])
    assert {"records": [p["capsule_id"], q["capsule_id"]], "code": "sealer_conflation"} in report.failures
    assert report.settlements[0]["payment_state"] is None  # no draft state applies; never agreed
    assert [d["code"] for d in report.diagnostics] == ["sealer_conflation"]
    assert not report.conforming


def _resign(capsule: dict, party: Party) -> dict:
    """The same capsule content, signed by another key (anyone can do this)."""
    from capsule_emit.signing import resolve_signer, sign_producer_envelope

    copy_ = copy.deepcopy(capsule)
    copy_["signature"], copy_["key_id"] = sign_producer_envelope(resolve_signer(party.ledger), copy_["capsule_id"])
    return copy_


def test_a_resigned_copy_cannot_knock_out_the_genuine_leg_under_a_key_policy(tmp_path):
    terms, p, q, payer, payee = _two_sided(tmp_path)
    spoof = _resign(q, Party(tmp_path, "attacker"))
    assert spoof["capsule_id"] == q["capsule_id"] and spoof["key_id"] != q["key_id"]
    for order in ([terms, p, spoof, q], [terms, p, q, spoof]):
        report = verify_settlements(order, key_policy=_policy(payer, payee), wrapped_objects=[EXACT_PAYLOAD])
        assert report.failures == [{"records": [q["capsule_id"]], "code": "sealer_not_authorized_for_role"}]
        assert report.settlements[0]["payment_state"] == "agreed"


def test_a_copy_resigned_with_the_payer_key_is_conflation_but_the_genuine_pair_still_agrees(tmp_path):
    terms, p, q, payer, payee = _two_sided(tmp_path)
    spoof = _resign(q, payer)
    for order in ([terms, p, spoof, q], [terms, p, q, spoof]):
        report = verify_settlements(order, wrapped_objects=[EXACT_PAYLOAD])
        assert report.failures == [{"records": [p["capsule_id"], q["capsule_id"]], "code": "sealer_conflation"}]
        assert report.settlements[0]["payment_state"] == "agreed"
        assert [d["code"] for d in report.diagnostics] == ["several_keys_for_role"]


def test_a_resigned_copy_of_the_terms_leg_changes_nothing(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path)
    spoof = _resign(terms, Party(tmp_path, "attacker"))
    report = verify_settlements([spoof, terms, p, q], wrapped_objects=[EXACT_PAYLOAD])
    assert len(report.settlements) == 1 and report.settlements[0]["payment_state"] == "agreed"


def test_a_key_policy_refuses_a_role_claim_from_another_key_and_ignores_it(tmp_path):
    terms, p, q, payer, payee = _two_sided(tmp_path)
    impostor = Party(tmp_path, "impostor").seal(_payee_leg(terms["capsule_id"]))
    report = verify_settlements([terms, p, q, impostor], key_policy=_policy(payer, payee),
                                wrapped_objects=[EXACT_PAYLOAD])
    assert report.failures == [{"records": [impostor["capsule_id"]], "code": "sealer_not_authorized_for_role"}]
    assert report.settlements[0]["payment_state"] == "agreed"


def test_rotated_keys_are_fine_when_all_are_in_the_policy(tmp_path):
    terms, p, q, payer, payee = _two_sided(tmp_path)
    rotated = Party(tmp_path, "payee-rotated")
    d = rotated.seal(_delivered(terms["capsule_id"], "payee"))
    r = payer.seal(_delivered(terms["capsule_id"], "payer"))
    policy = {"payer": payer.key, "payee": [payee.key, rotated.key]}
    s = _state([terms, p, q, d, r], key_policy=policy)
    assert (s["payment_state"], s["delivery_state"]) == ("agreed", "matched")


def test_without_a_policy_two_keys_on_one_side_are_a_diagnostic_not_a_difference(tmp_path):
    terms, p, q, _, payee = _two_sided(tmp_path)
    impostor = Party(tmp_path, "impostor")
    other = impostor.seal(_payee_leg(terms["capsule_id"]))
    report = verify_settlements([terms, p, q, other], wrapped_objects=[EXACT_PAYLOAD])
    s = report.settlements[0]
    assert s["payment_state"] == "agreed" and "differs" not in s
    assert report.diagnostics == [{"terms": terms["capsule_id"], "code": "several_keys_for_role", "role": "payee",
                                   "keys": sorted([payee.key, impostor.key]), "key_policy_applied": False}]


def test_the_same_capsule_twice_counts_once(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path)
    report = verify_settlements([terms, p, p, q, q], wrapped_objects=[EXACT_PAYLOAD])
    assert report.conforming and report.settlements[0]["payment_state"] == "agreed"


def test_a_forged_record_passed_twice_is_reported_once(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path)
    forged = copy.deepcopy(q)
    forged[SETTLEMENT_MEMBER]["received"]["value"] = "1"
    report = verify_settlements([terms, p, forged, forged])
    assert report.failures == [{"records": [forged["capsule_id"]], "code": "capsule_invalid"}]


def test_an_edited_leg_is_capsule_invalid_and_takes_no_part(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path)
    forged = copy.deepcopy(q)
    forged[SETTLEMENT_MEMBER]["received"]["value"] = "1"
    report = verify_settlements([terms, p, forged], wrapped_objects=[EXACT_PAYLOAD])
    assert report.failures == [{"records": [forged["capsule_id"]], "code": "capsule_invalid"}]
    assert report.settlements[0]["payment_state"] == "payer_stated"


def test_a_recomputed_id_with_no_valid_envelope_is_envelope_invalid(tmp_path):
    terms, p, q, _, _ = _two_sided(tmp_path)
    forged = copy.deepcopy(q)
    forged[SETTLEMENT_MEMBER]["received"]["value"] = "1"
    forged["capsule_id"] = compute_capsule_id(forged)
    report = verify_settlements([terms, p, forged], wrapped_objects=[EXACT_PAYLOAD])
    assert report.failures == [{"records": [forged["capsule_id"]], "code": "envelope_invalid"}]
    forged.pop("signature")
    assert verify_settlements([terms, p, forged]).failures[0]["code"] == "envelope_invalid"


def test_an_unresolved_terms_ref_is_reported(tmp_path):
    _, p, _, _, _ = _two_sided(tmp_path)
    report = verify_settlements([p])
    assert report.failures == [{"records": [p["capsule_id"]], "code": "terms_ref_unresolved"}]


def test_an_unknown_payment_reference_type_never_joins(tmp_path):
    ref = {"type": "example.rail_ref", "value": "r-1"}
    terms, p, q, _, _ = _two_sided(tmp_path, payer_kw={"payment_ref": ref}, payee_kw={"payment_ref": ref})
    report = verify_settlements([terms, p, q])
    assert report.settlements[0]["payment_state"] == "unjoined"
    assert [f["code"] for f in report.findings] == ["payment_ref_type_unknown"] * 2


def test_wrapped_content_must_hash_to_its_digest(tmp_path):
    entry = wrap(b"receipt octets", type="x402.receipt", include_content=True)
    entry["digest"] = "0" * 64
    terms, p, q, _, _ = _two_sided(tmp_path, payee_kw={"wrapped": [entry]})
    report = verify_settlements([terms, p, q], wrapped_objects=[EXACT_PAYLOAD])
    assert report.failures == [{"records": [q["capsule_id"]], "code": "wrapped_digest_mismatch"}]


def test_non_dict_inputs_are_reported_not_raised():
    report = verify_settlements(["not a capsule"])
    assert report.failures == [{"records": [None], "code": "capsule_invalid"}]


# --- delivery -----------------------------------------------------------------


def test_delivery_states(tmp_path):
    terms, p, q, payer, payee = _two_sided(tmp_path)
    sent = payee.seal(_delivered(terms["capsule_id"], "payee"))
    received = payer.seal(_delivered(terms["capsule_id"], "payer"))
    other = payer.seal(_delivered(terms["capsule_id"], "payer", digest="e" * 64))
    assert _state([terms, p, q])["delivery_state"] == "none"
    assert _state([terms, p, q, sent])["delivery_state"] == "stated"
    assert _state([terms, p, q, sent, received])["delivery_state"] == "matched"
    assert _state([terms, p, q, sent, other])["delivery_state"] == "mismatch"


def test_delivery_is_checked_against_a_pinned_content_digest(tmp_path):
    payee = Party(tmp_path, "payee")
    terms = _terms(payee, deliverable={"content_digest": "f" * 64})
    sent = payee.seal(_delivered(terms["capsule_id"], "payee"))
    assert _state([terms, sent])["delivery_state"] == "mismatch"


def test_one_key_sealing_both_directions_is_matched_with_a_diagnostic(tmp_path):
    terms, _, _, _, _ = _two_sided(tmp_path)
    one = Party(tmp_path, "one")
    a = one.seal(_delivered(terms["capsule_id"], "payee"))
    b = one.seal(_delivered(terms["capsule_id"], "payer"))
    report = verify_settlements([terms, a, b])
    assert report.settlements[0]["delivery_state"] == "matched"
    assert report.diagnostics == [{"terms": terms["capsule_id"], "code": "delivery_sealed_under_one_key",
                                   "keys": [one.key]}]


def test_a_counterparty_citation_is_carried_as_custody(tmp_path):
    terms, p, _, _, payee = _two_sided(tmp_path)
    q = payee.seal(_payee_leg(terms["capsule_id"]), references=[counterparty_reference(p["capsule_id"])])
    assert q["references"] == [{"type": "agent-action-capsule", "digest_alg": "SHA-256", "digest": p["capsule_id"],
                                "citation_purpose": "counterparty_half"}]
    assert _state([terms, p, q])["payment_state"] == "agreed"
