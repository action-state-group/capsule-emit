# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for O16 audit item 16 ("Stamp-as-log-entry"):

Checkpoint/witness records must stop living only as an in-memory
``CheckpointRecord.witnesses`` mutation and become their own persisted
ledger entry -- written through ``ledger.append_to_ledger`` -- so that
checkpoint N's stamp is itself a leaf checkpoint N+1's MMR root covers
(frozen surface §2.3: "the stamp does land as its own log entry ... so
checkpoint N's stamp is covered by checkpoint N+1").

Covers:
- (a) the persisted entry's shape, written through the real ``seal()`` +
  witness default-on path against a stub Transparency Service
- (b) ``read_ledger`` (every existing capsule-only consumer: CLI, server,
  permalink, approval, holds, ``ledger.view``/``show``) stays capsule-only
  and unaffected
- (c) ``read_ledger_entries`` sees the raw file, stamps included
- (d) the stamp is actually folded into the MMR as a leaf and covered by the
  *next* checkpoint -- not just written to disk inertly
- (e) the stamp is still persisted even when every witness endpoint fails
  (a self-attested checkpoint is still history worth logging)
- (f) stamp entries never advance ``witness.maybe_checkpoint``'s cadence
  counter (they aren't written through ``core.emit()``)
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
# Hermetic stub Transparency Service -- same shape as
# tests/test_witness_default_on.py's, duplicated here so this file has no
# cross-file fixture dependency.
# ---------------------------------------------------------------------------


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


@pytest.fixture
def dead_ts():
    """A URL nothing is listening on -- every registration attempt fails."""
    return "http://127.0.0.1:1"


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
# (a) the persisted entry's shape
# ---------------------------------------------------------------------------


def test_checkpoint_is_persisted_as_its_own_ledger_entry(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(3):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1), "checkpoint was never registered with the TS"

    def _stamp_written():
        entries = ledger.read_ledger_entries(ledger_path)
        return any(e.get("kind") == ledger.CHECKPOINT_STAMP_KIND for e in entries)

    assert _wait_for(_stamp_written), "checkpoint was never persisted as a ledger entry"

    entries = ledger.read_ledger_entries(ledger_path)
    stamps = [e for e in entries if e.get("kind") == ledger.CHECKPOINT_STAMP_KIND]
    assert len(stamps) == 1, f"expected exactly one stamp entry, got {len(stamps)}"

    stamp = stamps[0]
    assert stamp["v"] == 1
    assert isinstance(stamp["capsule_id"], str) and len(stamp["capsule_id"]) == 64
    int(stamp["capsule_id"], 16)  # must be hex

    from capsule_emit.checkpoint import core as mmr_core

    cp = stamp["checkpoint"]
    assert cp["kind"] == "mmr_checkpoint"
    # mmr_size is total MMR *node* count, not leaf count -- node_count(f) = 2f - popcount(f).
    assert cp["mmr_size"] == mmr_core.node_count(3)
    assert stamp["capsule_id"] == _sha256_of_signing_body(cp)
    assert cp["witnesses"], "checkpoint was registered but no WitnessRecord was persisted"
    assert cp["witnesses"][0]["ts_url"] == ts_url


def _sha256_of_signing_body(cp: dict) -> str:
    body = {
        "v": cp["v"],
        "kind": cp["kind"],
        "log_id": cp["log_id"],
        "mmr_size": cp["mmr_size"],
        "root": cp["root"],
        "prev_size": cp["prev_size"],
        "prev_root": cp["prev_root"],
        "key_id": cp["key_id"],
        "timestamp": cp["timestamp"],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------------------
# (b)/(c) read_ledger stays capsule-only; read_ledger_entries sees everything
# ---------------------------------------------------------------------------


def test_read_ledger_excludes_stamp_entries_every_existing_consumer_is_unaffected(
    tmp_path, stub_ts, monkeypatch
):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    capsules = ledger.read_ledger(ledger_path)
    assert len(capsules) == 2, "read_ledger must return capsules only, unaffected by stamp entries"
    assert all(c.get("kind") != ledger.CHECKPOINT_STAMP_KIND for c in capsules)
    assert all("capsule_id" in c and "action_id" in c for c in capsules)

    raw = ledger.read_ledger_entries(ledger_path)
    assert len(raw) == 3, "read_ledger_entries must see the 2 capsules + 1 stamp entry"


def test_ledger_view_does_not_render_a_garbled_stamp_row(tmp_path, stub_ts, monkeypatch):
    import io

    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    out = io.StringIO()
    ledger.view(ledger_path, out=out)
    rendered = out.getvalue()
    assert "2 record(s)" in rendered, rendered
    assert ledger.CHECKPOINT_STAMP_KIND not in rendered


# ---------------------------------------------------------------------------
# (d) the stamp is actually folded into the MMR and covered by the next
#     checkpoint -- the literal "checkpoint N+1 covers checkpoint N's stamp"
#     acceptance behavior, not just "a file has an extra line."
# ---------------------------------------------------------------------------


def test_stamp_entry_is_a_leaf_covered_by_the_next_checkpoint(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    key = witness._resolve_key(str(ledger_path))
    assert _wait_for(lambda: witness._states.get(key) is not None and witness._states[key].prev is not None)
    first_cp = witness._states[key].prev
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )

    # Ledger now holds: 2 capsules + 1 stamp entry for the first checkpoint.
    assert len(ledger.read_ledger_entries(ledger_path)) == 3

    # Cross cadence again -- 2 more capsules. The due checkpoint's mmr.sync()
    # must fold in everything unindexed: the first checkpoint's stamp entry
    # AND the 2 new capsules -- 3 new leaves total.
    for i in range(2, 4):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 2)
    assert _wait_for(lambda: witness._states[key].prev is not first_cp)
    second_cp = witness._states[key].prev

    from capsule_emit.checkpoint import core as mmr_core

    # mmr_size is total MMR *node* count, not leaf count -- node_count(f) = 2f - popcount(f).
    # First checkpoint: 2 leaves (the 2 capsules). Second: 5 leaves (those 2 +
    # the first checkpoint's stamp entry + the 2 new capsules) -- the +1 over
    # "just 4 capsules" is exactly the stamp entry being folded in as a leaf.
    assert second_cp.prev_size == first_cp.mmr_size == mmr_core.node_count(2)
    assert second_cp.mmr_size == mmr_core.node_count(5), (
        "second checkpoint's mmr_size must cover the first checkpoint's stamp "
        "entry as a leaf (2 capsules + 1 stamp = 3 new leaves over the prior "
        f"2-leaf size), got mmr_size={second_cp.mmr_size}"
    )

    # The stamp entry's own leaf (seq 3, 0-indexed 2) must verify against the
    # second checkpoint's root -- proof, not inference, that it's covered.
    # By now the *second* checkpoint has also persisted its own stamp entry
    # (appended after second_cp.mmr_size was fixed, so it is NOT one of the
    # leaves second_cp itself covers) -- filter to the first checkpoint's
    # stamp specifically, by its digest.
    mmr = witness._states[key].mmr
    stamp_entries = [
        e for e in ledger.read_ledger_entries(ledger_path) if e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
    ]
    assert len(stamp_entries) == 2, "expected the first checkpoint's stamp plus the second's own"
    first_stamp = next(e for e in stamp_entries if e["capsule_id"] == first_cp.digest())
    stamp_digest = bytes.fromhex(first_stamp["capsule_id"])
    proof = mmr.inclusion_proof(3, size=second_cp.mmr_size)
    assert mmr_core.verify_inclusion(
        bytes.fromhex(second_cp.root), second_cp.mmr_size, 2, stamp_digest, proof
    ), "the first checkpoint's stamp entry does not verify as included under the second checkpoint's root"


# ---------------------------------------------------------------------------
# (e) still persisted (self-attested) even if every witness endpoint fails
# ---------------------------------------------------------------------------


def test_stamp_is_persisted_even_when_every_witness_endpoint_fails(tmp_path, dead_ts, monkeypatch, recwarn):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    monkeypatch.setenv("CAPSULE_EMIT_ATEXIT_WITNESS_TIMEOUT", "2.0")
    ledger_path = tmp_path / "ledger.jsonl"

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=dead_ts)

    def _stamp_written():
        return any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )

    assert _wait_for(_stamp_written, timeout=10.0), (
        "a checkpoint whose witness registration failed entirely must still be "
        "persisted as a self-attested log entry"
    )
    stamps = [e for e in ledger.read_ledger_entries(ledger_path) if e.get("kind") == ledger.CHECKPOINT_STAMP_KIND]
    assert stamps[0]["checkpoint"].get("witnesses", []) == []


# ---------------------------------------------------------------------------
# (f) stamp entries never advance the cadence counter -- they aren't written
#     through core.emit(), so witness.maybe_checkpoint never sees them.
# ---------------------------------------------------------------------------


def test_stamp_entries_never_advance_the_cadence_counter(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    key = witness._resolve_key(str(ledger_path))

    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(
        lambda: any(
            e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
            for e in ledger.read_ledger_entries(ledger_path)
        )
    )
    # Counter was reset to 0 at dispatch and only core.emit() increments it --
    # persisting the stamp entry (not routed through emit()) must leave it at 0.
    assert witness._counts.get(key, 0) == 0
