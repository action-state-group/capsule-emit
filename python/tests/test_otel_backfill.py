# SPDX-License-Identifier: Apache-2.0
"""Tests for ``seal_otlp_file``: backfilling capsules from an OTLP/JSON file."""
from __future__ import annotations

import hashlib
import json

import pytest

from capsule_emit import ledger_layout, read_ledger
from capsule_emit.otel import seal_otlp_file
from capsule_emit.otel.backfill import SPAN_TIME_KEY
from capsule_emit.otel.block import OTEL_BLOCK_KEY
from capsule_emit.verification import verify_capsule

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
START_NS = 1_546_300_800_123_456_789  # 2019-01-01T00:00:00.123456789Z
END_NS = START_NS + 2_000_000_000


def _span(span_id: str, name: str, **attrs) -> dict:
    return {
        "traceId": TRACE,
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(START_NS),
        "endTimeUnixNano": str(END_NS),
        "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()],
        "status": {"code": 1},
    }


def _export(*spans: dict) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "orders"}}]},
                "scopeSpans": [{"spans": list(spans)}],
            }
        ]
    }


def _write(tmp_path, *spans, name="trace.json"):
    path = tmp_path / name
    path.write_text(json.dumps(_export(*spans)))
    return path


def _effect():
    return _span("00f067aa0ba902b7", "write_order", **{"http.request.method": "POST"})


def _read():
    return _span("00f067aa0ba902b8", "read_order", **{"http.request.method": "GET"})


def _seal(path, ledger):
    return seal_otlp_file(path, operator="acme", developer="agent@v1", ledger=str(ledger))


def test_effect_span_is_sealed_as_backfilled_and_verifies(tmp_path):
    path = _write(tmp_path, _effect())
    ledger = tmp_path / "l.jsonl"
    report = _seal(path, ledger)
    assert len(report.sealed) == 1
    (capsule,) = read_ledger(ledger)
    mode = capsule["provenance_mode"]
    assert mode["mode"] == "backfilled"
    assert mode["time_rung"] == "self_attested"
    assert mode["source_asserted_at"] == "2019-01-01T00:00:00.123456Z"
    assert mode["import_batch"] == hashlib.sha256(path.read_bytes()).hexdigest() == report.import_batch
    assert mode["source_ref"] == {
        "type": "otel-span",
        "digest_alg": "SHA-256",
        "digest": hashlib.sha256(bytes.fromhex(TRACE + "00f067aa0ba902b7")).hexdigest(),
    }
    # the seal time is real and later than the span, never the span time
    assert mode["imported_at"] > mode["source_asserted_at"]
    assert mode["imported_at"][:4] >= "2026"
    compute = capsule["model_attestation"]["compute_attestation"]
    assert compute[SPAN_TIME_KEY] == {
        "start": "2019-01-01T00:00:00.123456Z",
        "end": "2019-01-01T00:00:02.123456Z",
        "asserted_by": "source",
    }
    assert OTEL_BLOCK_KEY in compute and SPAN_TIME_KEY not in compute[OTEL_BLOCK_KEY]
    assert report.sealed[0] == capsule["capsule_id"]


def test_verifier_reports_self_attested_and_no_capture_time_claim(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _seal(_write(tmp_path, _effect()), ledger)
    (capsule,) = read_ledger(ledger)
    result = verify_capsule(capsule)
    assert result.ok
    assert result.assurance["provenance_mode"] == "backfilled"
    assert result.assurance["provenance_time_rung"] == "self_attested"
    assert "corroborates_source_time" not in json.dumps(capsule)
    assert capsule["provenance_mode"]["time_rung"] != "witnessed"


def test_rerun_adds_nothing(tmp_path):
    path = _write(tmp_path, _effect())
    ledger = tmp_path / "l.jsonl"
    _seal(path, ledger)
    before = ledger.read_bytes()
    report = _seal(path, ledger)
    assert report.sealed == ()
    assert report.skipped_duplicate == 1
    assert ledger.read_bytes() == before


def test_same_span_twice_in_one_file_is_sealed_once(tmp_path):
    ledger = tmp_path / "l.jsonl"
    report = _seal(_write(tmp_path, _effect(), _effect()), ledger)
    assert len(report.sealed) == 1 and report.skipped_duplicate == 1


def test_observation_span_is_not_sealed(tmp_path):
    ledger = tmp_path / "l.jsonl"
    report = _seal(_write(tmp_path, _read(), _effect()), ledger)
    assert len(report.sealed) == 1
    assert report.skipped_observation == 1
    assert len(read_ledger(ledger)) == 1


def test_jsonl_export_and_span_without_ids(tmp_path):
    path = tmp_path / "t.jsonl"
    bad = dict(_effect(), spanId="nope")
    path.write_text(json.dumps(_export(_effect())) + "\n" + json.dumps(_export(bad)) + "\n")
    with pytest.warns(RuntimeWarning, match="without valid"):
        report = _seal(path, tmp_path / "l.jsonl")
    assert len(report.sealed) == 1 and report.skipped_invalid == 1


def test_not_otlp_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"hello": 1}')
    with pytest.raises(ValueError, match="resourceSpans"):
        _seal(path, tmp_path / "l.jsonl")


