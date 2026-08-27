# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [O5-minimal] ("witness-outage is launch behavior"):

- **Durable queue** — a checkpoint that fails to register with a witness
  stays queryable/retryable from the ledger alone; it is never silently
  dropped, and nothing about the queue lives only in process memory.
- **Honest lag display** — `status`'s `checkpoints_awaiting_stamp` and new
  `witness_backlog` reflect a late backfill once it lands, and never claim
  a still-unconfirmed checkpoint is witnessed.
- **Per-witness cursors** — each configured witness drains its own backlog
  independently; one witness staying down never blocks another's drain,
  and a witness's drain resumes from its own oldest-pending point.

Covers: outage (registration fails, persists durably) + recovery (retry
drains) + restart-mid-outage (nothing lost, no reliance on in-process
state) + partial multi-witness (one down, one up, independent cursors) +
bounded retry behavior (a still-down witness doesn't get hammered past its
first failure) + status honesty (both directions).
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import time

import pytest
from _stub_receipt import (
    TEST_TS_PUBLIC_KEY_PEM,
    build_stub_receipt_b64,
    checkpoint_dict_from_cose,
    checkpoint_entry_hash,
)

from capsule_emit import ledger, seal, status, witness
from capsule_emit.checkpoint import emit as checkpoint_emit_mod

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service -- same shape as the other witness test
# files (no cross-file fixture dependency, per this repo's existing convention).
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


def _start_stub_ts(port: int = 0):
    received: list[dict] = []
    handler_cls = type(
        "_BoundStubWitnessTSHandler", (_StubWitnessTSHandler,), {"received": received}
    )
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    actual_port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{actual_port}", received, srv.shutdown


def _reserve_port() -> int:
    """Reserve a free port, then release it -- lets a test point a witness
    URL at an address nothing is listening on yet (fast connection-refused
    failures, like the ``dead_ts`` fixture other witness test files use),
    then later bind a REAL stub server to that exact same port/URL to
    simulate the witness coming back -- without ever changing the
    configured URL mid-test."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def stub_ts():
    base_url, received, stop = _start_stub_ts()
    yield base_url, received
    stop()


@pytest.fixture
def dead_ts():
    """A URL nothing is listening on -- every registration attempt fails."""
    return "http://127.0.0.1:1"


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


def _simulate_process_restart():
    """Drop every in-process witness cache -- ``_states`` (the MMR/signer
    cache), ``_counts``/``_armed_at`` (the cadence counters), and the
    dispatch locks -- WITHOUT touching the ledger file on disk. A real
    process restart loses exactly this: everything in
    ``capsule_emit.witness``'s module-level dicts, nothing on disk."""
    witness._states.clear()
    witness._counts.clear()
    witness._armed_at.clear()
    witness._dispatch_locks.clear()
    witness._pending.clear()


# ---------------------------------------------------------------------------
# Durable queue: a failed registration is retryable from the ledger alone,
# and survives a simulated restart because nothing about it lives only in
# process memory.
# ---------------------------------------------------------------------------


def test_failed_registration_leaves_a_durable_backlog_entry(tmp_path, dead_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=dead_ts)

    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    backlog = witness.checkpoint_witness_backlog(str(ledger_path), [dead_ts])
    assert len(backlog[dead_ts]) == 1, "the self-attested checkpoint must appear in its backlog"

    states = witness.checkpoint_witness_states(str(ledger_path))
    assert len(states) == 1
    assert states[0].effective_witnesses == {}
    assert states[0].grade().value == "self-attested"


