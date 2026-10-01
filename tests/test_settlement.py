# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.settlement: building, sealing and joining
settlement records sealed independently by a payer and a payee."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from capsule_emit.canonicalization import compute_capsule_id
from capsule_emit.core import _emit_capsule
from capsule_emit.settlement import (
    SETTLEMENT_EXTENSION_KEY,
    SettlementError,
    amount,
    build_observation,
    join,
    seal_observation,
    terms_digest,
    wrap,
)
from capsule_emit.verification import verify_capsule

TX = "0x" + "ab" * 32
NETWORK = "eip155:84532"
REF = {"type": "x402.transaction", "value": TX, "network": NETWORK}
USDC = "eip155:84532/erc20:0x036cbd53842c5426634e7929541ec2318f3dcf7e"
TERMS = {"scheme": "exact", "network": NETWORK, "amount": "10000", "asset": USDC, "payTo": "0x" + "11" * 20}
TERMS_DIGEST = terms_digest(TERMS)
PRICE = amount(10000, USDC, 6)
DELIVERED = "d" * 64


def _seal(tmp_path: Path, party: str, obs: dict, **kw) -> dict:
    return seal_observation(
        obs, operator=party, developer=f"{party}-agent@v1", ledger=tmp_path / party / "ledger.jsonl",
        witness=False, **kw,
    ).capsule


def _obs(role: str, leg: str, **kw) -> dict:
    base = dict(role=role, leg=leg, payment_ref=REF, terms_digest=TERMS_DIGEST)
    if leg == "payer_observed":
        base.update(status="settled", amount=PRICE)
    if leg == "payee_observed":
        base.update(status="received", amount=PRICE)
    if leg == "delivered":
        base.update(delivered_digest=DELIVERED)
    base.update(kw)
    return build_observation(**base)


def _two_sided(tmp_path: Path, payer_kw=None, payee_kw=None, payee_party="payee") -> list[dict]:
    payer = _seal(tmp_path, "payer", _obs("payer", "payer_observed", **(payer_kw or {})))
    payee = _seal(tmp_path, payee_party, _obs("payee", "payee_observed", **(payee_kw or {})))
    return [payer, payee]


# --- building ---------------------------------------------------------------


def test_amount_refuses_floats_and_fractions():
    with pytest.raises(SettlementError):
        amount(0.01, USDC, 6)
    with pytest.raises(SettlementError):
        amount("0.01", USDC, 6)
    with pytest.raises(SettlementError):
        amount(True, USDC, 6)
    assert amount(10000, USDC, 6) == {"value": "10000", "assetCode": USDC, "assetScale": 6}


def test_only_the_payer_seals_payer_observed_and_only_the_payee_payee_observed():
    with pytest.raises(SettlementError, match="only the payer"):
        _obs("payee", "payer_observed")
    with pytest.raises(SettlementError, match="only the payee"):
        _obs("payer", "payee_observed")


def test_status_belongs_to_observed_legs_only():
    with pytest.raises(SettlementError, match="carries no status"):
        _obs("payer", "terms", status="settled")
    with pytest.raises(SettlementError, match="status is one of"):
        _obs("payer", "payer_observed", status="received")


def test_delivered_leg_needs_a_digest_and_only_it_carries_one():
    with pytest.raises(SettlementError, match="delivered_digest"):
        _obs("payee", "delivered", delivered_digest=None)
    with pytest.raises(SettlementError, match="only a delivered leg"):
        _obs("payee", "terms", delivered_digest=DELIVERED)


def test_x402_reference_needs_a_caip2_network_and_an_evm_hash():
    with pytest.raises(SettlementError, match="CAIP-2"):
        _obs("payer", "terms", payment_ref={"type": "x402.transaction", "value": TX})
    with pytest.raises(SettlementError, match="0x"):
        _obs("payer", "terms", payment_ref={**REF, "value": "0x1234"})


def test_evm_hash_is_lowercased_so_both_sides_join():
    obs = _obs("payer", "terms", payment_ref={**REF, "value": TX.upper().replace("0X", "0x")})
    assert obs["payment_ref"]["value"] == TX


def test_unknown_reference_type_is_accepted_and_unknown_fields_are_not():
    obs = _obs("payer", "terms", payment_ref={"type": "example.rail_ref", "value": "r-1"})
    assert obs["payment_ref"]["type"] == "example.rail_ref"
    with pytest.raises(SettlementError, match="unknown fields"):
        _obs("payer", "terms", payment_ref={**REF, "memo": "x"})


