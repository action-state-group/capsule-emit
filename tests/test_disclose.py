# SPDX-License-Identifier: Apache-2.0
"""Tests for O16 audit item 10 — ``capsule_emit.disclose``.

Builds real checkpoint chains through ``seal()`` + the default witness
wiring (same stub-TS harness as ``tests/test_bundle.py``), then checks that
``disclose()`` assembles bundle + selected payload content + a completeness
statement + audience-suppression profile + its own self-sealed disclosure
record — and that ``verify_disclosure()`` genuinely checks each piece
(every negative case below flips exactly one thing and confirms the mutant
is caught).
"""

from __future__ import annotations

import copy
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
from capsule_emit.bundle import BundleError, bundle
from capsule_emit.checkpoint import emit as checkpoint_emit_mod
from capsule_emit.disclose import DiscloseError, Disclosure, disclose, verify_disclosure

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service — same shape as test_bundle.py
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
    handler_cls = type("_BoundStubWitnessTSHandler", (_StubWitnessTSHandler,), {"received": received})
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
# Three checkpointed records with distinct agent_input/agent_output — the
# common fixture for record-selection, payload, and completeness tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def three_record_ledger(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")
    ts_url, _received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    caps = []
    payloads = []
    for i in range(3):
        agent_input = {"n": i, "kind": "input"}
        agent_output = {"n": i, "kind": "output"}
        result = seal(
            agent_input,
            action=f"act-{i}",
            operator="acme",
            agent_output=agent_output,
            anchor=False,
            ledger=ledger_path,
            witness_url=ts_url,
        )
        caps.append(result.capsule)
        payloads.append((agent_input, agent_output))
    assert _wait_for(lambda: _stamp_count(ledger_path) >= 1)

    return ledger_path, caps, payloads


def _reveal_all(caps, payloads):
    return {
        caps[i]["capsule_id"]: {"agent_input": payloads[i][0], "agent_output": payloads[i][1]}
        for i in range(len(caps))
    }


# ---------------------------------------------------------------------------
# Record selection — single id, contiguous range, explicit list.
# ---------------------------------------------------------------------------


def test_disclose_single_id_is_contiguous(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    d = disclose(
        ledger_path,
        cid,
        audience="auditor",
        reveal={cid: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]}},
    )
    assert d.record_ids == (cid,)
    assert d.completeness["records_mode"] == "contiguous"
    ok, errors = verify_disclosure(d)
    assert ok, errors


