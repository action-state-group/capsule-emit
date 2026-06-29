# SPDX-License-Identifier: Apache-2.0
"""Precision/honesty tests (r4): failOpen warning, digest salting, receipt surface.

Covers:
- salt_digests=True (default): per-emit random salt → unique digests for identical inputs
- salt_digests=True: digest_salt stored in compute_attestation
- salt_digests=False: deterministic digest (backward-compatible / opt-out)
- salt_digests threads through MCPCapsuleEmitter → emit_capsule → emit()
- failOpen warning: anchor HTTP failure logs WARNING, does not raise
- EmitResult.anchored=True set when anchor attempt started (regardless of success)
- EmitResult.receipt populated by wait_receipt() on success
- EmitResult.wait_receipt() returns None when anchor=False
- EmitResult.wait_receipt() returns None on anchor failure (after warning)
- EmitResult.receipt field present and None initially (anchor=True, not waited)
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from capsule_emit import emit
from capsule_emit.adapters.mcp import MCPCapsuleEmitter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit(tmp_path, **kw) -> Any:
    kw.setdefault("anchor", False)
    kw.setdefault("ledger", tmp_path / "ledger.jsonl")
    return emit(action="test_action", operator="org", developer="agent@v1", **kw)


def _ca(cap) -> dict:
    return cap.capsule["model_attestation"]["compute_attestation"]


def _mcp_emitter(tmp_path, **kw) -> MCPCapsuleEmitter:
    kw.setdefault("anchor", False)
    return MCPCapsuleEmitter(
        operator="test-org",
        developer="agent@v1",
        ledger=tmp_path / "ledger.jsonl",
        **kw,
    )


# ---------------------------------------------------------------------------
# Tiny HTTP server fixture: returns 200 + JSON receipt
# ---------------------------------------------------------------------------


class _AnchorHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"ok": True, "tree_size": 42}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # suppress test noise


class _FailHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):
        pass


def _start_server(handler_class) -> tuple[str, HTTPServer]:
    srv = HTTPServer(("127.0.0.1", 0), handler_class)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}/anchor", srv


# ---------------------------------------------------------------------------
# digest salting
# ---------------------------------------------------------------------------


def test_salt_digests_default_produces_unique_digests(tmp_path):
    """Default salt_digests=True: identical inputs → different digests per call."""
    inp = {"vendor": "ACME", "total": 100}
    cap_a = _emit(tmp_path, agent_input=inp)
    cap_b = _emit(tmp_path, agent_input=inp)
    ca_a = _ca(cap_a)
    ca_b = _ca(cap_b)
    assert ca_a["agent_input_digest"] != ca_b["agent_input_digest"], (
        "salt_digests=True must produce unique digests for identical inputs"
    )


def test_salt_digests_default_stores_salt_in_compute_attestation(tmp_path):
    """digest_salt is stored in compute_attestation when salt_digests=True."""
    cap = _emit(tmp_path, agent_input={"x": 1}, agent_output={"y": 2})
    ca = _ca(cap)
    assert "digest_salt" in ca, "digest_salt must appear in compute_attestation"
    assert len(ca["digest_salt"]) == 32, "salt must be 16-byte hex (32 chars)"


def test_salt_digests_false_is_deterministic(tmp_path):
    """salt_digests=False: same input → same digest across calls."""
    inp = {"vendor": "ACME", "total": 100}
    cap_a = _emit(tmp_path, agent_input=inp, salt_digests=False)
    cap_b = _emit(tmp_path, agent_input=inp, salt_digests=False)
    ca_a = _ca(cap_a)
    ca_b = _ca(cap_b)
    assert ca_a["agent_input_digest"] == ca_b["agent_input_digest"]
    assert "digest_salt" not in ca_a, "no digest_salt field when salt_digests=False"


def test_salt_digests_no_input_no_salt_field(tmp_path):
    """When no agent_input/output, no digest_salt appears (nothing to salt)."""
    cap = emit(
        action="test",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_path / "ledger.jsonl",
        salt_digests=True,
    )
    # No agent_input/output → emit_salt generated but no digests to store,
    # so digest_salt should NOT appear (no compute_attestation at all or missing key)
    ma = cap.capsule.get("model_attestation", {})
    ca = ma.get("compute_attestation", {})
    assert "digest_salt" not in ca


def test_mcp_salt_digests_threads_through(tmp_path):
    """MCPCapsuleEmitter(salt_digests=True) produces unique digests per call."""
    emitter = _mcp_emitter(tmp_path, salt_digests=True)

    @emitter.tool("fn")
    def fn(x: int) -> int:
        return x

    fn(x=1)
    fn(x=1)
    results = emitter.results
    ca_a = results[0].capsule["model_attestation"]["compute_attestation"]
    ca_b = results[1].capsule["model_attestation"]["compute_attestation"]
    assert ca_a["agent_input_digest"] != ca_b["agent_input_digest"], (
        "MCPCapsuleEmitter(salt_digests=True) must produce unique digests per call"
    )
    assert "digest_salt" in ca_a


def test_mcp_salt_digests_false_deterministic(tmp_path):
    """MCPCapsuleEmitter(salt_digests=False) produces same digest for same input."""
    emitter = _mcp_emitter(tmp_path, salt_digests=False)

    @emitter.tool("fn")
    def fn(x: int) -> int:
        return x

    fn(x=1)
    fn(x=1)
    results = emitter.results
    ca_a = results[0].capsule["model_attestation"]["compute_attestation"]
    ca_b = results[1].capsule["model_attestation"]["compute_attestation"]
    assert ca_a["agent_input_digest"] == ca_b["agent_input_digest"]


# ---------------------------------------------------------------------------
# failOpen warning
# ---------------------------------------------------------------------------


def test_failopen_warning_on_anchor_failure(tmp_path, caplog):
    """Anchor HTTP failure logs WARNING; does not raise; anchored=True (attempt recorded)."""
    url, srv = _start_server(_FailHandler)
    try:
        with caplog.at_level(logging.WARNING, logger="capsule_emit.core"):
            cap = emit(
                action="test",
                operator="org",
                developer="agent@v1",
                anchor=True,
                anchor_url=url,
                ledger=tmp_path / "ledger.jsonl",
                agent_input={"x": 1},
            )
            cap.wait_receipt(timeout=5.0)  # wait for the thread to finish
    finally:
        srv.shutdown()

    assert cap.anchored is True, "anchored must be True when anchor attempt was started"
    assert any("FAILED" in r.message for r in caplog.records), (
        "expected 'FAILED' warning in capsule_emit.core logger"
    )
    assert cap.receipt is None, "receipt must be None when anchor failed"


def test_failopen_no_raise_on_network_error(tmp_path, caplog):
    """Network-unreachable anchor does not raise — failOpen."""
    bad_url = "http://127.0.0.1:1/"  # port 1 is system-reserved; connection refused
    with caplog.at_level(logging.WARNING, logger="capsule_emit.core"):
        cap = emit(
            action="test",
            operator="org",
            developer="agent@v1",
            anchor=True,
            anchor_url=bad_url,
            ledger=tmp_path / "ledger.jsonl",
        )
        cap.wait_receipt(timeout=5.0)

    assert cap.anchored is True
    assert any("FAILED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# receipt surface
# ---------------------------------------------------------------------------


def test_receipt_populated_on_success(tmp_path):
    """wait_receipt() returns the anchor's JSON response dict on success."""
    url, srv = _start_server(_AnchorHandler)
    try:
        cap = emit(
            action="test",
            operator="org",
            developer="agent@v1",
            anchor=True,
            anchor_url=url,
            ledger=tmp_path / "ledger.jsonl",
            agent_input={"x": 1},
        )
        receipt = cap.wait_receipt(timeout=5.0)
    finally:
        srv.shutdown()

    assert receipt is not None, "wait_receipt() must return dict on success"
    assert receipt.get("ok") is True
    assert cap.receipt == receipt, "wait_receipt() result stored on .receipt"


