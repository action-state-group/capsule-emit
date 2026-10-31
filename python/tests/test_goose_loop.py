# SPDX-License-Identifier: Apache-2.0
"""The Goose loop extension (examples/goose-capsule/loop_server.py): one test group per
loop step it exposes, against two nodes that each keep their own book."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp package not installed")

_SRC = Path(__file__).resolve().parents[2] / "examples" / "goose-capsule" / "loop_server.py"
_spec = importlib.util.spec_from_file_location("goose_loop_server", _SRC)
loop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loop)

TERMS = json.dumps({"item": "widget", "qty": 3})
ACTION = "buy_widget"
HEX64 = "ab" * 32


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    monkeypatch.delenv("CAPSULE_LEDGER", raising=False)


@pytest.fixture
def books(tmp_path):
    return str(tmp_path / "buyer"), str(tmp_path / "seller")


def call(fn, **kw) -> dict:
    return json.loads(fn(**kw))


def test_every_step_tool_is_registered():
    import asyncio

    tools = loop.server.list_tools()
    tools = asyncio.run(tools) if asyncio.iscoroutine(tools) else tools
    tools = getattr(tools, "tools", tools)
    assert {
        "loop_join", "loop_probe", "loop_book", "loop_admit", "loop_deal_open", "loop_deal_respond",
        "loop_deal_push", "loop_deal_reconcile", "loop_settle", "loop_settle_verify",
    } <= {t.name for t in tools}


# 12 Join


def test_join_opens_a_book_and_a_stranger_probe_verifies_it(books):
    buyer, _ = books
    joined = call(loop.loop_join, operator="buyer-co", ledger=buyer)
    assert joined["ok"] and joined["position"] == 0
    assert joined["commitment"].startswith("NOT_CHECKED")
    probe = call(loop.loop_probe, ledger=buyer)
    assert probe == {"checked": 1, "failures": [], "ok": True, "step": "12"}


def test_probe_of_an_empty_book_is_not_ok(books):
    assert call(loop.loop_probe, ledger=books[0])["ok"] is False


def test_probe_reports_a_tampered_record(books, tmp_path):
    buyer, _ = books
    call(loop.loop_join, operator="buyer-co", ledger=buyer)
    rec = loop._capsules(buyer)[0]
    rec["operator"] = "someone-else"
    probe = call(loop.loop_probe, ledger=_flat(tmp_path, [rec]))
    assert probe["ok"] is False and probe["failures"]


def _flat(tmp_path, recs) -> str:
    p = tmp_path / "copy.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return str(p)


# 02 Book


def test_book_gives_position_and_is_not_checked_without_a_verified_receipt(books):
    buyer, _ = books
    call(loop.loop_join, operator="buyer-co", ledger=buyer)
    adm = call(loop.loop_admit, peer="seller-co", decision="GO", operator="buyer-co", ledger=buyer)
    out = call(loop.loop_book, capsule_id=adm["capsule_id"], ledger=buyer)
    assert out["ok"] and out["position"] == 1 and out["capsule_count"] == 2
    assert out["witness_receipt"].startswith("NOT_CHECKED")


def test_book_with_a_stub_checkpoint_stays_not_checked(books, monkeypatch):
    buyer, _ = books
    monkeypatch.setenv("CAPSULE_WITNESS", "stub")
    joined = call(loop.loop_join, operator="buyer-co", ledger=buyer)
    out = call(loop.loop_book, capsule_id=joined["capsule_id"], ledger=buyer, checkpoint_now=True)
    assert out["ok"] and out["checkpoint"] is not None
    assert out["witness_receipt"].startswith("NOT_CHECKED")


def test_book_refuses_a_short_or_unknown_prefix(books):
    buyer, _ = books
    call(loop.loop_join, operator="buyer-co", ledger=buyer)
    assert call(loop.loop_book, capsule_id="abc", ledger=buyer)["ok"] is False
    assert call(loop.loop_book, capsule_id="deadbeefdeadbeef", ledger=buyer)["ok"] is False


# 04 Admit


def test_admit_rejects_a_decision_other_than_go_or_no(books):
    assert call(loop.loop_admit, peer="p", decision="MAYBE", ledger=books[0])["ok"] is False


def test_admit_seals_before_the_deal_and_the_deal_cites_it(books):
    buyer, _ = books
    adm = call(loop.loop_admit, peer="seller-co", decision="go", operator="buyer-co", ledger=buyer)
    assert adm["ok"] and adm["decision"] == "GO"
    deal = call(loop.loop_deal_open, peer="seller-co", action=ACTION, terms=TERMS, operator="buyer-co", ledger=buyer)
    assert deal["ok"] and deal["admission"] == adm["capsule_id"]
    order = [r["capsule_id"] for r in loop._capsules(buyer)]
    assert order.index(adm["capsule_id"]) < order.index(deal["capsule_id"])


def test_a_deal_with_no_admission_is_refused(books):
    out = call(loop.loop_deal_open, peer="seller-co", action=ACTION, terms=TERMS, ledger=books[0])
    assert out["ok"] is False and "loop_admit" in out["error"]


def test_a_no_admission_blocks_the_deal_and_a_later_go_lifts_it(books):
    buyer, _ = books
    call(loop.loop_admit, peer="seller-co", decision="NO", operator="buyer-co", ledger=buyer)
    out = call(loop.loop_deal_open, peer="seller-co", action=ACTION, terms=TERMS, ledger=buyer)
    assert out["ok"] is False and "NO" in out["error"]
    call(loop.loop_admit, peer="seller-co", decision="GO", operator="buyer-co", ledger=buyer)
    assert call(loop.loop_deal_open, peer="seller-co", action=ACTION, terms=TERMS, operator="buyer-co", ledger=buyer)["ok"]


# 05 Deal and push


def _open_and_push(books, tmp_path):
    buyer, seller = books
    call(loop.loop_admit, peer="seller-co", decision="GO", operator="buyer-co", ledger=buyer)
    req = call(loop.loop_deal_open, peer="seller-co", action=ACTION, terms=TERMS, operator="buyer-co", ledger=buyer)
    req_file = str(tmp_path / "request.json")
    call(loop.loop_deal_push, capsule_ids=[req["capsule_id"]], out_path=req_file, ledger=buyer)
    return req, req_file


def test_both_sides_seal_halves_push_them_and_reconcile(books, tmp_path):
    buyer, seller = books
    req, req_file = _open_and_push(books, tmp_path)
    call(loop.loop_admit, peer="buyer-co", decision="GO", operator="seller-co", ledger=seller)
    resp = call(loop.loop_deal_respond, request_file=req_file, action=ACTION, terms=TERMS,
                operator="seller-co", ledger=seller)
    assert resp["ok"] and resp["cites"] == req["capsule_id"]
    half = [r for r in loop._capsules(seller) if r["capsule_id"] == resp["capsule_id"]][0]
    cited = json.dumps(half)
    assert req["capsule_id"] in cited
    resp_file = str(tmp_path / "response.json")
    assert call(loop.loop_deal_push, capsule_ids=[resp["capsule_id"]], out_path=resp_file, ledger=seller)["ok"]
    rec = call(loop.loop_deal_reconcile, peer_file=resp_file, ledger=buyer)
    assert rec["fold"]["matched"] == 1 and rec["fold"]["total"] == 1


def test_reconcile_with_nothing_pushed_is_requester_only(books, tmp_path):
    buyer, _ = books
    _open_and_push(books, tmp_path)
    empty = tmp_path / "none.json"
    empty.write_text("[]")
    rec = call(loop.loop_deal_reconcile, peer_file=str(empty), ledger=buyer)
    assert rec["fold"]["requester_only"] == 1 and rec["fold"]["matched"] == 0


def test_respond_refuses_an_unadmitted_requester(books, tmp_path):
    _, seller = books
    _, req_file = _open_and_push(books, tmp_path)
    out = call(loop.loop_deal_respond, request_file=req_file, action=ACTION, terms=TERMS, operator="seller-co", ledger=seller)
    assert out["ok"] is False and "buyer-co" in out["error"]


def test_respond_refuses_terms_the_request_did_not_commit_to(books, tmp_path):
    _, seller = books
    _, req_file = _open_and_push(books, tmp_path)
    call(loop.loop_admit, peer="buyer-co", decision="GO", operator="seller-co", ledger=seller)
    out = call(loop.loop_deal_respond, request_file=req_file, action=ACTION,
               terms=json.dumps({"item": "widget", "qty": 99}), operator="seller-co", ledger=seller)
    assert out["ok"] is False and "different terms" in out["error"]


def test_respond_refuses_a_tampered_request(books, tmp_path):
    _, seller = books
    _, req_file = _open_and_push(books, tmp_path)
    recs = json.loads(Path(req_file).read_text())
    recs[0]["operator"] = "mallory"
    Path(req_file).write_text(json.dumps(recs[0]))
    call(loop.loop_admit, peer="mallory", decision="GO", operator="seller-co", ledger=seller)
    out = call(loop.loop_deal_respond, request_file=req_file, action=ACTION, terms=TERMS, ledger=seller)
    assert out["ok"] is False and "does not verify" in out["error"]


def test_push_refuses_a_record_not_in_the_book(books, tmp_path):
    out = call(loop.loop_deal_push, capsule_ids=["f" * 64], out_path=str(tmp_path / "x.json"), ledger=books[0])
    assert out["ok"] is False


def test_deal_open_refuses_non_json_terms(books):
    buyer, _ = books
    call(loop.loop_admit, peer="seller-co", decision="GO", operator="buyer-co", ledger=buyer)
    assert call(loop.loop_deal_open, peer="seller-co", action=ACTION, terms="{not json", ledger=buyer)["ok"] is False


# 06 Settle


def _leg(ledger, **kw):
    base = dict(operator="buyer-co", ledger=ledger)
    base.update(kw)
    return call(loop.loop_settle, **base)


def test_settlement_legs_seal_and_verify_across_two_books(books, tmp_path):
    buyer, seller = books
    ref = json.dumps({"type": "x402.transaction", "value": "0x" + "1" * 64, "network": "eip155:8453"})
    terms = _leg(seller, leg="terms", sealer_role="payee", amount_value="1000000", asset_code="USDC",
                 asset_scale=6, operator="seller-co")
    assert terms["ok"], terms
    paid = _leg(buyer, leg="payer_observed", sealer_role="payer", amount_value="1000000", asset_code="USDC",
                asset_scale=6, terms_ref=terms["capsule_id"], payment_ref=ref, status="settled",
                observed_at="2026-10-31T12:00:00Z", counterparty_capsule_id=terms["capsule_id"])
    assert paid["ok"], paid
    peer_file = str(tmp_path / "seller-legs.json")
    call(loop.loop_deal_push, capsule_ids=[terms["capsule_id"]], out_path=peer_file, ledger=seller)
    rep = call(loop.loop_settle_verify, peer_file=peer_file, ledger=buyer)
    assert rep["ok"] and rep["legs"] == 2 and rep["report"]["failures"] == []
    assert rep["report"]["key_policy_applied"] is False


def test_settle_verify_reports_a_nonconforming_leg_and_refuses_a_bad_peer_file(books, tmp_path):
    buyer, _ = books
    call(loop.loop_admit, peer="p", decision="NO", reason="mentions settlement here", ledger=buyer)
    terms = _leg(buyer, leg="terms", sealer_role="payee", amount_value="5", asset_code="USDC", asset_scale=6)
    bad = loop._capsules(buyer)[-1]
    bad[loop._settlement.SETTLEMENT_MEMBER] = {"version": "x"}
    peer = tmp_path / "legs.json"
    peer.write_text(json.dumps([bad]))
    rep = call(loop.loop_settle_verify, peer_file=str(peer), ledger=buyer)
    assert terms["ok"] and rep["ok"] is False and rep["legs"] == 2
    for body in ("null", "7", '{"a": 1}', "[1]"):
        peer.write_text(body)
        assert call(loop.loop_settle_verify, peer_file=str(peer), ledger=buyer)["ok"] is False


def test_settlement_refuses_a_float_amount_and_a_wrong_role(books):
    buyer, _ = books
    bad = _leg(buyer, leg="terms", sealer_role="payee", amount_value="1.5", asset_code="USDC", asset_scale=6)
    assert bad["ok"] is False and "amount_not_exact" in bad["error"]
    wrong = _leg(buyer, leg="payer_observed", sealer_role="payee", amount_value="1", asset_code="USDC",
                 asset_scale=6, terms_ref=HEX64, payment_ref=json.dumps({"type": "acp.order_id", "value": "o1"}),
                 status="settled", observed_at="2026-10-31T12:00:00Z")
    assert wrong["ok"] is False


def test_settlement_refuses_malformed_payment_ref_json(books):
    out = _leg(books[0], leg="payer_observed", sealer_role="payer", amount_value="1", asset_code="USDC",
               asset_scale=6, terms_ref=HEX64, payment_ref="{", status="settled", observed_at="2026-10-31T12:00:00Z")
    assert out["ok"] is False and "payment_ref" in out["error"]


# 03, 07, 09: stated NOT_CHECKED, no tool


def test_unmapped_steps_have_no_tool_and_the_doc_says_so():
    names = {n for n in dir(loop) if n.startswith("loop_")}
    assert not {n for n in names if any(w in n for w in ("advertise", "ask", "dispute", "adjudicate"))}
    for step in ("03", "07", "09"):
        assert f"    {step}    none" in loop.__doc__


# review fixes


def _answered(books, tmp_path, terms=TERMS):
    buyer, seller = books
    req, req_file = _open_and_push(books, tmp_path)
    call(loop.loop_admit, peer="buyer-co", decision="GO", operator="seller-co", ledger=seller)
    resp = call(loop.loop_deal_respond, request_file=req_file, action=ACTION, terms=terms,
                operator="seller-co", ledger=seller)
    resp_file = str(tmp_path / "response.json")
    call(loop.loop_deal_push, capsule_ids=[resp["capsule_id"]], out_path=resp_file, ledger=seller)
    return req, resp, resp_file


def test_reconcile_pairs_by_request_so_a_repeat_order_is_not_matched_by_one_response(books, tmp_path):
    buyer, _ = books
    _, _, resp_file = _answered(books, tmp_path)
    call(loop.loop_deal_open, peer="seller-co", action=ACTION, terms=TERMS, operator="buyer-co", ledger=buyer)
    rec = call(loop.loop_deal_reconcile, peer_file=resp_file, ledger=buyer)
    assert rec["fold"] == {"matched": 1, "requester_only": 1, "contradicted": 0, "total": 2}


def _forged_half(tmp_path, req, operator, digest, name):
    """A responder half citing *req*, sealed straight through the library (the tools refuse to)."""
    from capsule_emit.bilateral import seal_action

    book = str(tmp_path / name)
    res = seal_action(operator, "x", ACTION, digest, req["capsule_id"], ledger=book,
                      extra_compute={"request_capsule_id": req["capsule_id"]})
    f = tmp_path / f"{name}.json"
    f.write_text(json.dumps([res.capsule]))
    return str(f)


def test_reconcile_ignores_a_response_from_someone_other_than_the_peer(books, tmp_path):
    buyer, _ = books
    req, _ = _open_and_push(books, tmp_path)
    digest = loop._compute(loop._capsules(buyer)[-1])["action_digest"]
    f = _forged_half(tmp_path, req, "carol-co", digest, "carol")
    rec = call(loop.loop_deal_reconcile, peer_file=f, ledger=buyer)
    assert rec["fold"]["matched"] == 0 and rec["fold"]["requester_only"] == 1


def test_reconcile_reports_a_digest_that_differs_as_contradicted(books, tmp_path):
    buyer, _ = books
    req, _ = _open_and_push(books, tmp_path)
    f = _forged_half(tmp_path, req, "seller-co", "0" * 64, "seller")
    rec = call(loop.loop_deal_reconcile, peer_file=f, ledger=buyer)
    assert rec["fold"]["contradicted"] == 1 and rec["fold"]["matched"] == 0


def test_respond_refuses_a_request_addressed_to_another_node(books, tmp_path):
    _, seller = books
    _, req_file = _open_and_push(books, tmp_path)
    call(loop.loop_admit, peer="buyer-co", decision="GO", operator="seller-co", ledger=seller)
    out = call(loop.loop_deal_respond, request_file=req_file, action=ACTION, terms=TERMS, operator="carol-co", ledger=seller)
    assert out["ok"] is False and "addressed to" in out["error"]


def test_respond_refuses_a_response_half_passed_as_the_request(books, tmp_path):
    _, _, resp_file = _answered(books, tmp_path)
    out = call(loop.loop_deal_respond, request_file=resp_file, action=ACTION, terms=TERMS,
               operator="buyer-co", ledger=books[0])
    assert out["ok"] is False and "exactly one request half" in out["error"]


def test_admission_bound_to_a_key_refuses_a_request_signed_by_another(books, tmp_path):
    _, seller = books
    req, req_file = _open_and_push(books, tmp_path)
    call(loop.loop_admit, peer="buyer-co", decision="GO", peer_key_id="0" * 64, operator="seller-co", ledger=seller)
    out = call(loop.loop_deal_respond, request_file=req_file, action=ACTION, terms=TERMS, operator="seller-co", ledger=seller)
    assert out["ok"] is False and "key" in out["error"]
    key_id = json.loads(Path(req_file).read_text())[0]["key_id"]
    call(loop.loop_admit, peer="buyer-co", decision="GO", peer_key_id=key_id, operator="seller-co", ledger=seller)
    assert call(loop.loop_deal_respond, request_file=req_file, action=ACTION, terms=TERMS,
                operator="seller-co", ledger=seller)["ok"]


def test_probe_fails_on_a_corrupt_line_in_a_flat_book(books, tmp_path):
    buyer, _ = books
    call(loop.loop_join, operator="buyer-co", ledger=buyer)
    path = _flat(tmp_path, loop._capsules(buyer))
    assert call(loop.loop_probe, ledger=path)["ok"] is True
    Path(path).write_text(Path(path).read_text() + "{truncated\n")
    out = call(loop.loop_probe, ledger=path)
    assert out["ok"] is False and "corrupt" in out["failures"][0]["gap"]


def test_book_uses_the_leaf_position_and_does_not_cover_a_record_after_the_checkpoint(books, monkeypatch):
    buyer, _ = books
    monkeypatch.setenv("CAPSULE_WITNESS", "stub")
    first = call(loop.loop_join, operator="buyer-co", ledger=buyer)
    call(loop.loop_book, capsule_id=first["capsule_id"], ledger=buyer, checkpoint_now=True)
    late = call(loop.loop_admit, peer="p", decision="GO", operator="buyer-co", ledger=buyer)
    out = call(loop.loop_book, capsule_id=late["capsule_id"], ledger=buyer)
    stamps = [e for e in loop.read_ledger_entries(buyer) if e.get("kind") == "checkpoint_stamp"]
    assert stamps and out["position"] > 1
    assert out["checkpoint"]["covers_record"] is False
    assert out["witness_receipt"].startswith("NOT_CHECKED")


def test_book_refuses_an_ambiguous_prefix(books):
    buyer, _ = books
    for peer in ("a", "b", "c"):
        call(loop.loop_admit, peer=peer, decision="GO", ledger=buyer)
    ids = [r["capsule_id"] for r in loop._capsules(buyer)]
    shared = ""
    for ch in ids[0]:
        if all(i.startswith(shared + ch) for i in ids):
            shared += ch
    if len(shared) >= 8:  # only when the ids happen to share 8 characters
        assert "matches" in call(loop.loop_book, capsule_id=shared, ledger=buyer)["error"]


def test_a_request_resigned_by_another_key_with_the_admitted_key_id_is_refused(books, tmp_path):
    _, seller = books
    req, req_file = _open_and_push(books, tmp_path)
    forged = json.loads(Path(req_file).read_text())
    victim = loop._capsules(books[0])
    real_key = forged[0]["key_id"]
    # Mallory seals the same request under her own key, then copies the admitted key_id over hers.
    from capsule_emit.bilateral import seal_request
    mal = seal_request("buyer-co", "x", ACTION, loop._compute(forged[0])["action_digest"],
                       ledger=str(tmp_path / "mallory"), extra_compute={"peer": "seller-co"}).capsule
    assert mal["key_id"] != real_key and victim
    mal["key_id"] = real_key
    f = tmp_path / "forged.json"
    f.write_text(json.dumps([mal]))
    call(loop.loop_admit, peer="buyer-co", decision="GO", peer_key_id=real_key, operator="seller-co", ledger=seller)
    out = call(loop.loop_deal_respond, request_file=str(f), action=ACTION, terms=TERMS, operator="seller-co", ledger=seller)
    assert out["ok"] is False and "signed" in out["error"]


def test_admission_reports_whether_it_is_bound_to_a_key(books, tmp_path):
    _, seller = books
    _, req_file = _open_and_push(books, tmp_path)
    unbound = call(loop.loop_admit, peer="buyer-co", decision="GO", operator="seller-co", ledger=seller)
    assert unbound["binding"].startswith("unbound")
    resp = call(loop.loop_deal_respond, request_file=req_file, action=ACTION, terms=TERMS, operator="seller-co", ledger=seller)
    assert resp["admission_binding"].startswith("unbound")
    key_id = json.loads(Path(req_file).read_text())[0]["key_id"]
    bound = call(loop.loop_admit, peer="buyer-co", decision="GO", peer_key_id=key_id, operator="seller-co", ledger=seller)
    assert bound["binding"] == "key"


def test_probe_fails_a_record_whose_key_id_was_swapped(books, tmp_path):
    buyer, _ = books
    call(loop.loop_join, operator="buyer-co", ledger=buyer)
    rec = loop._capsules(buyer)[0]
    rec["key_id"] = "0" * 64
    out = call(loop.loop_probe, ledger=_flat(tmp_path, [rec]))
    assert out["ok"] is False