def test_disclose_accepts_unambiguous_prefix(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    prefix = cid[:10]
    d = disclose(ledger_path, prefix, audience="auditor", payloads="selected")
    assert d.record_ids == (cid,)


def test_disclose_range_is_contiguous(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    selector = f"{caps[0]['capsule_id']}..{caps[2]['capsule_id']}"
    d = disclose(ledger_path, selector, audience="auditor", reveal=_reveal_all(caps, payloads))
    assert d.record_ids == tuple(c["capsule_id"] for c in caps)
    assert d.completeness["records_mode"] == "contiguous"
    ok, errors = verify_disclosure(d)
    assert ok, errors


def test_disclose_explicit_list_is_producer_selected(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    selector = f"{caps[0]['capsule_id']},{caps[2]['capsule_id']}"  # skips caps[1]
    reveal = {
        caps[0]["capsule_id"]: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]},
        caps[2]["capsule_id"]: {"agent_input": payloads[2][0], "agent_output": payloads[2][1]},
    }
    d = disclose(ledger_path, selector, audience="auditor", reveal=reveal)
    assert d.record_ids == (caps[0]["capsule_id"], caps[2]["capsule_id"])
    assert d.completeness["records_mode"] == "producer-selected"
    ok, errors = verify_disclosure(d)
    assert ok, errors


def test_disclose_explicit_list_dedupes_and_orders_by_ledger_position(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    selector = f"{caps[2]['capsule_id']},{caps[0]['capsule_id']},{caps[0]['capsule_id']}"
    d = disclose(ledger_path, selector, audience="auditor", payloads="selected")
    assert d.record_ids == (caps[0]["capsule_id"], caps[2]["capsule_id"])


def test_disclose_range_backwards_raises(three_record_ledger):
    ledger_path, caps, _payloads = three_record_ledger
    selector = f"{caps[2]['capsule_id']}..{caps[0]['capsule_id']}"
    with pytest.raises(DiscloseError, match="backwards"):
        disclose(ledger_path, selector, audience="auditor", payloads="selected")


def test_disclose_unknown_capsule_id_raises(three_record_ledger):
    ledger_path, _caps, _payloads = three_record_ledger
    with pytest.raises(DiscloseError, match="no record matches"):
        disclose(ledger_path, "f" * 64, audience="auditor", payloads="selected")


def test_disclose_ambiguous_prefix_raises(three_record_ledger):
    from capsule_emit.disclose import _match_one

    records = [{"capsule_id": "aaaaaaaa1111"}, {"capsule_id": "aaaaaaaa2222"}]
    with pytest.raises(DiscloseError, match="matches 2 records"):
        _match_one(records, "aaaaaaaa")


def test_disclose_cannot_target_a_checkpoint_stamp_entry(three_record_ledger):
    ledger_path, _caps, _payloads = three_record_ledger
    entries = ledger_mod.read_ledger_entries(ledger_path)
    stamp = next(e for e in entries if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND)
    with pytest.raises(DiscloseError, match="no record matches"):
        disclose(ledger_path, stamp["capsule_id"], audience="auditor", payloads="selected")


def test_disclose_empty_ledger_raises(tmp_path):
    with pytest.raises(DiscloseError, match="empty or not found"):
        disclose(tmp_path / "nope.jsonl", "f" * 64, audience="auditor", payloads="selected")


def test_disclose_uncovered_record_raises(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "100")
    ts_url, _received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    result = seal(
        None, action="lonely", operator="acme", anchor=False, ledger=ledger_path, witness_url=ts_url
    )
    with pytest.raises(DiscloseError, match="cannot bundle"):
        disclose(ledger_path, result.capsule["capsule_id"], audience="auditor", payloads="selected")


# ---------------------------------------------------------------------------
# Payload completeness — payloads="all" vs "selected", and the
# equivocation-honesty refusal.
# ---------------------------------------------------------------------------


def test_disclose_payloads_all_requires_every_eligible_field(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    with pytest.raises(DiscloseError, match="payloads='all'"):
        disclose(
            ledger_path,
            cid,
            audience="auditor",
            payloads="all",
            reveal={cid: {"agent_input": payloads[0][0]}},  # agent_output missing
        )


def test_disclose_payloads_selected_allows_partial(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    d = disclose(
        ledger_path,
        cid,
        audience="auditor",
        payloads="selected",
        reveal={cid: {"agent_input": payloads[0][0]}},
    )
    assert d.envelopes[cid]["disclosures"] == {"agent_input": payloads[0][0]}
    assert d.completeness["payloads_mode"] == "selected"
    ok, errors = verify_disclosure(d)
    assert ok, errors


def test_disclose_default_payloads_mode_is_all(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    d = disclose(
        ledger_path,
        cid,
        audience="auditor",
        reveal={cid: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]}},
    )
    assert d.completeness["payloads_mode"] == "all"


def test_disclose_no_reveal_is_bundle_only(three_record_ledger):
    ledger_path, caps, _payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    d = disclose(ledger_path, cid, audience="auditor", payloads="selected")
    assert d.envelopes[cid]["disclosures"] == {}
    ok, errors = verify_disclosure(d)
    assert ok, errors


def test_disclose_invalid_payloads_mode_raises(three_record_ledger):
    ledger_path, caps, _payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    with pytest.raises(DiscloseError, match="payloads must be one of"):
        disclose(ledger_path, cid, audience="auditor", payloads="everything")


def test_disclose_mismatched_payload_digest_raises(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    with pytest.raises(DiscloseError, match="does not match"):
        disclose(
            ledger_path,
            cid,
            audience="auditor",
            payloads="selected",
            reveal={cid: {"agent_input": {"wrong": "value"}}},
        )


def test_disclose_reveal_for_unselected_record_raises(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    selected_cid = caps[0]["capsule_id"]
    other_cid = caps[1]["capsule_id"]
    with pytest.raises(DiscloseError, match="not part of"):
        disclose(
            ledger_path,
            selected_cid,
            audience="auditor",
            payloads="selected",
            reveal={other_cid: {"agent_input": payloads[1][0]}},
        )


def test_disclose_reveal_unknown_field_raises(three_record_ledger):
    ledger_path, caps, _payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    with pytest.raises(DiscloseError, match="not one of"):
        disclose(
            ledger_path,
            cid,
            audience="auditor",
            payloads="selected",
            reveal={cid: {"not_a_field": {}}},
        )


# ---------------------------------------------------------------------------
# Audience-suppression profile.
# ---------------------------------------------------------------------------


def test_disclose_suppress_excludes_field_from_all_requirement(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    d = disclose(
        ledger_path,
        cid,
        audience="public",
        payloads="all",
        reveal={cid: {"agent_input": payloads[0][0]}},
        suppress=["agent_output"],
    )
    assert d.suppressed_fields == ("agent_output",)
    assert "agent_output" not in d.envelopes[cid]["disclosures"]
    assert "agent_output" in d.completeness["payloads_note"]
    ok, errors = verify_disclosure(d)
    assert ok, errors


def test_disclose_suppress_and_reveal_same_field_conflict_raises(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    with pytest.raises(DiscloseError, match="cannot be disclosed and suppressed"):
        disclose(
            ledger_path,
            cid,
            audience="public",
            payloads="selected",
            reveal={cid: {"agent_input": payloads[0][0]}},
            suppress=["agent_input"],
        )


def test_disclose_suppress_unknown_field_raises(three_record_ledger):
    ledger_path, caps, _payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    with pytest.raises(DiscloseError, match="unknown field"):
        disclose(ledger_path, cid, audience="public", payloads="selected", suppress=["ssn"])


# ---------------------------------------------------------------------------
# The self-sealing disclosure record — persisted, signed, and NOT a
# capsule for any other consumer.
# ---------------------------------------------------------------------------


def test_disclosure_record_is_persisted_and_excluded_from_read_ledger(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    before = len(ledger_mod.read_ledger_entries(ledger_path))
    d = disclose(
        ledger_path,
        cid,
        audience="auditor",
        reveal={cid: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]}},
    )
    entries = ledger_mod.read_ledger_entries(ledger_path)
    assert len(entries) == before + 1
    assert entries[-1]["capsule_id"] == d.disclosure_record["capsule_id"]
    assert entries[-1]["kind"] == ledger_mod.DISCLOSURE_RECORD_KIND

    # never leaks into the capsule-only view
    capsule_ids = {c["capsule_id"] for c in ledger_mod.read_ledger(ledger_path)}
    assert d.disclosure_record["capsule_id"] not in capsule_ids


def test_disclosure_record_cannot_be_targeted_by_bundle(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    d = disclose(
        ledger_path,
        cid,
        audience="auditor",
        reveal={cid: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]}},
    )
    with pytest.raises(BundleError, match="no record matches"):
        bundle(ledger_path, d.disclosure_record["capsule_id"])


def test_disclosure_record_becomes_an_mmr_leaf_for_the_next_checkpoint(
    three_record_ledger, stub_ts, monkeypatch
):
    ledger_path, caps, payloads = three_record_ledger
    ts_url, _received = stub_ts
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    cid = caps[0]["capsule_id"]
    d = disclose(
        ledger_path,
        cid,
        audience="auditor",
        reveal={cid: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]}},
    )
    before = _stamp_count(ledger_path)
    seal(None, action="another", operator="acme", anchor=False, ledger=ledger_path, witness_url=ts_url)
    seal(None, action="another2", operator="acme", anchor=False, ledger=ledger_path, witness_url=ts_url)
    assert _wait_for(lambda: _stamp_count(ledger_path) > before)

    # the disclosure record is now indexed as a leaf just like any other
    # entry, via the raw MMR reconstruction (not disclose()'s own
    # capsule-only resolver, which deliberately can't target it)
    from capsule_emit.checkpoint import core as mmr_core
    from capsule_emit.checkpoint.emit import CheckpointRecord

    entries = ledger_mod.read_ledger_entries(ledger_path)
    checkpoints = [
        CheckpointRecord.from_dict(e["checkpoint"])
        for e in entries
        if e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND
    ]
    disclosure_seq = next(
        i for i, e in enumerate(entries, start=1) if e.get("capsule_id") == d.disclosure_record["capsule_id"]
    )
    covering = next(cp for cp in checkpoints if mmr_core.leaf_count(cp.mmr_size) >= disclosure_seq)
    assert mmr_core.leaf_count(covering.mmr_size) >= disclosure_seq


# ---------------------------------------------------------------------------
# Serialization round-trip.
# ---------------------------------------------------------------------------


def test_disclosure_json_roundtrip_still_verifies(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    d = disclose(
        ledger_path,
        cid,
        audience="auditor",
        reveal={cid: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]}},
    )
    raw = json.dumps(d.to_dict())
    restored = Disclosure.from_dict(json.loads(raw))
    ok, errors = verify_disclosure(restored)
    assert ok, errors
    assert restored.to_dict() == d.to_dict()


# ---------------------------------------------------------------------------
# Mutation tests — every negative check in verify_disclosure must actually
# catch its mutant, one field at a time.
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_disclosure(three_record_ledger):
    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    return disclose(
        ledger_path,
        cid,
        audience="auditor",
        reveal={cid: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]}},
    )


