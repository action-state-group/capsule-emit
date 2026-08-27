# SPDX-License-Identifier: Apache-2.0
"""Acceptance test for O16 audit item 3 ("Kill-switch scope, incl.
stamp-fetch + legacy anchor"):

Before this fix, ``witness=False`` / ``CAPSULE_WITNESS=off`` gated only
checkpoint posting. ``status``'s witness-receipt re-check had nothing to
gate it (item 17 didn't exist yet), and the legacy anchor channel was gated
by the wholly separate ``CAPSULE_ANCHOR`` env var -- so ``CAPSULE_WITNESS=off``
alone left the legacy anchor channel, and (once item 17 landed) ``status``'s
network re-check, fully live.

This is now ONE switch that zeroes all three egress paths:
- checkpoint posting (already correct pre-O16-03; re-asserted here)
- ``status``'s read-only witness-receipt re-check (net-new gate)
- the legacy anchor channel, even when explicitly re-enabled via
  ``anchor=True`` / ``CAPSULE_ANCHOR=legacy-on`` (net-new gate)

Named-test-coverage entry (O16 migration audit, item 3): "a single
no-network test asserting zero egress across checkpoint-post, status
stamp-fetch, and any configured legacy-anchor path." ``test_witness_off_is_zero_egress_across_all_three_paths``
below is that test, driven against one hermetic stub HTTP server standing in
for every endpoint (witness, legacy anchor, and the TS `status` would
re-check against) so any leak is directly observable as a request the stub
received.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import threading
import time

import pytest

import capsule_emit.core as core
import capsule_emit.witness as witness
from capsule_emit import seal, status
from capsule_emit.checkpoint.cose_wire import verify_checkpoint_cose_offline

# ---------------------------------------------------------------------------
# Hermetic stub Transparency Service -- same shape as
# tests/test_status.py's / tests/test_single_egress_default.py's, duplicated
# here so this file has no cross-file fixture dependency.
# ---------------------------------------------------------------------------


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
        if not raw:
            body = {}
        else:
            try:
                body = _checkpoint_dict_from_cose(raw)
            except ValueError as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
        self.received.append({"method": "POST", "path": self.path, "body": body})
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

    def do_GET(self):
        self.received.append({"method": "GET", "path": self.path, "body": None})
        self.send_response(404)
        self.end_headers()


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
    core._dep_notice_printed = False
    witness._notice_printed = False
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()
    monkeypatch.delenv("CAPSULE_ANCHOR", raising=False)
    monkeypatch.delenv("AAC_ANCHOR_URL", raising=False)
    monkeypatch.delenv("CAPSULE_WITNESS", raising=False)
    yield
    core._disclosure_printed = False
    core._dep_notice_printed = False
    witness._notice_printed = False
    witness._counts.clear()
    witness._armed_at.clear()
    witness._states.clear()
    witness._dispatch_locks.clear()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    ok = predicate()
    while not ok and time.monotonic() < deadline:
        time.sleep(0.01)
        ok = predicate()
    return ok


def _has_stamp(ledger_path):
    from capsule_emit import ledger as ledger_mod

    return any(
        e.get("kind") == ledger_mod.CHECKPOINT_STAMP_KIND
        for e in ledger_mod.read_ledger_entries(ledger_path)
    )


def test_witness_off_is_zero_egress_across_all_three_paths(tmp_path, stub_ts, monkeypatch):
    ts_url, received = stub_ts
    ledger_path = tmp_path / "ledger.jsonl"

    # --- Arrange: a witnessed checkpoint exists, from BEFORE the kill switch
    # is engaged -- this gives `status` something whose receipt it would
    # otherwise re-check over the network, so the gate below is exercised
    # against a real witnessed checkpoint, not an empty ledger.
    monkeypatch.setenv("CAPSULE_WITNESS_CADENCE_ENTRIES", "2")
    for i in range(2):
        seal(
            None, action=f"pre-{i}", operator="acme", anchor=False,
            ledger=ledger_path, witness_url=ts_url,
        )
    assert _wait_for(lambda: len(received) >= 1), "setup: the witness checkpoint never registered"
    assert _wait_for(lambda: _has_stamp(ledger_path)), "setup: the checkpoint stamp never persisted"
    received.clear()

    # --- Act: engage the kill switch, and explicitly configure the legacy
    # anchor channel back on (the exact combination that used to leak).
    monkeypatch.setenv("CAPSULE_WITNESS", "off")
    monkeypatch.setenv("CAPSULE_ANCHOR", "legacy-on")

    anchor_calls = []
    monkeypatch.setattr(
        core, "async_anchor",
        lambda *a, **kw: anchor_calls.append(a) or pytest.fail(
            "async_anchor() must never be called -- the witness kill switch is set"
        ),
    )

    r = seal(
        None, action="post-kill-switch", operator="acme",
        ledger=ledger_path, witness_url=ts_url, anchor_url=ts_url,
    )

    assert r.anchor_status == "skipped", (
        "the legacy anchor channel must stay off even with CAPSULE_ANCHOR=legacy-on "
        "once the witness kill switch is set"
    )
    assert anchor_calls == []

    recheck_calls = []

    def _boom(*a, **k):
        recheck_calls.append((a, k))
        raise AssertionError("verify_receipt_offline must not be called -- the witness kill switch is set")

    monkeypatch.setattr("capsule_emit.checkpoint.verify_receipt_offline", _boom)

    # Deliberately NOT passing offline=True -- the kill switch alone must be
    # sufficient to suppress the network re-check.
    result = status.compute_status(str(ledger_path), offline=False)

    assert recheck_calls == []
    assert result["witnessing_enabled_now"] is False
    cp = result["latest_checkpoint"]
    assert cp is not None
    assert cp["witnesses"][0]["confirmed"] is None

    out = io.StringIO()
    status.render_status(result, out=out)
    rendered = out.getvalue()
    assert "unconfirmed (witness disabled)" in rendered

    # --- Assert: give any wrongly-dispatched worker thread a moment to land,
    # then confirm the stub -- the one shared endpoint for witness, legacy
    # anchor, AND the receipt re-check -- received nothing at all.
    time.sleep(0.3)
    assert received == [], f"expected zero network egress, the stub received: {received}"