def test_known_reference_types_check_their_values():
    with pytest.raises(SettlementError):
        _obs("payer", "terms", payment_ref={"type": "ln.payment_hash", "value": "zz"})
    with pytest.raises(SettlementError):
        _obs("payer", "terms", payment_ref={"type": "iso20022.uetr", "value": "not-a-uuid"})
    with pytest.raises(SettlementError):
        _obs("payer", "terms", payment_ref={"type": "open_payments.incoming_payment", "value": "http://x"})
    _obs("payer", "terms", payment_ref={"type": "iso20022.uetr", "value": "eb6305c9-1f7f-49de-aed0-16487c27b42d"})


def test_binding_names_keep_upstream_case_but_need_a_namespace():
    obs = _obs("payer", "terms", bindings={"erc8004.agentId": "7", "x402.requestHash": "0x" + "00" * 32})
    assert obs["bindings"]["erc8004.agentId"] == "7"
    with pytest.raises(SettlementError, match="namespace"):
        _obs("payer", "terms", bindings={"agentId": "7"})
    with pytest.raises(SettlementError, match="non-empty"):
        _obs("payer", "terms", bindings={"erc8004.agentId": ""})


def test_wrap_digests_bytes_as_given_and_objects_over_jcs():
    import hashlib

    raw = b'{"b":1,"a":2}'
    assert wrap(raw, type="x402.offer")["digest"] == hashlib.sha256(raw).hexdigest()
    assert wrap({"b": 1, "a": 2}, type="x402.offer")["digest"] == hashlib.sha256(b'{"a":2,"b":1}').hexdigest()
    with pytest.raises(SettlementError):
        wrap(raw, type="Offer")


def test_sealed_record_carries_the_block_and_verifies(tmp_path):
    capsule = _seal(tmp_path, "payer", _obs("payer", "payer_observed"))
    block = capsule["model_attestation"]["compute_attestation"][SETTLEMENT_EXTENSION_KEY]
    assert block["payment_ref"] == REF
    assert capsule["action_type"] == "fyi"
    assert verify_capsule(capsule).ok


def test_legs_chain_within_one_party(tmp_path):
    terms = _seal(tmp_path, "payer", _obs("payer", "terms"))
    observed = _seal(tmp_path, "payer", _obs("payer", "payer_observed"), prior=terms["capsule_id"])
    assert observed["chain"]["parent_capsule_id"] == terms["capsule_id"]
    assert observed["chain"]["relation"] == "follows"


# --- join -------------------------------------------------------------------


def test_two_sided_agreed_with_iso20022_codes(tmp_path):
    joins, refused = join(_two_sided(tmp_path))
    assert refused == []
    assert len(joins) == 1
    j = joins[0]
    assert j.state == "agreed_untrusted", j.differences
    assert j.differences == []
    assert j.iso20022 == {"payer_observed": "ACSC", "payee_observed": "ACCC"}
    assert len(j.payer_capsule_ids) == 1 and len(j.payee_capsule_ids) == 1


def test_one_side_held_is_one_sided_never_a_failure(tmp_path):
    payer, payee = _two_sided(tmp_path)
    assert join([payer])[0][0].state == "payer_only"
    assert join([payee])[0][0].state == "payee_only"


def test_amount_mismatch_differs(tmp_path):
    joins, _ = join(_two_sided(tmp_path, payee_kw={"amount": amount(9999, USDC, 6)}))
    assert joins[0].state == "differs"
    assert "amount" in joins[0].differences


def test_asset_mismatch_differs(tmp_path):
    joins, _ = join(_two_sided(tmp_path, payee_kw={"amount": amount(10000, "eip155:84532/erc20:0x00", 6)}))
    assert joins[0].state == "differs"
    assert "amount" in joins[0].differences


def test_terms_mismatch_differs(tmp_path):
    joins, _ = join(_two_sided(tmp_path, payee_kw={"terms_digest": "e" * 64}))
    assert joins[0].state == "differs"
    assert "terms_digest" in joins[0].differences


def test_payee_reports_rejected_differs(tmp_path):
    joins, _ = join(_two_sided(tmp_path, payee_kw={"status": "rejected"}))
    assert joins[0].state == "differs"
    assert "payee_observed.status=rejected" in joins[0].differences
    assert joins[0].iso20022["payee_observed"] == "RJCT"


def test_same_key_on_both_sides_is_not_independent(tmp_path):
    joins, _ = join(_two_sided(tmp_path, payee_party="payer"))
    assert joins[0].state == "not_independent"


