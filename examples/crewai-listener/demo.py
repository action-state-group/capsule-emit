#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""CrewAI event-bus listener quickstart — register once, every tool call seals.

What this demo does, end to end, with no external service:

  1. Starts a hermetic local stub SCITT Transparency Service (the same
     pattern capsule-emit's own test suite uses) — no network, no live anchor.
  2. Registers ``CapsuleEventListener`` and drives CrewAI's real event bus
     through a realistic crew run's event sequence:

       crew kickoff started
       tool started   (fetch_supplier)   → planned capsule
       tool finished  (fetch_supplier)   → confirmed capsule, chained
       tool started   (write_po)         → planned capsule
       tool error     (write_po)         → errored/failed capsule, chained
       crew kickoff completed

     (Events are emitted synthetically so the demo needs no LLM key; with a
     real crew the same listener seals the same way — register it before
     ``crew.kickoff()`` and change nothing else.)
  3. Reads the ledger back and runs ``agent_action_capsule.verify()`` over
     every sealed capsule — the same offline check a third party would run.

Run:
    pip install "capsule-emit[crewai]"
    python examples/crewai-listener/demo.py

Falls back to driving the framework-free core directly (same sealing logic,
synthetic duck-typed events) when crewai is not installed.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import pathlib
import sys
import tempfile
import threading
from datetime import datetime, timezone

from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# 1. Hermetic stub SCITT TS (mirrors tests/test_anchor_honesty.py)
# ---------------------------------------------------------------------------


class _StubTSHandler(http.server.BaseHTTPRequestHandler):
    pubkey_hex: str = ""

    def log_message(self, *_args):  # keep the demo output clean
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
            self._send_json(200, {"pubkey_hex": self.pubkey_hex, "key_id": "demo-stub"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/transparency/register-statement":
            length = int(self.headers.get("Content-Length", 0))
            statement_bytes = base64.b64decode(json.loads(self.rfile.read(length))["signed_statement_b64"])
            entry_hash = hashlib.sha256(statement_bytes).hexdigest()
            receipt_b64 = base64.b64encode(b"stub-receipt-not-a-real-cose-receipt").decode()
            self._send_json(200, {"receipt_b64": receipt_b64, "entry_hash": entry_hash})
        else:
            self.send_response(404)
            self.end_headers()


def start_stub_ts() -> tuple[str, http.server.ThreadingHTTPServer]:
    handler = type("_Bound", (_StubTSHandler,), {"pubkey_hex": os.urandom(32).hex()})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


# ---------------------------------------------------------------------------
# 2. Drive a crew-run event sequence through the listener
# ---------------------------------------------------------------------------


def run(ledger: pathlib.Path, anchor_url: str) -> None:
    listener_kw = dict(
        operator="acme-co",
        developer="ops-crew@v1",
        ledger=ledger,
        anchor=True,
        anchor_url=anchor_url,
        anchor_wait=10.0,  # block for the real (stubbed) confirmation
    )
    from types import SimpleNamespace

    try:
        from crewai.events import (
            CrewKickoffCompletedEvent,
            CrewKickoffStartedEvent,
            ToolUsageErrorEvent,
            ToolUsageFinishedEvent,
            ToolUsageStartedEvent,
            crewai_event_bus,
        )

        from capsule_emit.adapters.crewai_listener import CapsuleEventListener

        print("mode: real crewai event bus\n")

        def emit_and_wait(event) -> None:
            future = crewai_event_bus.emit(None, event)
            if future is not None:  # async bus returns a Future
                future.result(timeout=30)

        # scoped_handlers keeps the demo hermetic: only OUR listener handles
        # these synthetic events (crewai's console listener expects objects a
        # real crew run supplies). In a real app: just instantiate the
        # listener before crew.kickoff() — no scoping needed.
        scope = crewai_event_bus.scoped_handlers()
        scope.__enter__()
        CapsuleEventListener(**listener_kw)
        emit_and_wait(CrewKickoffStartedEvent(crew_name="po-crew", inputs={"supplier": "ACME"}))
        emit_and_wait(ToolUsageStartedEvent(tool_name="fetch_supplier", tool_args={"supplier": "ACME"}))
        emit_and_wait(
            ToolUsageFinishedEvent(
                tool_name="fetch_supplier",
                tool_args={"supplier": "ACME"},
                output="supplier ACME: terms NET30, contact po@acme.example",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
        emit_and_wait(ToolUsageStartedEvent(tool_name="write_po", tool_args={"po": "PO-7", "amount": "1240.00"}))
        emit_and_wait(
            ToolUsageErrorEvent(
                tool_name="write_po",
                tool_args={"po": "PO-7", "amount": "1240.00"},
                error="ERP rejected: duplicate PO number",
            )
        )
        # crewai's own console listener expects a CrewOutput-like object here
        crew_output = SimpleNamespace(raw="1 PO fetched, 1 write failed")
        emit_and_wait(CrewKickoffCompletedEvent(crew_name="po-crew", output=crew_output))
        scope.__exit__(None, None, None)
    except ImportError:
        from capsule_emit.adapters.crewai import CrewAIListenerCore

        core = CrewAIListenerCore(**listener_kw)
        print("mode: crewai not installed — framework-free core, synthetic events\n")
        core.on_crew_kickoff(SimpleNamespace(crew_name="po-crew"), "started")
        core.on_tool_started(SimpleNamespace(tool_name="fetch_supplier", tool_args={"supplier": "ACME"}))
        core.on_tool_finished(
            SimpleNamespace(
                tool_name="fetch_supplier",
                tool_args={"supplier": "ACME"},
                output="supplier ACME: terms NET30, contact po@acme.example",
            )
        )
        core.on_tool_started(SimpleNamespace(tool_name="write_po", tool_args={"po": "PO-7", "amount": "1240.00"}))
        core.on_tool_error(
            SimpleNamespace(
                tool_name="write_po",
                tool_args={"po": "PO-7", "amount": "1240.00"},
                error="ERP rejected: duplicate PO number",
            )
        )
        core.on_crew_kickoff(SimpleNamespace(crew_name="po-crew", output="1 PO fetched, 1 write failed"), "completed")


# ---------------------------------------------------------------------------
# 3. Read the ledger back and verify every capsule offline
# ---------------------------------------------------------------------------


def verify_ledger(ledger: pathlib.Path) -> bool:
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    print(f"{'action':34} {'effect':10} {'verdict':9} {'chained':8} {'verify()'}")
    print("-" * 75)
    all_ok = True
    for row in rows:
        cap = row["capsule"] if "capsule" in row else row
        result = verify(cap)
        all_ok &= result.ok
        effect = (cap.get("effect") or {}).get("status", "-")
        verdict = (cap.get("disposition") or {}).get("verdict_class", "-")
        chained = "yes" if cap.get("chain") else "-"
        print(f"{cap['action_id'][:34]:34} {effect:10} {verdict:9} {chained:8} {'OK' if result.ok else 'FAIL'}")
    print("-" * 75)
    print(f"{len(rows)} capsules, verify: {'ALL OK' if all_ok else 'FAILURES'}")
    return all_ok


def main() -> int:
    anchor_url, srv = start_stub_ts()
    print(f"stub SCITT TS: {anchor_url} (hermetic, in-process)")
    with tempfile.TemporaryDirectory() as tmp:
        ledger = pathlib.Path(tmp) / "ledger.jsonl"
        run(ledger, anchor_url)
        ok = verify_ledger(ledger)
    srv.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
