# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [capsule-emit-witness-required-profile] (per
JamesCarnley's projnanda/nandatown#217 review): a fail-closed
``require_witness=True`` option on ``_emit_capsule()``/``seal()`` that raises
``WitnessRequiredError`` rather than silently returning a local-only capsule
when no configured witness confirms it, plus the granular
``EmitResult.witness_outcome`` states.

Same hermetic stub-Transparency-Service pattern as
``tests/test_witness_outage_queue.py`` (no cross-file fixture dependency, per
this repo's existing convention) -- a real, connection-refused dead port for
"witness down," a real local HTTP server for "witness up."
"""
from __future__ import annotations

import http.server
import json
import threading

import pytest
from _stub_receipt import build_stub_receipt_b64, checkpoint_dict_from_cose, checkpoint_entry_hash

from capsule_emit import seal, witness
from capsule_emit.witness import WitnessRequiredError


class _StubWitnessTSHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path == "/checkpoints":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            body = checkpoint_dict_from_cose(raw)
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
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _StubWitnessTSHandler)
    actual_port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{actual_port}", srv.shutdown


@pytest.fixture
def stub_ts():
    base_url, stop = _start_stub_ts()
    yield base_url
    stop()


@pytest.fixture
def dead_ts():
    """A URL nothing is listening on -- every registration attempt fails
    fast with connection-refused, same fixture shape as
    test_witness_outage_queue.py's."""
    return "http://127.0.0.1:1"


@pytest.fixture(autouse=True)
def _clean_witness_state():
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    yield
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()


# ---------------------------------------------------------------------------
# The core mutant: witness up -> passes, witness down -> fails.
# ---------------------------------------------------------------------------


def test_require_witness_true_succeeds_when_witness_is_up(tmp_path, stub_ts):
    ledger_path = tmp_path / "ledger.jsonl"

    result = seal(
        {"amount": 1},
        action="pay",
        operator="acme",
        anchor=False,
        ledger=ledger_path,
        witness_url=stub_ts,
        require_witness=True,
    )

    assert result.witness_outcome == "witness_receipt_obtained"


def test_require_witness_true_raises_when_witness_is_down(tmp_path, dead_ts):
    ledger_path = tmp_path / "ledger.jsonl"

    with pytest.raises(WitnessRequiredError):
        seal(
            {"amount": 1},
            action="pay",
            operator="acme",
            anchor=False,
            ledger=ledger_path,
            witness_url=dead_ts,
            require_witness=True,
        )


def test_require_witness_true_raises_when_witnessing_is_disabled(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"

    with pytest.raises(WitnessRequiredError):
        seal(
            {"amount": 1},
            action="pay",
            operator="acme",
            anchor=False,
            ledger=ledger_path,
            witness=False,
            require_witness=True,
        )


def test_require_witness_true_with_one_live_and_one_dead_endpoint_still_succeeds(
    tmp_path, stub_ts, dead_ts
):
    """Fan-out isolation applies here too -- one endpoint confirming is
    enough (any-of semantics), same as the default best-effort path."""
    ledger_path = tmp_path / "ledger.jsonl"

    result = seal(
        {"amount": 1},
        action="pay",
        operator="acme",
        anchor=False,
        ledger=ledger_path,
        witness_url=[stub_ts, dead_ts],
        require_witness=True,
    )

    assert result.witness_outcome == "witness_receipt_obtained"


# ---------------------------------------------------------------------------
# Default (require_witness=False, the unchanged posture) never blocks or
# raises regardless of witness reachability -- only opting in changes
# anything.
# ---------------------------------------------------------------------------


def test_default_best_effort_path_is_unchanged_even_when_witness_is_down(tmp_path, dead_ts):
    ledger_path = tmp_path / "ledger.jsonl"

    result = seal(
        {"amount": 1},
        action="pay",
        operator="acme",
        anchor=False,
        ledger=ledger_path,
        witness_url=dead_ts,
    )

    assert result.witness_outcome == "checkpoint_queued"


def test_default_witness_outcome_is_local_sealed_when_witnessing_is_off(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"

    result = seal(
        {"amount": 1},
        action="pay",
        operator="acme",
        anchor=False,
        ledger=ledger_path,
        witness=False,
    )

    assert result.witness_outcome == "local_sealed"


def test_witness_outcome_is_independent_of_the_legacy_anchored_field(tmp_path, stub_ts):
    """anchored/anchor_status report the legacy, non-default anchor channel
    only -- require_witness=True must never change their meaning (existing
    callers reading .anchored are unaffected)."""
    ledger_path = tmp_path / "ledger.jsonl"

    result = seal(
        {"amount": 1},
        action="pay",
        operator="acme",
        anchor=False,
        ledger=ledger_path,
        witness_url=stub_ts,
        require_witness=True,
    )

    assert result.witness_outcome == "witness_receipt_obtained"
    assert result.anchored is False
    assert result.anchor_status == "skipped"
