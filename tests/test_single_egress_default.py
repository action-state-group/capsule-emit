# SPDX-License-Identifier: Apache-2.0
"""Acceptance test for [O16-01-02]: per-seal `anchor=True` is killed as a
default, so a default-config `seal()`/`received()` call has exactly
ONE egress channel -- the checkpoint/witness stream -- not two.

Named-test-coverage entry (O16 migration audit, items 1-2): "a no-network
test with witness on/anchor removed asserting exactly one POST call site
exists." This test drives that literally: a single hermetic stub
Transparency Service stands in for both the legacy anchor endpoint and the
witness endpoint (same shape, same stub), default config only (no `anchor=`
kwarg, no `CAPSULE_ANCHOR` env), and asserts the stub receives exactly one
POST -- the checkpoint registration -- while `async_anchor` is never called
at all.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
import time

import pytest

import capsule_emit.core as core
import capsule_emit.witness as witness
from capsule_emit import seal
from capsule_emit.checkpoint.cose_wire import verify_checkpoint_cose_offline


def _checkpoint_dict_from_cose(cose_bytes: bytes) -> dict:
    """Decode+verify a COSE-wire checkpoint (real signature check, same as
    what capsule-anchor's witness route will independently do) and
    reconstruct the JSON CheckpointRecord-shaped dict this stub's own
    entry_hash logic already expects."""
    result = verify_checkpoint_cose_offline(cose_bytes)
    if not result.ok:
        raise ValueError(f"stub TS could not verify COSE checkpoint: {result.errors}")
    return result.decoded.to_checkpoint_record().to_dict()


class _StubTSHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args):  # silence stdlib access logging in test output
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = _checkpoint_dict_from_cose(raw)
        except ValueError as exc:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(str(exc).encode())
            return
        self.received.append({"path": self.path, "body": body})
        digest = body.get("capsule_id", "")
        entry_hash = hashlib.sha256(bytes.fromhex(digest)).hexdigest() if digest else ""
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


@pytest.fixture
def stub_ts():
    received: list[dict] = []
    handler_cls = type("_BoundStubTSHandler", (_StubTSHandler,), {"received": received})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", received
    srv.shutdown()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    core._disclosure_printed = False
    witness._counts.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    witness._notice_printed = False
    monkeypatch.delenv("CAPSULE_ANCHOR", raising=False)
    monkeypatch.delenv("AAC_ANCHOR_URL", raising=False)
    monkeypatch.delenv("CAPSULE_WITNESS", raising=False)
    yield
    core._disclosure_printed = False
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


def test_default_config_has_exactly_one_egress_channel(tmp_path, stub_ts, monkeypatch):
    """No `anchor=` kwarg, no `CAPSULE_ANCHOR` env, witness on (the 0.5.0
    default) and pointed at the same stub endpoint the legacy anchor channel
    would have used: exactly one POST (the checkpoint registration) reaches
    the network, and `async_anchor` is never invoked."""
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "3")
    ts_url, received = stub_ts

    anchor_calls = []
    monkeypatch.setattr(
        core, "async_anchor", lambda *a, **kw: anchor_calls.append(a) or pytest.fail(
            "the legacy anchor channel must not dispatch by default"
        )
    )

    ledger = tmp_path / "ledger.jsonl"
    results = [
        seal(
            None, action=f"action-{i}", operator="acme", ledger=ledger,
            witness_url=ts_url, anchor_url=ts_url,
        )
        for i in range(5)
    ]

    assert all(r.capsule_id for r in results)
    assert all(r.anchor_status == "skipped" for r in results), (
        "the legacy anchor channel must report 'skipped' by default"
    )
    assert anchor_calls == [], "async_anchor() must never be called with default config"

    assert _wait_for(lambda: len(received) >= 1), "the witness checkpoint was never registered"
    time.sleep(0.2)  # give a wrongly-dispatched second channel a chance to land
    assert len(received) == 1, (
        f"expected exactly one POST (the checkpoint registration), got {len(received)}: {received}"
    )
