#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""LangChain callback listener quickstart — one config line, every tool call seals.

What this demo does, end to end, with no external service and no LLM key:

  1. Starts a hermetic local stub SCITT Transparency Service (the same
     pattern capsule-emit's own test suite uses) — no network, no live anchor.
  2. Registers ``LangChainCapsuleListener`` via ``config={"callbacks": [...]}``
     and drives REAL langchain-core runnables and tools:

       root chain started                 → fyi capsule
       tool get_price   (succeeds)        → planned + confirmed (chained)
       tool submit_order (raises)         → planned + failed    (chained)
       root chain completed               → fyi capsule

  3. Reads the ledger back and runs ``agent_action_capsule.verify()`` over
     every sealed capsule — the same offline check a third party would run.
  4. Renders the ledger with ``capsule-emit evidence`` — the fail-closed
     Verification-stage report (venue-neutral: same command the goose
     contribution demo uses).

Run:
    pip install "capsule-emit[langchain]"
    python examples/langchain-listener/demo.py
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

from agent_action_capsule import verify

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
            statement_bytes = base64.b64decode(
                json.loads(self.rfile.read(length))["signed_statement_b64"]
            )
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
# 2. A real LangChain pipeline with the listener registered
# ---------------------------------------------------------------------------


def run(ledger: pathlib.Path, anchor_url: str) -> None:
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool

    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

    listener = LangChainCapsuleListener(
        operator="acme-co",
        developer="purchasing-agent@v1",
        ledger=ledger,
        anchor=True,
        anchor_url=anchor_url,
        anchor_wait=10.0,  # block for the real (stubbed) confirmation
    )
    config = {"callbacks": [listener]}

    @tool
    def get_price(sku: str) -> str:
        """Return the current price for a SKU."""
        return f"price for {sku}: 12.00 USD"

    @tool
    def submit_order(po: str) -> str:
        """Submit a purchase order to the order gateway."""
        raise RuntimeError("order gateway down")

    def buy(inputs: dict) -> dict:
        price = get_price.invoke({"sku": inputs["sku"]}, config=config)
        try:
            submit_order.invoke({"po": inputs["po"]}, config=config)
            status = "ordered"
        except RuntimeError:
            status = "failed: gateway down"  # error is EVIDENCE now, not silence
        return {"price": price, "status": status}

    pipeline = RunnableLambda(buy, name="purchase_pipeline")
    result = pipeline.invoke({"sku": "SKU-9", "po": "PO-7"}, config=config)
    print(f"pipeline result: {result}\n")


# ---------------------------------------------------------------------------
# 3. Offline verification + evidence render
# ---------------------------------------------------------------------------


def main() -> int:
    anchor_url, srv = start_stub_ts()
    with tempfile.TemporaryDirectory() as td:
        ledger = pathlib.Path(td) / "ledger.jsonl"
        run(ledger, anchor_url)

        caps = [json.loads(line) for line in ledger.read_text().splitlines()]
        print(f"sealed {len(caps)} capsules:")
        ok = True
        for cap in caps:
            v = verify(cap)
            ok &= v.ok
            status = cap.get("effect", {}).get("status", "-")
            chained = "chained" if cap.get("chain", {}).get("parent_capsule_id") else "  --  "
            print(
                f"  {'PASS' if v.ok else 'FAIL'}  {cap['action_id'][:34]:36s}"
                f" {status:9s} {chained}  {cap['capsule_id'][:12]}…"
            )
        print()

        proc = subprocess.run(
            [sys.executable, "-m", "capsule_emit.cli", "evidence", "--ledger", str(ledger)],
            capture_output=True,
            text=True,
        )
        print("capsule-emit evidence (Verification-stage render, fail-closed):")
        print("\n".join(proc.stdout.splitlines()[:18]))
        srv.shutdown()

        if not ok or proc.returncode != 0:
            print("\nDEMO FAILED — a capsule did not verify or evidence refused to render")
            return 1
        print("\nall capsules verify offline; evidence renders clean. done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
