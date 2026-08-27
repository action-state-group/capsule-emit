# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for O16 audit item 17 ("status's fetch-fold"):

Net-new ``status`` verb: ladder position (self-attested/witnessed grade,
item 11), checkpoint/stamp lag (records awaiting checkpoint, checkpoints
awaiting a witness stamp), and a read-only witness re-check of the latest
checkpoint's receipt(s) unless ``--offline``.

Covers:
- (a) no ledger / empty ledger reports honestly, doesn't crash
- (b) capsules sealed below cadence: all of them are "awaiting checkpoint",
  zero checkpoints exist
- (c) a self-attested checkpoint (every witness endpoint dead) grades
  self-attested and counts as "awaiting stamp"
- (d) a witnessed checkpoint grades witnessed, is not "awaiting stamp", and
  its receipt is (unless --offline) independently re-checked over the
  network -- a read-only GET, never a re-registration
- (e) ``--offline`` skips the network re-check entirely
- (f) records sealed after the latest checkpoint (including the
  checkpoint's own stamp entry) count as "awaiting checkpoint"
- (g) CLI wiring: ``capsule-emit status`` renders text; ``--json`` renders
  the same data as parseable JSON
- (h) mutation-proof: self-attested vs witnessed fixtures produce different
  rendered output -- a status that ignored ``grade()`` and always reported
  one value would fail this
"""
from __future__ import annotations

import http.server
import io
import json
import threading
import time

import pytest
from _stub_receipt import TEST_TS_PUBLIC_KEY_PEM, build_stub_receipt_b64, checkpoint_entry_hash

from capsule_emit import cli, ledger, seal, status, witness
from capsule_emit.checkpoint import emit as checkpoint_emit_mod

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service -- same shape as
# tests/test_witness_stamp_persistence.py's, duplicated here so this file has
# no cross-file fixture dependency.
# ---------------------------------------------------------------------------


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

    def do_GET(self):
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


def _has_stamp(ledger_path):
    return any(
        e.get("kind") == ledger.CHECKPOINT_STAMP_KIND
        for e in ledger.read_ledger_entries(ledger_path)
    )


# ---------------------------------------------------------------------------
# (a) no ledger / empty ledger
# ---------------------------------------------------------------------------


def test_status_on_missing_ledger_reports_honestly(tmp_path):
    result = status.compute_status(str(tmp_path / "nope.jsonl"), offline=True)
    assert result["capsule_count"] == 0
    assert result["checkpoint_count"] == 0
    assert result["records_awaiting_checkpoint"] == 0
    assert result["checkpoints_awaiting_stamp"] == 0
    assert result["latest_checkpoint"] is None


# ---------------------------------------------------------------------------
# (b) below cadence: no checkpoint yet
# ---------------------------------------------------------------------------


def test_status_below_cadence_all_records_awaiting_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "100")
    ledger_path = tmp_path / "ledger.jsonl"
    for i in range(3):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness=False)

    result = status.compute_status(str(ledger_path), offline=True)
    assert result["capsule_count"] == 3
    assert result["checkpoint_count"] == 0
    assert result["records_awaiting_checkpoint"] == 3
    assert result["checkpoints_awaiting_stamp"] == 0
    assert result["latest_checkpoint"] is None


# ---------------------------------------------------------------------------
# (c) self-attested checkpoint (dead witness)
# ---------------------------------------------------------------------------


def test_status_self_attested_checkpoint_is_awaiting_stamp(tmp_path, dead_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    monkeypatch.setenv("CAPSULE_EMIT_ATEXIT_WITNESS_TIMEOUT", "2.0")
    ledger_path = tmp_path / "ledger.jsonl"
    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=dead_ts)

    assert _wait_for(lambda: _has_stamp(ledger_path), timeout=10.0)

    result = status.compute_status(str(ledger_path), offline=True)
    assert result["checkpoint_count"] == 1
    assert result["checkpoints_awaiting_stamp"] == 1
    cp = result["latest_checkpoint"]
    assert cp["grade"] == "self-attested"
    assert cp["witnesses"] == []
    # The checkpoint's own stamp entry is itself uncovered until the *next*
    # checkpoint -- one record (the stamp) is genuinely awaiting.
    assert result["records_awaiting_checkpoint"] == 0  # the stamp isn't a "capsule" record


# ---------------------------------------------------------------------------
# (d) witnessed checkpoint: grade flips, not awaiting stamp, receipt re-checked
# ---------------------------------------------------------------------------


def test_status_witnessed_checkpoint_grades_witnessed_and_rechecks_online(
    tmp_path, stub_ts, monkeypatch
):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(lambda: _has_stamp(ledger_path))

    result = status.compute_status(str(ledger_path), offline=False)
    cp = result["latest_checkpoint"]
    assert cp["grade"] == "witnessed"
    assert result["checkpoints_awaiting_stamp"] == 0
    assert len(cp["witnesses"]) == 1
    assert cp["witnesses"][0]["ts_url"] == ts_url
    # The stub's receipt is not a real COSE receipt, so the independent
    # re-check must come back negative rather than crash -- proving the
    # network call genuinely ran and was actually checked, not rubber-stamped.
    assert cp["witnesses"][0]["confirmed"] is False


# ---------------------------------------------------------------------------
# (e) --offline skips the network re-check entirely
# ---------------------------------------------------------------------------


def test_status_offline_skips_network_recheck(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(lambda: _has_stamp(ledger_path))

    called = []

    def _boom(*a, **k):
        called.append(True)
        raise AssertionError("verify_receipt_offline must not be called with offline=True")

    monkeypatch.setattr("capsule_emit.checkpoint.verify_receipt_offline", _boom)

    result = status.compute_status(str(ledger_path), offline=True)
    assert not called
    cp = result["latest_checkpoint"]
    assert cp["witnesses"][0]["confirmed"] is None

    out = io.StringIO()
    status.render_status(result, out=out)
    rendered = out.getvalue()
    assert "unconfirmed (--offline)" in rendered


# ---------------------------------------------------------------------------
# (f) records sealed after the latest checkpoint count as awaiting
# ---------------------------------------------------------------------------


def test_status_counts_records_sealed_after_the_checkpoint(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"
    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=ledger_path, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(lambda: _has_stamp(ledger_path))

    # One more capsule, below cadence again -- must not trigger a second
    # checkpoint, so it's genuinely awaiting.
    seal(None, action="action-2", operator="acme", anchor=False,
         ledger=ledger_path, witness=False)

    result = status.compute_status(str(ledger_path), offline=True)
    assert result["checkpoint_count"] == 1
    assert result["capsule_count"] == 3
    assert result["records_awaiting_checkpoint"] == 1


# ---------------------------------------------------------------------------
# (g) CLI wiring
# ---------------------------------------------------------------------------


def test_cli_status_renders_text(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "100")
    ledger_path = tmp_path / "ledger.jsonl"
    seal(None, action="action-0", operator="acme", anchor=False,
         ledger=ledger_path, witness=False)

    rc = cli.main(["status", str(ledger_path), "--offline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "capsules sealed" in out
    assert "1" in out
    assert "none yet" in out  # no checkpoint yet


def test_cli_status_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "100")
    ledger_path = tmp_path / "ledger.jsonl"
    seal(None, action="action-0", operator="acme", anchor=False,
         ledger=ledger_path, witness=False)

    rc = cli.main(["status", str(ledger_path), "--offline", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["capsule_count"] == 1
    assert parsed["checkpoint_count"] == 0


def test_cli_status_missing_ledger_reports_empty_not_found(tmp_path, capsys):
    rc = cli.main(["status", str(tmp_path / "nope.jsonl"), "--offline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "empty or not found" in out


# ---------------------------------------------------------------------------
# (h) mutation-proof: self-attested vs witnessed render differently
# ---------------------------------------------------------------------------


def test_status_text_differs_between_self_attested_and_witnessed(
    tmp_path, dead_ts, stub_ts, monkeypatch
):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    monkeypatch.setenv("CAPSULE_EMIT_ATEXIT_WITNESS_TIMEOUT", "2.0")

    self_attested_ledger = tmp_path / "self_attested.jsonl"
    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=self_attested_ledger, witness_url=dead_ts)
    assert _wait_for(lambda: _has_stamp(self_attested_ledger), timeout=10.0)

    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()

    ts_url, received = stub_ts
    witnessed_ledger = tmp_path / "witnessed.jsonl"
    for i in range(2):
        seal(None, action=f"action-{i}", operator="acme", anchor=False,
             ledger=witnessed_ledger, witness_url=ts_url)
    assert _wait_for(lambda: len(received) >= 1)
    assert _wait_for(lambda: _has_stamp(witnessed_ledger))

    self_attested_result = status.compute_status(str(self_attested_ledger), offline=True)
    witnessed_result = status.compute_status(str(witnessed_ledger), offline=True)

    out_a = io.StringIO()
    status.render_status(self_attested_result, out=out_a)
    out_b = io.StringIO()
    status.render_status(witnessed_result, out=out_b)

    assert "self-attested" in out_a.getvalue()
    assert "witnessed" in out_b.getvalue()
    # Precise, not just "the strings differ somewhere" -- pins the *grade*
    # line itself, so a mutant that always reports one grade regardless of
    # input (independent of whether a witnesses section is rendered at all)
    # is caught here even if it happens to leave other lines unchanged.
    assert "latest checkpoint grade       self-attested" in out_a.getvalue()
    assert "latest checkpoint grade       witnessed" in out_b.getvalue()
    assert "witnessed" not in out_a.getvalue()
    assert out_a.getvalue() != out_b.getvalue()
