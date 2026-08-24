# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for the [emit-witness-0.5.0-followup] refinements:

- ``witness_url=`` / ``CAPSULE_WITNESS_URL`` accept a single endpoint or
  several (list, or comma-separated string) and fan the checkpoint
  registration out to each one independently.
- one witness endpoint failing never blocks registration with the others.
- the first-use notice prints exactly once per process, to stderr, and
  states what's sent, where, and how to disable.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
import time

import pytest

from capsule_emit import seal, witness
from capsule_emit.checkpoint import Grade

_ = base64, hashlib  # re-exported for parity with the stub TS handler below


class _StubWitnessTSHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path == "/v1/digest":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            body = json.loads(raw)
            self.received.append(body)
            digest = body["capsule_id"]
            entry_hash = hashlib.sha256(bytes.fromhex(digest)).hexdigest()
            resp = {
                "entry_hash": entry_hash,
                "receipt_b64": base64.b64encode(b"stub-receipt").decode(),
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
def two_stub_ts():
    url_a, received_a, stop_a = _start_stub_ts()
    url_b, received_b, stop_b = _start_stub_ts()
    yield (url_a, received_a), (url_b, received_b)
    stop_a()
    stop_b()


@pytest.fixture(autouse=True)
def _clean_witness_state():
    witness._counts.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False
    yield
    witness._counts.clear()
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


# ---------------------------------------------------------------------------
# _parse_witness_urls -- the normalization helper both call sites share.
# ---------------------------------------------------------------------------


def test_parse_witness_urls_accepts_none():
    assert witness._parse_witness_urls(None) == []


def test_parse_witness_urls_accepts_single_string():
    assert witness._parse_witness_urls("https://a.example") == ["https://a.example"]


def test_parse_witness_urls_accepts_comma_separated_string():
    assert witness._parse_witness_urls("https://a.example, https://b.example") == [
        "https://a.example",
        "https://b.example",
    ]


def test_parse_witness_urls_accepts_list():
    assert witness._parse_witness_urls(["https://a.example", "https://b.example"]) == [
        "https://a.example",
        "https://b.example",
    ]


def test_parse_witness_urls_drops_blanks_and_dedupes():
    assert witness._parse_witness_urls("https://a.example,,https://a.example, ") == [
        "https://a.example"
    ]


# ---------------------------------------------------------------------------
# multi-witness fan-out via emit(witness_url=[...])
# ---------------------------------------------------------------------------


def test_witness_url_list_fans_out_to_every_endpoint(tmp_path, two_stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    (url_a, received_a), (url_b, received_b) = two_stub_ts
    ledger = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger,
             witness_url=[url_a, url_b])

    assert _wait_for(lambda: len(received_a) >= 1), "first witness never received the checkpoint"
    assert _wait_for(lambda: len(received_b) >= 1), "second witness never received the checkpoint"

    key = witness._resolve_key(str(ledger))
    state = witness._states[key]
    assert len(state.prev.witnesses) == 2, "checkpoint should carry one WitnessRecord per endpoint"
    urls_recorded = {w.ts_url for w in state.prev.witnesses}
    assert urls_recorded == {url_a, url_b}


# ---------------------------------------------------------------------------
# multi-witness fan-out via CAPSULE_WITNESS_URL comma-separated env var
# ---------------------------------------------------------------------------


def test_capsule_witness_url_env_var_comma_separated_fans_out(tmp_path, two_stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    (url_a, received_a), (url_b, received_b) = two_stub_ts
    monkeypatch.setenv("CAPSULE_WITNESS_URL", f"{url_a},{url_b}")
    ledger = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger)

    assert _wait_for(lambda: len(received_a) >= 1)
    assert _wait_for(lambda: len(received_b) >= 1)


# ---------------------------------------------------------------------------
# one endpoint failing never blocks the others
# ---------------------------------------------------------------------------


def test_one_failing_endpoint_does_not_block_the_others(tmp_path, stub_ts_single, monkeypatch, recwarn):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    good_url, received = stub_ts_single
    dead_url = "http://127.0.0.1:1"  # nothing listens here -- connection refused
    ledger = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger,
             witness_url=[dead_url, good_url])

    assert _wait_for(lambda: len(received) >= 1), (
        "a failing witness endpoint must not prevent registration with a working one"
    )
    registration_warnings = [
        w for w in recwarn.list if "did not complete" in str(w.message)
    ]
    assert any(dead_url in str(w.message) for w in registration_warnings), (
        "the failing endpoint's failure should surface as a warning naming it"
    )


@pytest.fixture
def stub_ts_single():
    base_url, received, stop = _start_stub_ts()
    yield base_url, received
    stop()


# ---------------------------------------------------------------------------
# multi-witness any-of grading: one live stamp is enough (O16 item 11)
# ---------------------------------------------------------------------------


def test_one_valid_stamp_grades_witnessed_even_if_another_endpoint_fails(
    tmp_path, stub_ts_single, monkeypatch
):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    good_url, received = stub_ts_single
    dead_url = "http://127.0.0.1:1"  # nothing listens here -- connection refused
    ledger = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger,
             witness_url=[dead_url, good_url])

    assert _wait_for(lambda: len(received) >= 1)

    key = witness._resolve_key(str(ledger))
    state = witness._states[key]
    assert len(state.prev.witnesses) == 1, "only the live endpoint should have stamped"
    assert state.prev.grade() == Grade.WITNESSED, (
        "any-of semantics: one valid stamp is enough to grade witnessed, "
        "regardless of how many other endpoints failed"
    )


# ---------------------------------------------------------------------------
# first-use notice: prints once, to stderr, with the right content
# ---------------------------------------------------------------------------


def test_first_use_notice_prints_once_with_disable_instructions(tmp_path, stub_ts_single, monkeypatch, capsys):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts_single
    ledger = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness_url=ts_url)
    assert _wait_for(lambda: len(received) >= 1)
    time.sleep(0.1)  # let the notice print land before we capture it

    err = capsys.readouterr().err
    assert err.count("this process just sent its first witness checkpoint") == 1
    assert "32-byte digest" in err
    assert ts_url in err
    assert "witness=False" in err
    assert "CAPSULE_WITNESS=off" in err

    # A second cadence-crossing checkpoint in the same process must not
    # reprint the notice.
    for i in range(2, 4):
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness_url=ts_url)
    assert _wait_for(lambda: len(received) >= 2)
    time.sleep(0.1)

    err_after = capsys.readouterr().err
    assert "this process just sent its first witness checkpoint" not in err_after, (
        "the first-use notice must print at most once per process"
    )


def test_first_use_notice_not_printed_before_any_checkpoint_is_due(tmp_path, stub_ts_single, capsys):
    ts_url, received = stub_ts_single
    ledger = tmp_path / "ledger.jsonl"

    # Real default cadence (100) -- far below the threshold, no checkpoint due.
    seal(None, action="single-shot", operator="acme", anchor=False, ledger=ledger, witness_url=ts_url)

    err = capsys.readouterr().err
    assert "this process just sent its first witness checkpoint" not in err
    assert received == []
