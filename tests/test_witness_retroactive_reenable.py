# SPDX-License-Identifier: Apache-2.0
"""Regression test for O16 audit item 18 ("Retroactive witnessing on
re-enable"): disabling witnessing mid-stream, sealing several records, then
re-enabling must NOT leave a gap. ``MmrLedger.sync()`` (see
``capsule_emit.checkpoint.index``) rescans the entire ledger from scratch on
every checkpoint, and ``witness._get_state`` builds its MMR state lazily on
first-due with no persisted cursor spanning the off period -- so the very
first checkpoint built after re-enabling must cover every record sealed while
witnessing was off, not just the ones sealed after re-enable.

This is a real structural property already, not a code change (see the audit:
"Code change: none required") -- this file is the test lock so a future
refactor of ``sync()`` or ``_get_state`` (e.g. introducing a persisted
"last-synced" cursor) can't silently reintroduce a gap.
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
from capsule_emit.checkpoint.cose_wire import verify_checkpoint_cose_offline

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


def _checkpoint_dict_from_cose(cose_bytes: bytes) -> dict:
    """Decode+verify a COSE-wire checkpoint (real signature check, same as
    what capsule-anchor's witness route will independently do) and
    reconstruct the JSON CheckpointRecord-shaped dict this stub's own
    entry_hash/receipt logic already expects."""
    result = verify_checkpoint_cose_offline(cose_bytes)
    if not result.ok:
        raise ValueError(f"stub TS could not verify COSE checkpoint: {result.errors}")
    return result.decoded.to_checkpoint_record().to_dict()


class _StubWitnessTSHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args):  # silence stdlib access logging in test output
        pass

    def do_POST(self):
        if self.path == "/checkpoints":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = _checkpoint_dict_from_cose(raw)
            except ValueError as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
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
    """Same discipline as ``tests/test_witness_default_on.py``: the witness
    module keeps process-global, per-ledger-path state by design -- reset it
    around every test so no test's counter/lock/MMR state leaks into
    another's."""
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


def test_records_sealed_while_witness_off_are_covered_by_the_next_checkpoint(
    tmp_path, stub_ts, monkeypatch
):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")
    ts_url, received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    # -- off period: witnessing explicitly disabled ---------------------
    off_period = [
        seal(
            None, action=f"off-{i}", operator="acme", anchor=False,
            ledger=ledger, witness_url=ts_url, witness=False,
        )
        for i in range(4)
    ]
    key = witness._resolve_key(str(ledger))
    assert key not in witness._states, (
        "witness=False must not build any MMR state during the off period"
    )

    # -- re-enable: seal enough to cross cadence -------------------------
    on_period = [
        seal(
            None, action=f"on-{i}", operator="acme", anchor=False,
            ledger=ledger, witness_url=ts_url,
        )
        for i in range(3)
    ]

    assert _wait_for(lambda: len(received) >= 1), (
        "checkpoint never fired after re-enabling witnessing"
    )
    assert _wait_for(
        lambda: key in witness._states and witness._states[key].prev is not None
    ), "no CheckpointRecord was ever built after re-enable"

    state = witness._states[key]
    all_records = off_period + on_period

    assert state.mmr.leaf_count() == len(all_records), (
        f"re-enabling witnessing must retroactively cover every record sealed "
        f"while it was off, not just the ones sealed after re-enable -- "
        f"expected {len(all_records)} indexed leaves, got {state.mmr.leaf_count()}"
    )
    for i, record in enumerate(all_records, start=1):
        assert state.mmr.body_digest(i) == bytes.fromhex(record.capsule_id), (
            f"leaf {i} does not correspond to the record sealed at that ledger "
            "position -- an off-period record was skipped or reordered"
        )


def test_disabled_witness_leaves_no_cursor_to_resume_from(tmp_path, stub_ts, monkeypatch):
    """Companion negative check: if a future refactor introduced a persisted
    "last-synced" cursor that only advances while witnessing is on, this
    would catch it directly -- confirm no witness module state of any kind
    (counts, armed-at clock, MMR state) exists for a ledger path that has
    only ever been sealed with witnessing off."""
    ts_url, _received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    for i in range(5):
        seal(
            None, action=f"off-{i}", operator="acme", anchor=False,
            ledger=ledger, witness_url=ts_url, witness=False,
        )

    key = witness._resolve_key(str(ledger))
    assert key not in witness._states
    assert key not in witness._counts
    assert key not in witness._armed_at