def test_restart_mid_outage_loses_nothing(tmp_path, dead_ts, monkeypatch):
    """The core acceptance scenario: kill the witness, seal past cadence
    (checkpoint queues durably), simulate a process restart (drop every
    in-memory witness cache), bring the witness back, and confirm the
    pending checkpoint is STILL there and drains correctly -- proving the
    queue never depended on anything the restart destroyed."""
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    port = _reserve_port()
    dead_url = f"http://127.0.0.1:{port}"
    ledger_path = tmp_path / "ledger.jsonl"
    # Simulate that this hermetic stub IS the pinned default witness
    # ([verify-batch-fastfollow] item D) so the no-key grade() read path
    # signature-verifies the backfilled stamp instead of merely structurally
    # validating it.
    monkeypatch.setattr(checkpoint_emit_mod, "DEFAULT_TS_URL", dead_url)
    monkeypatch.setattr(checkpoint_emit_mod, "DEFAULT_TS_PUBLIC_KEY_PEM", TEST_TS_PUBLIC_KEY_PEM)

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=dead_url)

    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )
    assert len(witness.checkpoint_witness_backlog(str(ledger_path), [dead_url])[dead_url]) == 1

    _simulate_process_restart()

    live_url, received, stop = _start_stub_ts(port=port)
    assert live_url == dead_url, "the witness must come back on the SAME configured URL"
    try:
        # Nothing in-process remembers the outage -- the backlog re-derives
        # correctly from the ledger alone, straight after the "restart".
        backlog = witness.checkpoint_witness_backlog(str(ledger_path), [dead_url])
        assert len(backlog[dead_url]) == 1, "the pending checkpoint must survive the restart"

        result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=dead_url)
        assert result == {dead_url: 1}
        assert len(received) == 1, "the retry must have actually registered with the now-live TS"

        backfills = [
            e for e in ledger.read_ledger_entries(ledger_path)
            if e.get("kind") == ledger.WITNESS_BACKFILL_KIND
        ]
        assert len(backfills) == 1
        assert backfills[0]["witness"]["ts_url"] == dead_url

        states = witness.checkpoint_witness_states(str(ledger_path))
        assert states[0].grade().value == "witnessed"
        assert dead_url in states[0].effective_witnesses

        # Drained -- nothing left pending for this witness.
        assert witness.checkpoint_witness_backlog(str(ledger_path), [dead_url])[dead_url] == []
    finally:
        stop()


def test_retry_is_a_noop_on_an_empty_backlog(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    received_count = len(received)

    result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=ts_url)
    assert result == {ts_url: 0}
    assert len(received) == received_count, "retry must not re-register an already-witnessed checkpoint"


def test_retry_respects_the_kill_switch(tmp_path, dead_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=dead_ts)

    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=dead_ts)
    assert result == {}, "the kill switch must gate retries exactly like the original registration"


# ---------------------------------------------------------------------------
# Per-witness cursors: independent drain, oldest-pending-first, bounded to
# one attempt per still-down witness per call.
# ---------------------------------------------------------------------------


def test_partial_multi_witness_one_down_does_not_block_the_other(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    live_url, received = stub_ts
    dead_url = "http://127.0.0.1:1"
    ledger_path = tmp_path / "ledger.jsonl"
    # Simulate that this hermetic stub IS the pinned default witness so the
    # no-key grade() read path signature-verifies the live stamp.
    monkeypatch.setattr(checkpoint_emit_mod, "DEFAULT_TS_URL", live_url)
    monkeypatch.setattr(checkpoint_emit_mod, "DEFAULT_TS_PUBLIC_KEY_PEM", TEST_TS_PUBLIC_KEY_PEM)

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=[live_url, dead_url])

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    states = witness.checkpoint_witness_states(str(ledger_path))
    assert len(states) == 1
    # The live witness already advanced -- the checkpoint is witnessed --
    # even though the dead one never confirmed. Grade is any-of, not all-of.
    assert states[0].grade().value == "witnessed"
    assert live_url in states[0].effective_witnesses
    assert dead_url not in states[0].effective_witnesses

    backlog = witness.checkpoint_witness_backlog(str(ledger_path), [live_url, dead_url])
    assert backlog[live_url] == [], "the live witness has nothing pending -- it already confirmed"
    assert len(backlog[dead_url]) == 1, "the dead witness independently still has this checkpoint pending"


