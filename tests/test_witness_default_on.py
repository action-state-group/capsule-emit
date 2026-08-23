# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [emit-witness-default-on]: the CLL checkpoint/witness
layer flips from opt-in to default-ON in ``capsule_emit.core.emit()``.

Covers the acceptance check verbatim:
- default `emit()` path checkpoints+witnesses a stream (digest-only) with
  zero opt-in, once the ledger crosses the cadence threshold
- the disable path works (``witness=False`` and ``CAPSULE_WITNESS=off``)
- non-streaming (single, below-cadence) emit cost is unchanged -- run in a
  subprocess so no other test importing ``capsule_emit.checkpoint`` can leave
  it warm in ``sys.modules`` and mask a regression here, same discipline as
  ``tests/test_checkpoint_layer0_cost.py``
- only the checkpoint's own digest crosses the wire (digest-only, no ledger
  content, no capsule content)
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from capsule_emit import emit, witness

_WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service -- POST /v1/digest is the only endpoint
# capsule_emit.checkpoint.emit.register_checkpoint() ever calls.
# ---------------------------------------------------------------------------


class _StubWitnessTSHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args):  # silence stdlib access logging in test output
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


@pytest.fixture(autouse=True)
def _clean_witness_state():
    """The witness module keeps process-global, per-ledger-path state by
    design (cheap counters + lazily-built MMR state). Reset it around every
    test so no test's counter/lock leaks into another's."""
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
# (a) default path checkpoints + registers once cadence is reached
# ---------------------------------------------------------------------------