def test_same_hash_on_another_network_does_not_join(tmp_path):
    other = {**REF, "network": "eip155:8453"}
    payer = _seal(tmp_path, "payer", _obs("payer", "payer_observed"))
    payee = _seal(tmp_path, "payee", _obs("payee", "payee_observed", payment_ref=other))
    joins, _ = join([payer, payee])
    assert sorted(j.state for j in joins) == ["payee_only", "payer_only"]


def test_delivery_matched_and_differs(tmp_path):
    base = _two_sided(tmp_path)
    payee_d = _seal(tmp_path, "payee", _obs("payee", "delivered"))
    payer_d = _seal(tmp_path, "payer", _obs("payer", "delivered"))
    joins, _ = join(base + [payee_d, payer_d])
    assert (joins[0].state, joins[0].delivery) == ("agreed_untrusted", "matched")

    payer_bad = _seal(tmp_path, "payer", _obs("payer", "delivered", delivered_digest="f" * 64))
    joins, _ = join(base + [payee_d, payer_bad])
    assert joins[0].delivery == "differs"
    assert joins[0].state == "differs"


def test_delivered_bound_to_other_terms_differs(tmp_path):
    payee_d = _seal(tmp_path, "payee", _obs("payee", "delivered", terms_digest="e" * 64))
    joins, _ = join(_two_sided(tmp_path) + [payee_d])
    assert joins[0].state == "differs"
    assert "terms_digest" in joins[0].differences


def test_chained_legs_join_with_or_without_their_parents(tmp_path):
    records = []
    for party in ("payer", "payee"):
        terms = _seal(tmp_path, party, _obs(party, "terms"))
        observed = _seal(tmp_path, party, _obs(party, f"{party}_observed"), prior=terms["capsule_id"])
        delivered = _seal(tmp_path, party, _obs(party, "delivered"), prior=observed["capsule_id"])
        records += [terms, observed, delivered]
    joins, refused = join(records)
    assert refused == []
    assert (joins[0].state, joins[0].delivery) == ("agreed_untrusted", "matched")
    observed_only = [records[1], records[4]]
    joins, refused = join(observed_only)
    assert refused == []
    assert joins[0].state == "agreed_untrusted"


def test_missing_amount_on_an_observed_leg_is_never_agreed(tmp_path):
    joins, _ = join(_two_sided(tmp_path, payee_kw={"amount": None}))
    assert joins[0].state == "differs"
    assert "payee_observed.amount missing" in joins[0].differences
    joins, _ = join(_two_sided(tmp_path / "b", payer_kw={"amount": None}, payee_kw={"amount": None}))
    assert joins[0].state == "differs"


def test_same_sum_at_another_scale_differs(tmp_path):
    joins, _ = join(_two_sided(tmp_path, payee_kw={"amount": amount(10000000, USDC, 9)}))
    assert "amount" in joins[0].differences


def test_pending_then_settled_is_agreed_and_pending_alone_is_not(tmp_path):
    pending = _seal(tmp_path, "payer", _obs("payer", "payer_observed", status="pending"))
    payer, payee = _two_sided(tmp_path)
    joins, _ = join([pending, payer, payee])
    assert joins[0].state == "agreed_untrusted", joins[0].differences
    assert joins[0].iso20022["payer_observed"] == "ACSC"
    joins, _ = join([pending, payee])
    assert joins[0].state == "differs"
    assert joins[0].iso20022["payer_observed"] == "PDNG"


def test_two_outcomes_on_one_side_differ(tmp_path):
    rejected = _seal(tmp_path, "payer", _obs("payer", "payer_observed", status="rejected"))
    payer, payee = _two_sided(tmp_path)
    joins, _ = join([rejected, payer, payee])
    assert joins[0].state == "differs"
    assert "payer_observed.status" in joins[0].differences
    assert "payer_observed" not in joins[0].iso20022


def test_duplicate_observed_legs_with_different_amounts_differ(tmp_path):
    extra = _seal(tmp_path, "payer", _obs("payer", "payer_observed", amount=amount(1, USDC, 6)))
    joins, _ = join(_two_sided(tmp_path) + [extra])
    assert joins[0].state == "differs"
    assert "amount" in joins[0].differences


def test_one_key_sealing_delivery_for_both_roles_is_not_matched(tmp_path):
    a = _seal(tmp_path, "one", _obs("payer", "delivered"))
    b = _seal(tmp_path, "one", _obs("payee", "delivered"))
    joins, _ = join([a, b])
    assert joins[0].delivery == "not_independent"
    joins, _ = join(_two_sided(tmp_path) + [a, b])
    assert joins[0].state == "not_independent"