def test_dead_witness_cursor_resumes_independently_once_it_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    live_url_a, received_a, stop_a = _start_stub_ts()
    port_b = _reserve_port()
    dead_url_b = f"http://127.0.0.1:{port_b}"
    ledger_path = tmp_path / "ledger.jsonl"

    try:
        for i in range(2):
            seal(None, action=f"action-{i}", operator="acme", anchor=False,
                 ledger=ledger_path, witness_url=[live_url_a, dead_url_b])

        assert _wait_for(lambda: len(received_a) >= 1)
        assert _wait_for(
            lambda: any(
                e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
                for e in ledger.read_ledger_entries(ledger_path)
            )
        )

        # b is still down -- draining it must not touch a's already-confirmed
        # state, and must not re-contact a either.
        result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=[live_url_a, dead_url_b])
        assert result == {live_url_a: 0, dead_url_b: 0}
        assert len(received_a) == 1, "a must not be re-registered while draining b's independent backlog"

        # Now b comes back, on the SAME url/port.
        live_url_b, received_b, stop_b = _start_stub_ts(port=port_b)
        assert live_url_b == dead_url_b
        try:
            result = witness.retry_pending_witness_stamps(
                str(ledger_path), ts_url=[live_url_a, dead_url_b]
            )
            assert result == {live_url_a: 0, dead_url_b: 1}
            assert len(received_a) == 1, "a's cursor must stay put -- it had nothing pending"
            assert len(received_b) == 1, "b's cursor must have drained its own backlog"

            states = witness.checkpoint_witness_states(str(ledger_path))
            assert set(states[0].effective_witnesses) == {live_url_a, dead_url_b}
        finally:
            stop_b()
    finally:
        stop_a()


def test_retry_stops_at_first_failure_per_witness_still_down(tmp_path, monkeypatch):
    """Two checkpoints pile up while a witness is down. A retry pass must
    try the oldest, fail (still down), and stop -- not hammer the second
    one with a doomed attempt in the same pass. Verified by call count via
    a monkeypatched register_checkpoint, since a real dead socket makes
    both attempts fail identically and wouldn't distinguish "stopped early"
    from "tried both, both failed"."""
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1")
    dead_url = "http://127.0.0.1:1"
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=dead_url)
        assert _wait_for(
            lambda i=i: len(
                [e for e in ledger.read_ledger_entries(ledger_path) if e.get("kind") == ledger.CHECKPOINT_STAMP_KIND]
            ) == i + 1
        )
        # Each seal() dispatches its own async worker against the SAME
        # ledger path -- serialize so the second checkpoint's prev really is
        # the first (matches the sequential-cadence pattern other tests in
        # this repo use for cadence_entries=1).

    backlog_before = witness.checkpoint_witness_backlog(str(ledger_path), [dead_url])
    assert len(backlog_before[dead_url]) == 2, "expected both checkpoints pending"

    calls = []

    def _counting_register_checkpoint(checkpoint_cose, url, **kwargs):
        # retry_pending_witness_stamps() registers via each pending
        # checkpoint's persisted COSE-wire form (raw bytes), not a
        # CheckpointRecord -- decode it back to learn which checkpoint
        # (by mmr_size) this attempt was for.
        result = checkpoint_dict_from_cose(checkpoint_cose)
        calls.append(result["mmr_size"])
        raise ConnectionError("still down")

    import capsule_emit.checkpoint as checkpoint_pkg

    monkeypatch.setattr(checkpoint_pkg, "register_checkpoint", _counting_register_checkpoint)

    result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=dead_url)
    assert result == {dead_url: 0}
    assert len(calls) == 1, f"expected exactly one attempt (the oldest pending), got {len(calls)}"
    assert calls[0] == backlog_before[dead_url][0].mmr_size, "must try the OLDEST pending checkpoint first"


# ---------------------------------------------------------------------------
# Honest lag display: status reflects a backfill once it lands, in both
# directions -- never stuck "awaiting stamp" after a real stamp arrives,
# never "witnessed" before one genuinely does.
# ---------------------------------------------------------------------------