def test_live_exporter_records_carry_no_provenance_mode(tmp_path):
    from capsule_emit.otel import SpanFacts, process_span_facts

    ledger = tmp_path / "l.jsonl"
    facts = SpanFacts(
        name="write_order",
        attributes={"http.request.method": "POST"},
        resource_attributes=None,
        trace_id=TRACE,
        span_id="00f067aa0ba902b7",
        parent_span_id=None,
        trace_flags=None,
        tracestate=None,
        status_is_error=False,
    )
    process_span_facts(facts, operator="acme", developer="agent@v1", ledger=str(ledger))
    (capsule,) = read_ledger(ledger)
    assert "provenance_mode" not in capsule
    assert SPAN_TIME_KEY not in capsule["model_attestation"]["compute_attestation"]


def test_store_directory_ledger_seals_and_dedups(tmp_path):
    store = tmp_path / "store"
    path = _write(tmp_path, _effect())
    assert len(_seal(path, store).sealed) == 1
    again = _seal(path, store)
    assert again.sealed == () and again.skipped_duplicate == 1
    assert len(read_ledger(store)) == 1


def test_flat_ledger_refused_once_flat_writes_stop(tmp_path, monkeypatch):
    flat = tmp_path / "old.jsonl"
    _seal(_write(tmp_path, _effect()), flat)
    monkeypatch.setattr(ledger_layout, "_allow_new_flat", False)
    monkeypatch.setattr(ledger_layout, "flat_writes_stopped", lambda: True)
    other = _write(tmp_path, dict(_effect(), spanId="00f067aa0ba902c1"), name="t2.json")
    with pytest.raises(ledger_layout.FlatLedgerWriteRefused):
        _seal(other, flat)
    assert len(read_ledger(flat)) == 1


def test_seal_failure_is_reported_as_failed_not_observation(tmp_path, monkeypatch):
    from capsule_emit import core

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(core, "_append_capsule", boom)
    with pytest.warns(RuntimeWarning, match="seal failed"):
        report = _seal(_write(tmp_path, _effect()), tmp_path / "l.jsonl")
    assert report.failed == 1 and report.skipped_observation == 0 and report.sealed == ()


def test_uppercase_ids_are_accepted_and_dedup_across_case(tmp_path):
    ledger = tmp_path / "l.jsonl"
    upper = dict(_effect(), traceId=TRACE.upper(), spanId="00F067AA0BA902B7")
    assert len(_seal(_write(tmp_path, upper), ledger).sealed) == 1
    report = _seal(_write(tmp_path, _effect(), name="t2.json"), ledger)
    assert report.skipped_duplicate == 1 and report.sealed == ()


def test_all_zero_parent_is_a_root_span(tmp_path):
    ledger = tmp_path / "l.jsonl"
    report = _seal(_write(tmp_path, dict(_effect(), parentSpanId="0" * 16)), ledger)
    assert len(report.sealed) == 1


@pytest.mark.parametrize("start", ["0", "-5", "1000000000000000000000000000000", "abc", None])
def test_unusable_start_time_is_skipped_and_later_spans_still_seal(tmp_path, start):
    bad = dict(_effect(), spanId="00f067aa0ba902c2")
    if start is None:
        del bad["startTimeUnixNano"]
    else:
        bad["startTimeUnixNano"] = start
    with pytest.warns(RuntimeWarning):
        report = _seal(_write(tmp_path, bad, _effect()), tmp_path / "l.jsonl")
    assert report.skipped_invalid == 1 and len(report.sealed) == 1


def test_end_before_start_is_left_out_of_span_time(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _seal(_write(tmp_path, dict(_effect(), endTimeUnixNano="5")), ledger)
    (capsule,) = read_ledger(ledger)
    assert "end" not in capsule["model_attestation"]["compute_attestation"][SPAN_TIME_KEY]


def test_malformed_later_document_writes_nothing(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(_export(_effect())) + "\n" + '{"nope": 1}\n')
    ledger = tmp_path / "l.jsonl"
    with pytest.raises(ValueError, match="resourceSpans"):
        _seal(path, ledger)
    assert not ledger.exists() or read_ledger(ledger) == []


def test_odd_span_entries_do_not_abort(tmp_path):
    path = tmp_path / "t.json"
    doc = _export(_effect())
    doc["resourceSpans"][0]["scopeSpans"][0]["spans"].insert(0, None)
    path.write_text(json.dumps(doc))
    report = _seal(path, tmp_path / "l.jsonl")
    assert len(report.sealed) == 1 and report.skipped_invalid == 1


def test_unsupported_attribute_types_are_left_out_not_sealed_empty(tmp_path):
    from capsule_emit.otel.backfill import _attrs

    attrs = _attrs(
        [
            {"key": "a", "value": {"bytesValue": "AAAA"}},
            {"key": "b", "value": {"intValue": "x"}},
            {"key": "c", "value": {"intValue": "7"}},
            {"key": "d", "value": {"arrayValue": {"values": [{"stringValue": "s"}, {"kvlistValue": {}}]}}},
        ]
    )
    assert attrs == {"c": 7}