def test_default_path_checkpoints_and_registers_a_stream(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")
    ts_url, received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    results = [
        emit(f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness_url=ts_url)
        for i in range(5)
    ]

    assert all(r.capsule_id for r in results)
    assert _wait_for(lambda: len(received) >= 1), "checkpoint was never registered with the TS"

    key = witness._resolve_key(str(ledger))
    state = witness._states[key]
    assert state.prev is not None, "no CheckpointRecord was ever built"
    assert state.prev.witnesses, "CheckpointRecord never recorded its WitnessRecord"


def test_default_witness_true_matches_calling_emit_with_no_witness_kwarg(tmp_path, stub_ts, monkeypatch):
    """The acceptance check is 'zero opt-in' -- confirm the *default* kwarg
    value (leaving ``witness`` unset entirely) behaves identically to an
    explicit ``witness=True``, i.e. the feature is genuinely on by default,
    not merely available."""
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    for i in range(2):
        emit(f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1), (
        "emit() with no witness= kwarg at all never produced a checkpoint -- "
        "the feature is not actually on by default"
    )


# ---------------------------------------------------------------------------
# (b) disable path works -- both witness=False and CAPSULE_WITNESS=off
# ---------------------------------------------------------------------------


def test_witness_false_opts_out(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    for i in range(4):
        emit(f"action-{i}", operator="acme", anchor=False, ledger=ledger,
             witness_url=ts_url, witness=False)

    time.sleep(0.2)  # give a wrongly-dispatched worker a chance to land
    assert received == [], "witness=False still registered a checkpoint with the TS"
    key = witness._resolve_key(str(ledger))
    assert key not in witness._states, "witness=False still built MMR state"


def test_capsule_witness_off_env_var_opts_out(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    for i in range(4):
        emit(f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness_url=ts_url)

    time.sleep(0.2)
    assert received == [], "CAPSULE_WITNESS=off still registered a checkpoint with the TS"


def test_explicit_witness_true_overrides_env_off(tmp_path, stub_ts, monkeypatch):
    """An explicit kwarg always wins over the env var (documented in
    ``witness.witness_enabled``) -- exercise the override direction the other
    two disable tests don't cover."""
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    for i in range(2):
        emit(f"action-{i}", operator="acme", anchor=False, ledger=ledger,
             witness_url=ts_url, witness=True)

    assert _wait_for(lambda: len(received) >= 1), (
        "witness=True did not override CAPSULE_WITNESS=off"
    )


# ---------------------------------------------------------------------------
# (c) digest-only -- only the checkpoint's own digest crosses the wire
# ---------------------------------------------------------------------------


def test_only_the_checkpoint_digest_is_posted(tmp_path, stub_ts, monkeypatch):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    ts_url, received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    emit(
        "transfer_funds",
        operator="a-very-identifying-operator-name",
        developer="agent-x@v9",
        agent_input={"account": "secret-account-number-12345", "amount": "1000.00"},
        anchor=False,
        ledger=ledger,
        witness_url=ts_url,
    )
    emit("transfer_funds", operator="a-very-identifying-operator-name",
         anchor=False, ledger=ledger, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    body = received[0]

    assert set(body.keys()) == {"capsule_id"}, f"unexpected fields posted to the TS: {body}"
    digest = body["capsule_id"]
    assert isinstance(digest, str) and len(digest) == 64
    int(digest, 16)  # must be hex

    raw = json.dumps(body)
    for leaked in ("secret-account-number-12345", "1000.00", "transfer_funds",
                   "a-very-identifying-operator-name", "agent-x@v9", str(ledger)):
        assert leaked not in raw, f"{leaked!r} leaked into the TS POST body: {raw}"


# ---------------------------------------------------------------------------
# (d) non-streaming cost is unchanged -- a single, below-cadence emit() never
#     imports capsule_emit.checkpoint. Subprocess, matching
#     tests/test_checkpoint_layer0_cost.py's discipline.
# ---------------------------------------------------------------------------


def test_single_default_emit_never_imports_checkpoint_subpackage(tmp_path):
    script = (
        "import sys\n"
        "sys.path.insert(0, {!r})\n"
        "import capsule_emit\n"
        "r = capsule_emit.emit('single-shot', operator='acme', anchor=False, "
        "ledger={!r})\n"
        "assert 'capsule_emit.checkpoint' not in sys.modules, ("
        "'a single below-cadence emit() call imported capsule_emit.checkpoint -- '"
        "'non-streaming cost regression')\n"
        "print('OK')\n"
    ).format(_WORKTREE_ROOT, str(tmp_path / "ledger.jsonl"))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_many_emits_below_cadence_still_never_import_checkpoint(tmp_path):
    """20 calls with the real default cadence (100) is still "non-streaming"
    for this purpose -- confirm the threshold, not just a single call, gates
    the import."""
    script = (
        "import sys\n"
        "sys.path.insert(0, {!r})\n"
        "import capsule_emit\n"
        "for i in range(20):\n"
        "    capsule_emit.emit(f'action-{{i}}', operator='acme', anchor=False, "
        "ledger={!r})\n"
        "assert 'capsule_emit.checkpoint' not in sys.modules, ("
        "'20 calls under the default cadence (100) imported capsule_emit.checkpoint')\n"
        "print('OK')\n"
    ).format(_WORKTREE_ROOT, str(tmp_path / "ledger.jsonl"))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# (e) crossing cadence *is* what imports checkpoint -- the mutant this file's
#     "never imports" tests guard against a false positive on (QUEUE_PROTOCOL
#     §7: every check must be able to fail).
# ---------------------------------------------------------------------------


def test_crossing_cadence_does_import_checkpoint_subpackage(tmp_path, stub_ts):
    ts_url, _ = stub_ts
    script = (
        "import sys\n"
        "sys.path.insert(0, {!r})\n"
        "import os\n"
        "os.environ['CAPSULE_WITNESS_CADENCE_ENTRIES'] = '2'\n"
        "import capsule_emit\n"
        "for i in range(3):\n"
        "    capsule_emit.emit(f'action-{{i}}', operator='acme', anchor=False, "
        "ledger={!r}, witness_url={!r})\n"
        "import time\n"
        "for _ in range(200):\n"
        "    if 'capsule_emit.checkpoint' in sys.modules:\n"
        "        break\n"
        "    time.sleep(0.01)\n"
        "assert 'capsule_emit.checkpoint' in sys.modules, ("
        "'crossing the cadence threshold never imported capsule_emit.checkpoint at all -- "
        "the default wiring is not actually running')\n"
        "print('OK')\n"
    ).format(_WORKTREE_ROOT, str(tmp_path / "ledger.jsonl"), ts_url)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# (f) a burst of cadence-crossings against the same ledger never races two
#     overlapping workers into a false "rollback" (the race this module's
#     _dispatch_locks fixes -- both 1 and 2 registrations are legitimate here
#     depending on whether the first worker finishes before the second
#     crossing, since a checkpoint is a valid, separate stream event either
#     way; a RollbackError/"monotonicity violated" warning is not).
# ---------------------------------------------------------------------------


def test_burst_of_cadence_crossings_never_produces_a_rollback_race(
    tmp_path, stub_ts, monkeypatch, recwarn
):
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")
    ts_url, received = stub_ts
    ledger = tmp_path / "ledger.jsonl"

    for i in range(7):  # crosses cadence twice (at 3 and 6) in a tight loop
        emit(f"action-{i}", operator="acme", anchor=False, ledger=ledger, witness_url=ts_url)

    assert _wait_for(lambda: len(received) >= 1)
    time.sleep(0.3)  # let a second dispatch (racing or sequential) land too

    rollback_warnings = [
        w for w in recwarn.list
        if "monotonicity" in str(w.message) or "RollbackError" in str(w.message)
    ]
    assert not rollback_warnings, (
        f"a cadence-crossing burst produced a false rollback race: "
        f"{[str(w.message) for w in rollback_warnings]}"
    )
    assert 1 <= len(received) <= 2, (
        f"expected 1 or 2 legitimate checkpoint registrations for a 7-entry/cadence-3 "
        f"burst, got {len(received)}"
    )