def test_verify_disclosure_catches_tampered_record_signature(valid_disclosure):
    d = valid_disclosure
    tampered = dict(d.disclosure_record)
    tampered["signature"] = "00" * 64
    mutant = replace(d, disclosure_record=tampered)
    ok, errors = verify_disclosure(mutant)
    assert not ok
    assert any("signature" in e for e in errors)


def test_verify_disclosure_catches_tampered_record_content(valid_disclosure):
    d = valid_disclosure
    tampered = dict(d.disclosure_record)
    tampered["audience"] = "someone-else"  # content changed, capsule_id/signature stale
    mutant = replace(d, disclosure_record=tampered)
    ok, errors = verify_disclosure(mutant)
    assert not ok
    assert any("signature" in e for e in errors)


def test_verify_disclosure_catches_audience_mismatch(valid_disclosure):
    d = valid_disclosure
    mutant = replace(d, audience="not-the-real-audience")
    ok, errors = verify_disclosure(mutant)
    assert not ok
    assert any("audience" in e for e in errors)


def test_verify_disclosure_catches_record_ids_mismatch(valid_disclosure):
    d = valid_disclosure
    mutant = replace(d, record_ids=("f" * 64,))
    ok, errors = verify_disclosure(mutant)
    assert not ok
    assert any("disclosed_capsule_ids" in e for e in errors)