def test_status_shows_backlog_during_outage_and_clears_it_after_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    port = _reserve_port()
    url = f"http://127.0.0.1:{port}"
    ledger_path = tmp_path / "ledger.jsonl"
    # Simulate that this hermetic stub IS the pinned default witness so the
    # no-key grade() read path signature-verifies the backfilled stamp.
    monkeypatch.setattr(checkpoint_emit_mod, "DEFAULT_TS_URL", url)
    monkeypatch.setattr(checkpoint_emit_mod, "DEFAULT_TS_PUBLIC_KEY_PEM", TEST_TS_PUBLIC_KEY_PEM)

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=url)

    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    during = status.compute_status(str(ledger_path), offline=True, ts_url=url)
    assert during["checkpoints_awaiting_stamp"] == 1
    assert during["witness_backlog"] == {url: 1}
    assert during["latest_checkpoint"]["grade"] == "self-attested"

    live_url, received, stop = _start_stub_ts(port=port)
    try:
        result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=url)
        assert result == {url: 1}

        after = status.compute_status(str(ledger_path), offline=True, ts_url=url)
        assert after["checkpoints_awaiting_stamp"] == 0, (
            "status must not keep claiming a backfilled checkpoint is still awaiting a stamp"
        )
        assert after["witness_backlog"] == {url: 0}
        assert after["latest_checkpoint"]["grade"] == "witnessed"
    finally:
        stop()


def test_status_never_reports_witnessed_while_genuinely_still_down(tmp_path, dead_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=dead_ts)

    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    # A retry attempt against a witness still down must not flip anything.
    witness.retry_pending_witness_stamps(str(ledger_path), ts_url=dead_ts)

    result = status.compute_status(str(ledger_path), offline=True, ts_url=dead_ts)
    assert result["checkpoints_awaiting_stamp"] == 1
    assert result["witness_backlog"] == {dead_ts: 1}
    assert result["latest_checkpoint"]["grade"] == "self-attested"


def test_status_json_and_cli_surface_the_backlog(tmp_path, dead_ts, monkeypatch, capsys):
    from capsule_emit.cli import main as cli_main

    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=dead_ts)

    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    rc = cli_main(["status", str(ledger_path), "--offline", "--witness-url", dead_ts, "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["witness_backlog"] == {dead_ts: 1}

    rc = cli_main(["status", str(ledger_path), "--offline", "--witness-url", dead_ts])
    assert rc == 0
    rendered = capsys.readouterr().out
    assert "witness backlog" in rendered
    assert dead_ts in rendered


# ---------------------------------------------------------------------------
# Automatic drain: the next real emit() crossing cadence, after a witness
# returns, drains the backlog on its own -- no explicit retry call needed.
# ---------------------------------------------------------------------------


def test_next_emit_after_witness_returns_drains_the_backlog_automatically(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    port = _reserve_port()
    url = f"http://127.0.0.1:{port}"
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=url)

    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )
    assert len(witness.checkpoint_witness_backlog(str(ledger_path), [url])[url]) == 1

    live_url, received, stop = _start_stub_ts(port=port)
    try:
        assert live_url == url
        for i in range(2, 4):
            seal(None, action=f"action-{i}", operator="acme", anchor=False,
                 ledger=ledger_path, witness_url=url)

        # The second checkpoint (from this new batch) registers, AND the
        # backlog from the first, now-recovered witness drains -- 2 POSTs.
        assert _wait_for(lambda: len(received) >= 2)

        def _backfilled():
            return any(
                e.get("kind") == ledger.WITNESS_BACKFILL_KIND
                for e in ledger.read_ledger_entries(ledger_path)
            )

        assert _wait_for(_backfilled), "the first checkpoint's backlog must drain on the next real emit()"
        assert witness.checkpoint_witness_backlog(str(ledger_path), [url])[url] == []
    finally:
        stop()
