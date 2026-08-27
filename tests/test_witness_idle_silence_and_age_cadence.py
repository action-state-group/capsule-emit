# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for O16 audit item 5 ("Idle silence (+stamp-entry
exclusion)"):

Cadence is "100 entries or 15 minutes, whichever comes first, both
configurable" (frozen surface §0) -- but the age leg must only ever fire when
there is genuinely unwitnessed work. An idle log is silent, never a
heartbeat: there is no background timer, so a ledger with no new ``emit()``
calls must never produce a checkpoint on age alone, even once real wall-clock
time has passed the age cadence.

Covers the O1 acceptance fixture verbatim (referenced by the audit): witness
on, no unwitnessed work, clock advanced past age cadence, only new entry is a
returned stamp -> zero further egress.

Covers:
- (a) the age leg genuinely fires a checkpoint once enough time has passed,
  even with the entry count nowhere near ``cadence_entries``
- (b) an idle log (armed by one entry, then no further ``emit()`` calls)
  produces zero further egress even once real time passes the age cadence
- (c) the O1 fixture: after a checkpoint fires and its stamp is persisted,
  continued idleness (the stamp already sitting in the ledger, clock advanced
  again) still produces zero further egress
- (d) ``CAPSULE_WITNESS_CADENCE_SECONDS`` overrides the default age cadence
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
import time

import pytest

from capsule_emit import ledger, seal, witness

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service -- same shape as the other witness test
# files', duplicated here so this file has no cross-file fixture dependency.
# ---------------------------------------------------------------------------


#: The CLL CheckpointRecord fields a signature covers -- MUST match
#: ``capsule_emit.checkpoint.emit.CheckpointRecord.signing_body()``.
_CHECKPOINT_SIGNING_FIELDS = (
    "v", "kind", "log_id", "mmr_size", "root", "prev_size", "prev_root", "key_id", "timestamp",
)


def _entry_hash_for(cp: dict) -> str:
    """Reproduce capsule-anchor's ``/checkpoints`` entry_hash derivation --
    inlined to keep this file's zero-cross-file-dependency property."""
    body = {k: cp[k] for k in _CHECKPOINT_SIGNING_FIELDS}
    signing_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(signing_body).hexdigest()
    return hashlib.sha256(bytes.fromhex(digest)).hexdigest()


class _StubWitnessTSHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path == "/checkpoints":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            body = json.loads(raw)
            self.received.append(body)
            entry_hash = _entry_hash_for(body)
            resp = {
                "entry_hash": entry_hash,
                "receipt_b64": base64.b64encode(b"stub-receipt-not-a-real-cose-receipt").decode(),
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
def stub_ts():
    base_url, received, stop = _start_stub_ts()
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


# ---------------------------------------------------------------------------
# (a) the age leg fires a checkpoint before the entry-count cadence would
# ---------------------------------------------------------------------------


def test_age_leg_checkpoints_a_stream_before_entry_count_cadence(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1000")  # nowhere near reached
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_SECONDS", "0.2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    seal(None, action="first", operator="acme", anchor=False, ledger=ledger_path, witness_url=ts_url)
    assert received == [], "the first entry alone must not be due yet"

    time.sleep(0.35)  # clock advances past the age cadence
    seal(None, action="second", operator="acme", anchor=False, ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1), (
        "the age leg (CAPSULE_WITNESS_CADENCE_SECONDS) never fired a checkpoint even "
        "though real time passed the age cadence with unwitnessed work pending"
    )


# ---------------------------------------------------------------------------
# (b) an idle log is silent -- no further emit() calls means no checkpoint,
#     no matter how much wall-clock time passes.
# ---------------------------------------------------------------------------


def test_idle_log_never_checkpoints_on_age_alone_with_no_new_emits(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1000")
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_SECONDS", "0.15")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    seal(None, action="only-entry", operator="acme", anchor=False, ledger=ledger_path, witness_url=ts_url)

    time.sleep(0.4)  # well past the age cadence -- but nothing calls emit() again
    assert received == [], (
        "an idle ledger (no new emit() calls) produced a checkpoint on age alone -- "
        "there is no background timer; a checkpoint must only ever be built lazily, "
        "on the back of a real new entry"
    )


# ---------------------------------------------------------------------------
# (c) the O1 fixture, verbatim: witness on, no unwitnessed work, clock
#     advanced past age cadence, only new entry is a returned stamp -> zero
#     further egress.
# ---------------------------------------------------------------------------


def test_idle_after_stamp_persisted_produces_zero_further_egress(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "1")  # due immediately
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_SECONDS", "0.15")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    seal(None, action="only-real-entry", operator="acme", anchor=False,
         ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1), "the initial checkpoint never registered"
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    ), "the checkpoint's stamp was never persisted -- nothing to be idle-silent about"

    egress_after_stamp = len(received)

    # No further seal()/emit() calls -- the only thing that landed in the
    # ledger since is the checkpoint_stamp entry itself. Advance real time
    # well past the age cadence again.
    time.sleep(0.4)

    assert len(received) == egress_after_stamp, (
        "a persisted checkpoint-stamp entry, sitting idle with the age cadence long "
        "since elapsed, still produced further egress -- stamp entries must never "
        "wake the idle/age timer"
    )


# ---------------------------------------------------------------------------
# (d) CAPSULE_WITNESS_CADENCE_SECONDS overrides the default
# ---------------------------------------------------------------------------


def test_default_cadence_seconds_is_15_minutes():
    assert witness.DEFAULT_CADENCE_SECONDS == 900


def test_env_var_overrides_default_age_cadence(monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_SECONDS", "42")
    assert witness._resolved_age_cadence(None) == 42.0


def test_explicit_kwarg_overrides_env_var_age_cadence(monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_SECONDS", "42")
    assert witness._resolved_age_cadence(7.0) == 7.0


def test_invalid_age_cadence_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_SECONDS", "not-a-number")
    assert witness._resolved_age_cadence(None) == witness.DEFAULT_CADENCE_SECONDS