def test_verify_disclosure_catches_missing_bundle(valid_disclosure):
    d = valid_disclosure
    mutant = replace(d, bundles={})
    ok, errors = verify_disclosure(mutant)
    assert not ok
    assert any("missing bundle" in e for e in errors)


def test_verify_disclosure_catches_tampered_bundle(valid_disclosure):
    d = valid_disclosure
    cid = d.record_ids[0]
    tampered_receipt = dict(d.bundles[cid].receipt)
    tampered_receipt["capsule_id"] = "0" * 64
    tampered_bundle = replace(d.bundles[cid], receipt=tampered_receipt)
    mutant = replace(d, bundles={**d.bundles, cid: tampered_bundle})
    ok, errors = verify_disclosure(mutant)
    assert not ok


def test_verify_disclosure_catches_tampered_payload(valid_disclosure):
    d = valid_disclosure
    cid = d.record_ids[0]
    tampered_envelopes = copy.deepcopy(d.envelopes)
    tampered_envelopes[cid]["disclosures"]["agent_input"]["n"] = 9999
    mutant = replace(d, envelopes=tampered_envelopes)
    ok, errors = verify_disclosure(mutant)
    assert not ok
    assert any("does not match its committed digest" in e for e in errors)


def test_verify_disclosure_catches_disclosed_field_with_no_committed_digest(valid_disclosure):
    d = valid_disclosure
    cid = d.record_ids[0]
    tampered_envelopes = copy.deepcopy(d.envelopes)
    tampered_envelopes[cid]["disclosures"]["agent_output_extra"] = {"n": 1}
    mutant = replace(d, envelopes=tampered_envelopes)
    ok, errors = verify_disclosure(mutant)
    assert not ok
    assert any("no committed digest exists" in e for e in errors)


def test_verify_disclosure_returns_ok_never_raises_on_malformed_input(valid_disclosure):
    d = valid_disclosure
    mutant = replace(d, disclosure_record={})  # missing every expected key
    ok, errors = verify_disclosure(mutant)
    assert not ok
    assert errors


# ---------------------------------------------------------------------------
# CLI — capsule-emit disclose.
# ---------------------------------------------------------------------------