def test_receipt_none_when_anchor_false(tmp_path):
    """wait_receipt() returns None when anchor=False was passed."""
    cap = emit(
        action="test",
        operator="org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_path / "ledger.jsonl",
    )
    assert cap.receipt is None
    assert cap.wait_receipt(timeout=1.0) is None


def test_emit_result_receipt_field_present(tmp_path):
    """EmitResult always has a .receipt attribute (None by default)."""
    cap = _emit(tmp_path)
    assert hasattr(cap, "receipt")
    assert cap.receipt is None


def test_wait_receipt_is_idempotent(tmp_path):
    """Calling wait_receipt() twice returns the same value."""
    url, srv = _start_server(_AnchorHandler)
    try:
        cap = emit(
            action="test",
            operator="org",
            developer="agent@v1",
            anchor=True,
            anchor_url=url,
            ledger=tmp_path / "ledger.jsonl",
        )
        r1 = cap.wait_receipt(timeout=5.0)
        r2 = cap.wait_receipt(timeout=5.0)
    finally:
        srv.shutdown()

    assert r1 == r2


def test_anchored_true_means_attempt_not_confirmed(tmp_path):
    """anchored=True reflects that submission was started, NOT that it succeeded."""
    bad_url = "http://127.0.0.1:1/"
    cap = emit(
        action="test",
        operator="org",
        developer="agent@v1",
        anchor=True,
        anchor_url=bad_url,
        ledger=tmp_path / "ledger.jsonl",
    )
    assert cap.anchored is True  # attempt started
    cap.wait_receipt(timeout=3.0)
    assert cap.receipt is None  # but it failed
