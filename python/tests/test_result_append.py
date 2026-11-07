# SPDX-License-Identifier: Apache-2.0
"""``append_result``: a sealed Evidence Result capsule built by ``capsulectl
result build`` is appended to a ledger store, and ``capsule-emit bundle`` roots
an Evidence Bundle on it (capsule-emit#355). The records are
``test-vectors/capsulectl-result/``, as capsulectl wrote them."""
from __future__ import annotations

import base64
import dataclasses
import json
from pathlib import Path

import pytest
from agent_action_capsule.bundle import verify_bundle
from test_record_proof import _clean_witness_state, _stamp_count, _start_stub_ts, _wait_for  # noqa: F401

from capsule_emit import cli, seal
from capsule_emit.artifact.record import decode_record, encode_record
from capsule_emit.legacy_ledger import LegacyLedger, shared
from capsule_emit.result import ResultRefused, append_result, append_result_file

VECTORS = Path(__file__).resolve().parents[2] / "test-vectors" / "capsulectl-result"


def _record(name: str):
    return decode_record((VECTORS / f"{name}.record.json").read_text(encoding="utf-8"))


def _put(store, name: str) -> str:
    """A cited record, as a ledger holds it (the original is not kept)."""
    record = _record(name)
    shared(store).put_entry(dataclasses.replace(record, artifacts=[]))
    return record.capsule_id


def _ids(store) -> list:
    return [e.value.hex() for e in LegacyLedger(store).scan_entries(0, 100)]


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "capsule-ledger"
    _put(path, "day-1")
    _put(path, "day-2")
    return path


def test_appends_the_result_after_its_citations_and_again_writes_nothing(store):
    result = _record("result")
    first = append_result(result, store)
    assert (first.capsule_id, first.seq, first.appended) == (result.capsule_id, 3, True)
    again = append_result(result, store)
    assert (again.capsule_id, again.seq, again.appended) == (result.capsule_id, 3, False)
    assert _ids(store)[2] == result.capsule_id and len(_ids(store)) == 3
    stored = LegacyLedger(store).get(result.capsule_id)
    assert stored.capsule == result.capsule and stored.producer_envelope == result.producer_envelope


def test_appends_a_capsule_serialized_other_than_the_ledger_line_form(store):
    """capsulectl is Go: same value, other bytes (here indented; in the field raw UTF-8 and \\u003c)."""
    result = _record("result")
    reformatted = json.dumps(json.loads(result.capsule), indent=2, ensure_ascii=False).encode()
    assert reformatted != result.capsule
    assert append_result(dataclasses.replace(result, capsule=reformatted), store).appended
    assert LegacyLedger(store).get(result.capsule_id).capsule == result.capsule


def test_appends_from_the_capsulectl_file(store):
    done = append_result_file(VECTORS / "result.record.json", store)
    assert done.appended and done.seq == 3


def test_refuses_a_result_citing_a_record_the_ledger_lacks(tmp_path):
    store = tmp_path / "capsule-ledger"
    cited = _put(store, "day-1")
    with pytest.raises(ResultRefused, match="does not hold 1 record"):
        append_result(_record("result"), store)
    assert _ids(store) == [cited]


def _tampered_document(change):
    record = _record("result")
    original = record.artifacts[0]
    document = json.loads(original.content)
    change(document)
    content = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    return dataclasses.replace(record, artifacts=[dataclasses.replace(original, content=content, content_sha256="")])


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_tampered_document(lambda d: d["claims"].pop()), "does not verify"),
        (_tampered_document(lambda d: d.update(result_version="evidence-result-v1")), "does not verify"),
        (dataclasses.replace(_record("result"), artifacts=[]), "one retained 'payload' original"),
        (dataclasses.replace(_record("result"), producer_envelope=_record("day-1").producer_envelope), "Producer Envelope"),
        (dataclasses.replace(_record("result"), capsule=_record("day-1").capsule), "capsule id mismatch"),
        (_record("day-1"), "is not an evidence-result-v0 document"),
    ],
)
def test_refuses_what_is_not_a_sealed_result_and_writes_nothing(store, record, message):
    before = _ids(store)
    with pytest.raises(ResultRefused, match=message):
        append_result(record, store)
    assert _ids(store) == before


def test_the_document_must_be_bound_by_agent_input_digest():
    """A record whose document is bound by another digest field (checked directly:
    a validly sealed such record needs a re-sealed capsule)."""
    from capsule_emit.result import _payload

    result = _record("result")
    other = dataclasses.replace(
        result.artifacts[0], binding="model_attestation.compute_attestation.agent_output_digest"
    )
    with pytest.raises(ResultRefused, match="agent_input_digest"):
        _payload(dataclasses.replace(result, artifacts=[other]))
    assert _payload(result)["result_version"] == "evidence-result-v0"


def test_refuses_a_file_that_is_not_a_record(store, tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text('{"capsule_id": 1}')
    with pytest.raises(ResultRefused, match="not an artifact record file"):
        append_result_file(junk, store)
    with pytest.raises(ResultRefused, match="not an artifact record file"):
        append_result_file(tmp_path / "missing.json", store)


def test_the_record_round_trips_through_the_encoder():
    result = _record("result")
    assert decode_record(encode_record(result)) == result
    assert base64.b64decode(json.loads((VECTORS / "result.record.json").read_text())["capsule"])


def _cli(capsys, *argv):
    code = cli.main(["bundle", *map(str, argv)])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_a_bundle_rooted_on_the_appended_result_is_valid(tmp_path, monkeypatch, capsys):
    url, _, stop = _start_stub_ts()
    monkeypatch.setenv("CAPSULE_WITNESS_URL", url)
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1")
    store = tmp_path / "capsule-ledger"
    try:
        _put(store, "day-1")
        _put(store, "day-2")
        result = _record("result")
        assert append_result(result, store).seq == 3
        seal(None, action="after", operator="acme", anchor=False, ledger=store, witness_url=url)
        assert _wait_for(lambda: _stamp_count(store) >= 1)

        out = tmp_path / "bundle.json"
        code, stdout, err = _cli(capsys, store, "--record", result.capsule_id, "--closure-depth", "1", "--interval-from", "1", "-o", out)
        assert code == 0, err
        bundle = json.loads(out.read_bytes())
        assert bundle["root"] == result.capsule_id
        # The interval proof covers the log from position 1, so every record in
        # it is shown; the Result's citations are among them.
        shown = {r["capsule_id"] for r in bundle["records"]}
        assert {result.capsule_id, _record("day-1").capsule_id, _record("day-2").capsule_id} <= shown
        assert verify_bundle(bundle, strict=True).verdict == "VALID", stdout
        assert "VALID under strict verification" in stdout
    finally:
        stop()


def test_cli_appends_and_is_idempotent_and_refuses(store, tmp_path, capsys):
    path = VECTORS / "result.record.json"
    assert cli.main(["ledger", "append-result", str(path), "--ledger", str(store)]) == 0
    out = capsys.readouterr().out
    assert "appended Result" in out and "at position 3" in out
    assert cli.main(["ledger", "append-result", str(path), "--ledger", str(store)]) == 0
    assert "already in the ledger: Result" in capsys.readouterr().out
    empty = tmp_path / "empty-ledger"
    assert cli.main(["ledger", "append-result", str(path), "--ledger", str(empty)]) == 1
    assert "no ledger at" in capsys.readouterr().err
    assert cli.main(["ledger", "append-result", str(tmp_path / "missing.json"), "--ledger", str(store)]) == 1
    assert "not an artifact record file" in capsys.readouterr().err