def test_cli_disclose_stdout_summary(three_record_ledger, capsys):
    from capsule_emit.cli import main

    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    input_file = ledger_path.parent / "input.json"
    output_file = ledger_path.parent / "output.json"
    input_file.write_text(json.dumps(payloads[0][0]))
    output_file.write_text(json.dumps(payloads[0][1]))

    rc = main(
        [
            "disclose",
            str(ledger_path),
            cid,
            "--audience",
            "auditor",
            "--reveal",
            f"{cid}:agent_input={input_file}",
            "--reveal",
            f"{cid}:agent_output={output_file}",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "audience='auditor'" in out
    assert "contiguous, nothing omitted" in out


def test_cli_disclose_json_and_out_file(three_record_ledger, tmp_path, capsys):
    from capsule_emit.cli import main

    ledger_path, caps, payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    input_file = ledger_path.parent / "input.json"
    input_file.write_text(json.dumps(payloads[0][0]))
    out_file = tmp_path / "disclosure.json"

    rc = main(
        [
            "disclose",
            str(ledger_path),
            cid,
            "--audience",
            "auditor",
            "--payloads",
            "selected",
            "--reveal",
            f"{cid}:agent_input={input_file}",
            "--json",
            "--out",
            str(out_file),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["audience"] == "auditor"
    assert json.loads(out_file.read_text()) == parsed


def test_cli_disclose_tampered_reveal_exits_1(three_record_ledger, capsys):
    from capsule_emit.cli import main

    ledger_path, caps, _payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    input_file = ledger_path.parent / "bad_input.json"
    input_file.write_text(json.dumps({"not": "the-real-payload"}))

    rc = main(
        [
            "disclose",
            str(ledger_path),
            cid,
            "--audience",
            "auditor",
            "--payloads",
            "selected",
            "--reveal",
            f"{cid}:agent_input={input_file}",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "does not match" in err


def test_cli_disclose_empty_ledger_exits_1(tmp_path, capsys):
    from capsule_emit.cli import main

    rc = main(["disclose", str(tmp_path / "nope.jsonl"), "f" * 64, "--audience", "auditor"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "empty or not found" in err


def test_cli_disclose_error_exits_1(three_record_ledger, capsys):
    from capsule_emit.cli import main

    ledger_path, caps, _payloads = three_record_ledger
    cid = caps[0]["capsule_id"]
    rc = main(["disclose", str(ledger_path), cid, "--audience", "auditor"])  # payloads=all, no reveal
    err = capsys.readouterr().err
    assert rc == 1
    assert "payloads='all'" in err


def test_ADV_RUN2_verify_disclosure_rejects_forged_top_level_completeness(three_record_ledger):
    """[adv-run-2-fix-batch] A1 regression: verify_disclosure() must authenticate
    Disclosure.completeness/suppressed_fields against the SIGNED disclosure_record,
    not just accept whatever top-level values the caller hands it. Without the fix,
    an attacker who controls the Disclosure object in transit (not the signed ledger
    entry) could flip completeness.records_mode from honest "producer-selected" to a
    false "contiguous" and verify_disclosure() would still report ok=True."""
    ledger_path, caps, payloads = three_record_ledger
    selector = f"{caps[0]['capsule_id']},{caps[2]['capsule_id']}"
    reveal = {
        caps[0]["capsule_id"]: {"agent_input": payloads[0][0], "agent_output": payloads[0][1]},
        caps[2]["capsule_id"]: {"agent_input": payloads[2][0], "agent_output": payloads[2][1]},
    }
    d = disclose(ledger_path, selector, audience="auditor", reveal=reveal)

    # positive control: genuine disclosure is honest and verifies clean
    assert d.completeness["records_mode"] == "producer-selected"
    assert d.disclosure_record["completeness"]["records_mode"] == "producer-selected"
    ok, errors = verify_disclosure(d)
    assert ok, errors

    # ATTACK: forge the top-level completeness only; the signed disclosure_record is untouched.
    forged_completeness = dict(d.completeness)
    forged_completeness["records_mode"] = "contiguous"  # lie: claim nothing was omitted
    forged = replace(d, completeness=forged_completeness)
    assert forged.disclosure_record["completeness"]["records_mode"] == "producer-selected"

    ok2, errors2 = verify_disclosure(forged)
    assert ok2 is False
    assert any("completeness" in e for e in errors2)

    # same attack against suppressed_fields
    forged_suppressed = replace(d, suppressed_fields=("agent_input",))
    assert forged_suppressed.disclosure_record["suppressed_fields"] != ["agent_input"]
    ok3, errors3 = verify_disclosure(forged_suppressed)
    assert ok3 is False
    assert any("suppressed_fields" in e for e in errors3)