def test_one_sided_delivery(tmp_path):
    payee_d = _seal(tmp_path, "payee", _obs("payee", "delivered"))
    joins, _ = join(_two_sided(tmp_path) + [payee_d])
    assert (joins[0].state, joins[0].delivery) == ("agreed_untrusted", "payee_only")
    payer_d = _seal(tmp_path, "payer", _obs("payer", "delivered"))
    joins, _ = join([payer_d])
    assert (joins[0].state, joins[0].delivery) == ("no_observation", "payer_only")


def test_trusted_keys_refuse_a_role_claim_from_another_key(tmp_path):
    payer, payee = _two_sided(tmp_path)
    impostor = _seal(tmp_path, "impostor", _obs("payee", "payee_observed"))
    trusted = {"payer": [payer["key_id"]], "payee": [payee["key_id"]]}
    joins, refused = join([payer, payee, impostor], trusted_keys=trusted)
    assert [r.capsule_id for r in refused] == [impostor["capsule_id"]]
    assert "not trusted" in refused[0].reason
    assert joins[0].state == "differs"
    assert joins[0].differences == ["payee.untrusted_key"]
    _, refused = join([payer, payee], trusted_keys={"payer": [payer["key_id"]]})
    assert [r.capsule_id for r in refused] == [payee["capsule_id"]]


def test_non_x402_reference_joins_end_to_end(tmp_path):
    ref = {"type": "iso20022.uetr", "value": "eb6305c9-1f7f-49de-aed0-16487c27b42d"}
    joins, _ = join(_two_sided(tmp_path, payer_kw={"payment_ref": ref}, payee_kw={"payment_ref": ref}))
    assert len(joins) == 1 and joins[0].state == "agreed_untrusted"


def test_references_differing_only_in_case_do_not_join(tmp_path):
    a = {"type": "ap2.payment_id", "value": "PAY-1"}
    b = {"type": "ap2.payment_id", "value": "pay-1"}
    joins, _ = join(_two_sided(tmp_path, payer_kw={"payment_ref": a}, payee_kw={"payment_ref": b}))
    assert sorted(j.state for j in joins) == ["payee_only", "payer_only"]


def test_without_trusted_keys_a_full_match_is_only_agreed_untrusted(tmp_path):
    payer, payee = _two_sided(tmp_path)
    assert join([payer, payee])[0][0].state == "agreed_untrusted"
    trusted = {"payer": [payer["key_id"]], "payee": [payee["key_id"]]}
    assert join([payer, payee], trusted_keys=trusted)[0][0].state == "agreed"


def test_a_third_party_key_claiming_the_payee_role_is_not_agreed(tmp_path):
    payer, _ = _two_sided(tmp_path)
    stranger = _seal(tmp_path, "stranger", _obs("payee", "payee_observed"))
    joins, _ = join([payer, stranger])
    assert joins[0].state == "agreed_untrusted"  # nothing can tell a stranger from the payee
    trusted = {"payer": [payer["key_id"]], "payee": ["e" * 64]}
    joins, refused = join([payer, stranger], trusted_keys=trusted)
    assert joins[0].state == "payer_only" and len(refused) == 1


def test_real_payee_plus_an_impostor_is_a_difference(tmp_path):
    payer, payee = _two_sided(tmp_path)
    impostor = _seal(tmp_path, "impostor", _obs("payee", "payee_observed"))
    joins, _ = join([payer, payee, impostor])
    assert joins[0].state == "differs"
    assert "payee.keys" in joins[0].differences


def test_rotated_keys_all_trusted_are_agreed(tmp_path):
    payer, payee = _two_sided(tmp_path)
    rotated = _seal(tmp_path, "payee-rotated", _obs("payee", "delivered"))
    payer_d = _seal(tmp_path, "payer", _obs("payer", "delivered"))
    trusted = {"payer": [payer["key_id"]], "payee": [payee["key_id"], rotated["key_id"]]}
    joins, refused = join([payer, payee, rotated, payer_d], trusted_keys=trusted)
    assert refused == []
    assert (joins[0].state, joins[0].delivery) == ("agreed", "matched"), joins[0].differences
    joins, _ = join([payer, payee, rotated, payer_d])
    assert "payee.keys" in joins[0].differences


def test_an_untrusted_extra_key_beside_trusted_rotated_keys_is_a_difference(tmp_path):
    payer, payee = _two_sided(tmp_path)
    rotated = _seal(tmp_path, "payee-rotated", _obs("payee", "delivered"))
    extra = _seal(tmp_path, "extra", _obs("payee", "payee_observed"))
    trusted = {"payer": [payer["key_id"]], "payee": [payee["key_id"], rotated["key_id"]]}
    joins, refused = join([payer, payee, rotated, extra], trusted_keys=trusted)
    assert [r.capsule_id for r in refused] == [extra["capsule_id"]]
    assert joins[0].state == "differs"
    assert joins[0].differences == ["payee.untrusted_key"]


