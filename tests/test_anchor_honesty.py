# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for [capsule-emit-anchored-honesty] (capsule-emit#43).

``EmitResult.anchored`` MUST NOT be True unless a real ``AnchorResult``
confirmed the submission — it must never be set merely because anchoring was
requested. These tests exercise the three failure/success shapes:

- unreachable endpoint -> anchored stays False (default AND when waited)
- default non-blocking path -> a background failure never crashes and never
  silently reports True; the process-exit case surfaces it via a
  ``RuntimeWarning`` or the submission genuinely lands before exit
- ``anchor_wait`` against a real (stubbed) SCITT TS -> a genuine ``confirmed``
  result

The stubbed TS server implements the two endpoints ``agent_action_capsule``'s
``submit_anchor`` actually calls (``GET /anchor/authority-pubkey``,
``POST /transparency/register-statement``) well enough for a real round trip
to succeed, without writing test data to the live public anchor.
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
import warnings

import pytest

from capsule_emit import emit

_WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Hermetic stub SCITT Transparency Service
# ---------------------------------------------------------------------------


class _StubTSHandler(http.server.BaseHTTPRequestHandler):
    """Minimal stand-in for capsule-anchor's two submit_anchor()-facing routes."""

    pubkey_hex: str = ""
    received: list[dict] = []  # populated by the class the server was built with

    def log_message(self, *_args):  # silence stdlib access logging in test output
        pass

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/anchor/authority-pubkey":
            self._send_json(200, {"pubkey_hex": self.pubkey_hex, "key_id": "test-stub"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/transparency/register-statement":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            req = json.loads(raw)
            statement_bytes = base64.b64decode(req["signed_statement_b64"])
            entry_hash = hashlib.sha256(statement_bytes).hexdigest()
            self.received.append({"entry_hash": entry_hash})
            receipt_b64 = base64.b64encode(b"stub-receipt-not-a-real-cose-receipt").decode()
            self._send_json(200, {"receipt_b64": receipt_b64, "entry_hash": entry_hash})
        else:
            self.send_response(404)
            self.end_headers()


def _start_stub_ts(*, delay: float = 0.0):
    """Start a hermetic local stub TS server. Returns (base_url, received_list, stop_fn)."""
    pubkey_hex = os.urandom(32).hex()
    received: list[dict] = []

    handler_cls = type(
        "_BoundStubTSHandler",
        (_StubTSHandler,),
        {"pubkey_hex": pubkey_hex, "received": received},
    )

    if delay:
        _orig_post = handler_cls.do_POST

        def _delayed_post(self):
            import time

            time.sleep(delay)
            _orig_post(self)

        handler_cls.do_POST = _delayed_post

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    return base_url, received, srv.shutdown


@pytest.fixture
def stub_ts():
    base_url, received, stop = _start_stub_ts()
    yield base_url, received
    stop()


@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "ledger.jsonl"


# ---------------------------------------------------------------------------
# (a) unreachable endpoint -> anchored is False
# ---------------------------------------------------------------------------

_UNREACHABLE = "http://127.0.0.1:1/"  # nothing listens on port 1; connection refused fast


def test_unreachable_endpoint_default_path_never_reports_true(tmp_ledger):
    """The literal acceptance case: emit(anchor=True) against a dead endpoint
    must never report anchored=True, even on the default non-blocking path."""
    result = emit(
        action="anchor-honesty/unreachable-default",
        operator="o",
        developer="d",
        anchor=True,
        anchor_url=_UNREACHABLE,
        ledger=tmp_ledger,
    )
    assert result.anchored is False
    assert result.anchor_status == "submitted"


def test_unreachable_endpoint_with_wait_reports_real_failure(tmp_ledger):
    """The strong case: anchor_wait blocks for the real outcome, which is a
    genuine AnchorError, not a guess — anchored False + anchor_status 'failed'."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # atexit warning is covered elsewhere
        result = emit(
            action="anchor-honesty/unreachable-wait",
            operator="o",
            developer="d",
            anchor=True,
            anchor_url=_UNREACHABLE,
            anchor_wait=5.0,
            ledger=tmp_ledger,
        )
    assert result.anchored is False
    assert result.anchor_status == "failed"


# ---------------------------------------------------------------------------
# (c) anchor_wait returns a confirmed result against a stubbed anchor
# ---------------------------------------------------------------------------


def test_anchor_wait_returns_confirmed_result(stub_ts, tmp_ledger):
    base_url, received = stub_ts
    result = emit(
        action="anchor-honesty/confirmed",
        operator="o",
        developer="d",
        anchor=True,
        anchor_url=base_url,
        anchor_wait=10.0,
        ledger=tmp_ledger,
    )
    assert result.anchored is True
    assert result.anchor_status == "confirmed"
    assert received, "stub TS never received a register-statement POST"


def test_anchor_false_never_submits(stub_ts, tmp_ledger):
    base_url, received = stub_ts
    result = emit(
        action="anchor-honesty/skipped",
        operator="o",
        developer="d",
        anchor=False,
        anchor_url=base_url,
        ledger=tmp_ledger,
    )
    assert result.anchored is False
    assert result.anchor_status == "skipped"
    assert not received, "anchor=False must not submit anything"


# ---------------------------------------------------------------------------
# (b) process-exit race: submission either lands or produces a warning,
#     never a silent True
# ---------------------------------------------------------------------------

_SUBPROCESS_SCRIPT = """
import sys
import capsule_emit

result = capsule_emit.emit(
    action="anchor-honesty/subprocess-exit",
    operator="o",
    developer="d",
    anchor=True,
    anchor_url={anchor_url!r},
    ledger={ledger!r},
)
print(f"RESULT capsule_id={{result.capsule_id}} anchored={{result.anchored}} "
      f"anchor_status={{result.anchor_status}}")
# No anchor_wait, no sleep: the process falls off the end here immediately,
# exercising the exact race @thisjody reported. The atexit handler is the
# only thing standing between this and a silently dropped submission.
"""


def _run_subprocess_emit(*, anchor_url: str, ledger_path: str, atexit_timeout: str = "5.0"):
    script = _SUBPROCESS_SCRIPT.format(anchor_url=anchor_url, ledger=ledger_path)
    env = dict(os.environ)
    env["PYTHONPATH"] = _WORKTREE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["CAPSULE_EMIT_ATEXIT_ANCHOR_TIMEOUT"] = atexit_timeout
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_subprocess_exit_lands_when_ts_is_slow_but_reachable(stub_ts, tmp_path):
    """The submission is dispatched, the process exits almost immediately, but
    the atexit join keeps the interpreter alive long enough for the slow-but-
    reachable TS to actually receive it — the fix for the exact race filed in
    capsule-emit#43 (real signing-ceremony repro)."""
    base_url, received = stub_ts
    ledger_path = str(tmp_path / "ledger.jsonl")

    proc = _run_subprocess_emit(anchor_url=base_url, ledger_path=ledger_path)

    assert "RESULT" in proc.stdout, f"subprocess did not complete: {proc.stderr}"
    assert "anchored=False" in proc.stdout, (
        "the non-blocking path must never claim anchored=True: " + proc.stdout
    )
    assert received, (
        "submission never reached the TS before process exit — the exact "
        f"silent-drop bug from #43. stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_subprocess_exit_warns_when_endpoint_is_unreachable(tmp_path):
    """When the submission genuinely cannot land (dead endpoint), the process
    must not exit silently claiming success — it must warn."""
    ledger_path = str(tmp_path / "ledger.jsonl")

    proc = _run_subprocess_emit(anchor_url=_UNREACHABLE, ledger_path=ledger_path)

    assert "RESULT" in proc.stdout, f"subprocess did not complete: {proc.stderr}"
    assert "anchored=False" in proc.stdout
    assert "RuntimeWarning" in proc.stderr, (
        "an unreachable endpoint must surface a warning at process exit, "
        f"never a silent drop. stderr={proc.stderr!r}"
    )
    assert "failed" in proc.stderr.lower()
