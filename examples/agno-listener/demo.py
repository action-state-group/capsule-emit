#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Agno tool-hook listener quickstart — one hook, every tool call seals.

What this demo does, end to end, with no external service and no LLM key:

  1. Starts a hermetic local stub SCITT Transparency Service (the same
     pattern capsule-emit's own test suite uses) — no network, no live anchor.
  2. Registers ``AgnoCapsuleListener().hook`` as an agno tool hook and drives
     REAL agno tool calls through ``FunctionCall.execute()``:

       tool get_price    (succeeds)        → planned + confirmed (chained)
       tool submit_order (raises)          → planned + failed    (chained)
       tool get_price    (agno cache hit)  → planned + confirmed, marked
                                             `agno_replay_of` — the tool did
                                             NOT re-run, and the record says so

     ``FunctionCall`` is agno's own tool-execution path, the one an ``Agent``
     drives once a model picks a tool. Going through it directly is what keeps
     this demo hermetic: the hook chain, the cache, and the error handling are
     all the real thing, with no model call in front of them. In an application
     the registration is one line:

         agent = Agent(model=..., tools=[...], tool_hooks=[listener.hook])

  3. Reads the ledger back and runs ``agent_action_capsule.verify()`` over
     every sealed capsule — the same offline check a third party would run.
  4. Renders the ledger with ``capsule-emit evidence`` — the fail-closed
     Verification-stage report.

Run:
    pip install "capsule-emit[agno]"
    python examples/agno-listener/demo.py
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
# 2. Real agno tool calls with the listener hook registered
# ---------------------------------------------------------------------------


def run(ledger: pathlib.Path, anchor_url: str, cache_dir: pathlib.Path) -> None:
    from agno.tools.function import Function, FunctionCall

    from capsule_emit.adapters.agno_listener import AgnoCapsuleListener

    listener = AgnoCapsuleListener(
        operator="acme-co",
        developer="purchasing-agent@v1",
        ledger=ledger,
        anchor=True,
        anchor_url=anchor_url,
        anchor_wait=10.0,  # block for the real (stubbed) confirmation
    )

    runs = {"get_price": 0}

    def get_price(sku: str) -> str:
        """Return the current price for a SKU."""
        runs["get_price"] += 1
        return f"price for {sku}: 12.00 USD"

    def submit_order(po: str) -> str:
        """Submit a purchase order to the order gateway."""
        raise RuntimeError("order gateway down")

    def as_tool(fn, **attrs):
        f = Function.from_callable(fn)
        f.tool_hooks = [listener.hook]  # the one line an Agent does for you
        for key, value in attrs.items():
            setattr(f, key, value)
        return f

    priced = as_tool(get_price, cache_results=True, cache_dir=str(cache_dir))
    ordering = as_tool(submit_order)

    first = FunctionCall(function=priced, arguments={"sku": "SKU-9"}).execute()
    print(f"get_price      -> {first.status}: {first.result}")

    failed = FunctionCall(function=ordering, arguments={"po": "PO-7"}).execute()
    print(f"submit_order   -> {failed.status}: {failed.error}  (sealed as evidence)")

    again = FunctionCall(function=priced, arguments={"sku": "SKU-9"}).execute()
    print(f"get_price again-> {again.status}: {again.result}")
    print(
        f"\nthe tool body ran {runs['get_price']} time(s) for 2 get_price calls — "
        "agno served the second from its cache,\nso the repeat capsule is marked "
        "agno_replay_of instead of claiming a fresh execution.\n"
    )


# ---------------------------------------------------------------------------
# 3. Offline verification + evidence render
# ---------------------------------------------------------------------------


def main() -> int:
    anchor_url, srv = start_stub_ts()
    with tempfile.TemporaryDirectory() as td:
        ledger = pathlib.Path(td) / "ledger.jsonl"
        run(ledger, anchor_url, pathlib.Path(td) / "agno-cache")

        caps = [json.loads(line) for line in ledger.read_text().splitlines()]
        print(f"sealed {len(caps)} capsules:")
        ok = True
        for cap in caps:
            v = verify(cap)
            ok &= v.ok
            status = cap.get("effect", {}).get("status", "-")
            chained = "chained" if cap.get("chain", {}).get("parent_capsule_id") else "  --  "
            compute = cap.get("model_attestation", {}).get("compute_attestation", {})
            replay = " replay-of-" + compute["agno_replay_of"][:8] if compute.get("agno_replay_of") else ""
            print(
                f"  {'PASS' if v.ok else 'FAIL'}  {cap['action_id'][:34]:36s}"
                f" {status:9s} {chained}  {cap['capsule_id'][:12]}…{replay}"
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