def test_the_same_capsule_passed_twice_counts_once(tmp_path):
    payer, payee = _two_sided(tmp_path)
    extra = _seal(tmp_path, "payer", _obs("payer", "payer_observed", amount=amount(1, USDC, 6)))
    joins, refused = join([payer, payer, payee, payee])
    assert refused == []
    assert joins[0].payer_capsule_ids == [payer["capsule_id"]]
    assert joins[0].state == "agreed_untrusted"
    joins, _ = join([payer, extra, extra, payee])
    assert joins[0].payer_capsule_ids == [payer["capsule_id"], extra["capsule_id"]]


# --- adversarial inputs to the join ----------------------------------------


def test_edited_record_is_refused_not_joined(tmp_path):
    payer, payee = _two_sided(tmp_path)
    forged = copy.deepcopy(payee)
    forged["model_attestation"]["compute_attestation"][SETTLEMENT_EXTENSION_KEY]["amount"]["value"] = "1"
    joins, refused = join([payer, forged])
    assert [r.capsule_id for r in refused] == [forged["capsule_id"]]
    assert joins[0].state == "payer_only"


def test_edited_record_with_recomputed_id_and_stripped_signature_is_refused(tmp_path):
    payer, payee = _two_sided(tmp_path)
    forged = copy.deepcopy(payee)
    forged["model_attestation"]["compute_attestation"][SETTLEMENT_EXTENSION_KEY]["amount"]["value"] = "1"
    forged.pop("signature")
    forged.pop("key_id")
    forged["capsule_id"] = compute_capsule_id(forged)
    joins, refused = join([payer, forged])
    assert len(refused) == 1
    assert joins[0].state == "payer_only"


def test_structurally_invalid_capsule_with_a_valid_signature_is_refused(tmp_path):
    from capsule_emit.signing import resolve_signer, sign_producer_envelope

    payer, payee = _two_sided(tmp_path)
    forged = copy.deepcopy(payee)
    forged.pop("signature")
    forged.pop("key_id")
    forged.pop("operator")  # a required field: the content check, not the signature, catches this
    forged["capsule_id"] = compute_capsule_id(forged)
    signer = resolve_signer(tmp_path / "attacker" / "ledger.jsonl")
    forged["signature"], forged["key_id"] = sign_producer_envelope(signer, forged["capsule_id"])
    joins, refused = join([payer, forged])
    assert [r.reason for r in refused] == ["capsule content does not verify"]
    assert joins[0].state == "payer_only"


def test_signed_record_with_malformed_block_is_refused(tmp_path):
    bad = dict(_obs("payer", "payer_observed"))
    bad["amount"] = {"value": "1.5", "assetCode": USDC, "assetScale": 6}
    capsule = _emit_capsule(
        "settlement.payer_observed", ledger=tmp_path / "x" / "ledger.jsonl", witness=False,
        extra_compute={SETTLEMENT_EXTENSION_KEY: bad},
    ).capsule
    joins, refused = join([capsule])
    assert joins == []
    assert "settlement block refused" in refused[0].reason


def test_upper_case_key_id_cannot_pass_as_a_second_key(tmp_path):
    payer, payee = _two_sided(tmp_path, payee_party="payer")
    payee["key_id"] = payee["key_id"].upper()
    joins, refused = join([payer, payee])
    assert [r.reason for r in refused] == ["key_id is not 64 lowercase hex"]
    assert joins[0].state == "payer_only"


def test_unsigned_record_with_a_correct_id_is_refused(tmp_path):
    payer, payee = _two_sided(tmp_path)
    payee.pop("signature")
    joins, refused = join([payer, payee])
    assert len(refused) == 1
    assert joins[0].state == "payer_only"


def test_non_dict_inputs_are_refused_not_raised(tmp_path):
    payer, _ = _two_sided(tmp_path)
    odd = copy.deepcopy(payer)
    odd["model_attestation"] = "x"
    joins, refused = join(["not a capsule", odd])
    assert joins == [] and len(refused) == 2


def test_capsule_without_a_block_is_refused(tmp_path):
    capsule = _emit_capsule("other", ledger=tmp_path / "x" / "ledger.jsonl", witness=False).capsule
    joins, refused = join([capsule])
    assert joins == [] and refused[0].reason == "no settlement block"
