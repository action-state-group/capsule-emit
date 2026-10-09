# SPDX-License-Identifier: Apache-2.0
"""Every checkpoint after a log's first carries a consistency proof.

A witness that already accepted a checkpoint for a ``log_id`` expects each
later one to prove it extends the last accepted checkpoint, and may refuse
one that carries no proof. The COSE builder already refuses ``prev_size >
0`` without a proof; these tests pin the two places this producer used to
fall short:

* a process restart: the first checkpoint after it chained from nothing
  (``prev_size = 0``, no proof) for a log the witness already knew;
* the backlog drain: a checkpoint the witness is already past (its 409
  names a last-accepted size at or beyond it) blocked every later one.
"""
from __future__ import annotations

import http.server
import json
import threading
import time

import pytest
from _stub_receipt import build_stub_receipt_b64, checkpoint_entry_hash

from capsule_emit import ledger, seal, witness
from capsule_emit.checkpoint.cose_wire import verify_checkpoint_cose_offline


class _RecordingTS(http.server.BaseHTTPRequestHandler):
    """Accepts every checkpoint and keeps the decoded COSE claims."""

    received: list = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        result = verify_checkpoint_cose_offline(raw)
        if not result.ok:
            self.send_response(400)
            self.end_headers()
            return
        self.received.append(result.decoded)
        cp = result.decoded.to_checkpoint_record().to_dict()
        entry_hash = checkpoint_entry_hash(cp)
        payload = json.dumps(
            {
                "entry_hash": entry_hash,
                "receipt_b64": build_stub_receipt_b64(entry_hash),
                "leaf_index": 0,
                "tree_size": 1,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def recording_ts():
    received: list = []
    handler = type("_BoundRecordingTS", (_RecordingTS,), {"received": received})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", received
    srv.shutdown()


@pytest.fixture(autouse=True)
def _clean_witness_state():
    def clear():
        witness._counts.clear()
        witness._armed_at.clear()
        witness._states.clear()
        witness._dispatch_locks.clear()
        witness._pending.clear()
        witness._notice_printed = False

    clear()
    yield
    clear()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def _stamps(ledger_path):
    return [
        e for e in ledger.read_ledger_entries(ledger_path)
        if e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
    ]


def _seal_until_stamps(ledger_path, url, n_stamps, start):
    i = start
    while len(_stamps(ledger_path)) < n_stamps:
        seal(None, action=f"action-{i}", operator="acme", anchor=False, ledger=ledger_path, witness_url=url)
        i += 1
        assert _wait_for(lambda: witness._pending == {} or all(
            not t.is_alive() for t in witness._pending.values()
        ))
    return i


def test_first_checkpoint_after_a_restart_chains_and_carries_a_proof(tmp_path, monkeypatch, recording_ts):
    url, received = recording_ts
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"

    next_i = _seal_until_stamps(ledger_path, url, 1, 0)
    assert _wait_for(lambda: len(received) == 1)
    first = received[0]
    assert first.prev_size == 0
    assert first.consistency_proof is None

    # A process restart: every in-memory witness cache is gone, the ledger stays.
    witness._states.clear()
    witness._counts.clear()
    witness._armed_at.clear()
    witness._dispatch_locks.clear()
    witness._pending.clear()

    _seal_until_stamps(ledger_path, url, 2, next_i)
    assert _wait_for(lambda: len(received) == 2)
    second = received[1]
    assert second.log_id == first.log_id
    assert second.prev_size == first.mmr_size
    assert second.prev_root == first.root
    assert second.consistency_proof is not None
    assert second.consistency_proof.size_a == first.mmr_size


def test_a_moved_ledger_does_not_chain_from_another_log_id(tmp_path, monkeypatch):
    """The stamp's log_id is a hash of the ledger path; a ledger file moved
    elsewhere is a different log_id and starts over instead of failing to
    chain."""
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ledger_path = tmp_path / "ledger.jsonl"
    _seal_until_stamps(ledger_path, "http://127.0.0.1:1", 1, 0)
    assert witness._last_persisted_checkpoint(str(ledger_path), "some-other-log-id") is None
    stamped = witness.checkpoint_witness_states(str(ledger_path))[-1].checkpoint
    assert witness._last_persisted_checkpoint(str(ledger_path), stamped.log_id) == stamped


class _ContinuityRefused(Exception):
    """Stands in for ``cll.checkpoint.WitnessContinuityRefused`` (cll >= 0.5):
    the drain reads only its ``last_accepted_mmr_size``."""

    def __init__(self, last_accepted_mmr_size: int, last_accepted_root: str = "cd" * 32):
        super().__init__(f"HTTP 409: last accepted {last_accepted_mmr_size}")
        self.last_accepted_mmr_size = last_accepted_mmr_size
        self.last_accepted_root = last_accepted_root


def _two_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1")
    dead_url = "http://127.0.0.1:1"
    ledger_path = tmp_path / "ledger.jsonl"
    _seal_until_stamps(ledger_path, dead_url, 2, 0)
    pending = witness.checkpoint_witness_backlog(str(ledger_path), [dead_url])[dead_url]
    assert len(pending) == 2
    return dead_url, ledger_path, pending


def test_backlog_skips_a_checkpoint_the_witness_is_past_on_this_ledgers_chain(tmp_path, monkeypatch):
    dead_url, ledger_path, pending = _two_pending(tmp_path, monkeypatch)
    older, newer = pending
    calls = []

    def fake_register(checkpoint_cose, url, **kwargs):
        cp = verify_checkpoint_cose_offline(checkpoint_cose).decoded.to_checkpoint_record()
        calls.append(cp.mmr_size)
        if cp.mmr_size == older.mmr_size:
            raise _ContinuityRefused(last_accepted_mmr_size=older.mmr_size, last_accepted_root=older.root)
        entry_hash = checkpoint_entry_hash(cp.to_dict())
        from capsule_emit.checkpoint import WitnessRecord

        return WitnessRecord(
            ts_url=url, entry_hash=entry_hash,
            receipt_b64=build_stub_receipt_b64(entry_hash), leaf_index=0, tree_size=1,
        )

    import capsule_emit.checkpoint as checkpoint_pkg

    monkeypatch.setattr(checkpoint_pkg, "register_checkpoint", fake_register)
    result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=dead_url)
    assert calls == [older.mmr_size, newer.mmr_size]
    assert result == {dead_url: 1}


def test_backlog_stops_and_warns_when_the_witness_is_behind(tmp_path, monkeypatch):
    dead_url, ledger_path, pending = _two_pending(tmp_path, monkeypatch)
    older, _newer = pending
    calls = []

    def fake_register(checkpoint_cose, url, **kwargs):
        calls.append(1)
        raise _ContinuityRefused(last_accepted_mmr_size=older.mmr_size - 1)

    import capsule_emit.checkpoint as checkpoint_pkg

    monkeypatch.setattr(checkpoint_pkg, "register_checkpoint", fake_register)
    with pytest.warns(RuntimeWarning, match="must start a new log_id"):
        result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=dead_url)
    assert calls == [1]
    assert result == {dead_url: 0}


def test_backlog_keeps_a_witness_past_it_on_a_checkpoint_this_ledger_lacks(tmp_path, monkeypatch):
    """A lost-state or forked ledger: the witness holds a larger checkpoint
    under this log_id that is not one of this ledger's stamps. The drain
    must not skip past it silently; it stops and says to start a new log_id."""
    dead_url, ledger_path, pending = _two_pending(tmp_path, monkeypatch)
    older, newer = pending
    calls = []

    def fake_register(checkpoint_cose, url, **kwargs):
        calls.append(1)
        raise _ContinuityRefused(last_accepted_mmr_size=newer.mmr_size + 100, last_accepted_root="cd" * 32)

    import capsule_emit.checkpoint as checkpoint_pkg

    monkeypatch.setattr(checkpoint_pkg, "register_checkpoint", fake_register)
    with pytest.warns(RuntimeWarning, match="must start a new log_id"):
        result = witness.retry_pending_witness_stamps(str(ledger_path), ts_url=dead_url)
    assert calls == [1]
    assert result == {dead_url: 0}
    assert len(witness.checkpoint_witness_backlog(str(ledger_path), [dead_url])[dead_url]) == 2
