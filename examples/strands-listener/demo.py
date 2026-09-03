#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Strands Agents hook-listener quickstart — one HookProvider, every tool call seals.

What this demo does, end to end, with no external service and no LLM key:

  1. Starts a hermetic local stub SCITT Transparency Service (the same pattern
     capsule-emit's own test suite uses) — no network, no live anchor.
  2. Drives THREE real ``strands.Agent`` runs with
     ``StrandsCapsuleListener`` registered through the public
     ``Agent(hooks=[...])`` kwarg. No fork, no monkeypatch, no model provider
     credentials: the model is a scripted ``strands.models.Model`` subclass that
     replays a fixed assistant turn, so the agent event loop, the concurrent
     tool executor, and the hook registry are all the real thing.

       run 1  get_price + get_stock, concurrently in one turn
              → two planned + two confirmed, each chained to its OWN planned
                capsule (pairing is by toolUseId, which is why interleaving is safe)
       run 2  submit_order raises          → planned + failed (chained)
       run 3  a SECOND hook cancels the call in path via cancel_tool
              → planned + blocked (chained) — a refusal that took effect,
                recorded, and marked as somebody else's refusal
       run 4  a hook asks the executor to retry once
              → two attempts, four capsules, the second pair marked
                `strands_attempt: 2` / `strands_retry_of`

  3. Reads the ledger back and runs ``verify()`` over every sealed capsule —
     the same offline check a third party would run.
  4. Renders the ledger with ``capsule-emit evidence`` — the fail-closed
     Verification-stage report.

This listener is OBSERVATION ONLY. ``BeforeToolCallEvent.cancel_tool`` is
writable and the executor honours it in path, but the listener never writes it:
deny belongs to the gate layer. Run 3 shows the listener *recording* a
cancellation that some other hook performed.

Run:
    pip install "capsule-emit[strands]"
    python examples/strands-listener/demo.py
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

# Keep the demo hermetic: no checkpoint ever leaves this process.
os.environ.setdefault("CAPSULE_WITNESS", "off")

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
# 2. A scripted model — the no-LLM-key path
# ---------------------------------------------------------------------------


def scripted_model(script):
    """A ``strands.models.Model`` that replays fixed assistant turns.

    Modelled on the SDK's own test fixture (``tests/fixtures/
    mocked_model_provider.py`` in strands-agents/harness-sdk) but written here
    so the demo needs only the released wheel. Every layer BELOW the model —
    event loop, tool executor, hook registry — is the real SDK.
    """
    from strands.models import Model

    class ScriptedModel(Model):
        def __init__(self, turns):
            self.turns = list(turns)
            self.index = 0

        def get_config(self):
            return {"model_id": "scripted-demo-model"}

        def update_config(self, **_kw):
            pass

        async def structured_output(self, *_a, **_kw):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None,
                         tool_choice=None, **_kw):
            turn = self.turns[self.index]
            self.index += 1
            yield {"messageStart": {"role": "assistant"}}
            stop_reason = "end_turn"
            for content in turn:
                if "text" in content:
                    yield {"contentBlockStart": {"start": {}}}
                    yield {"contentBlockDelta": {"delta": {"text": content["text"]}}}
                    yield {"contentBlockStop": {}}
                if "toolUse" in content:
                    stop_reason = "tool_use"
                    use = content["toolUse"]
                    yield {"contentBlockStart": {"start": {"toolUse": {
                        "name": use["name"], "toolUseId": use["toolUseId"]}}}}
                    yield {"contentBlockDelta": {"delta": {"toolUse": {
                        "input": json.dumps(use["input"])}}}}
                    yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": stop_reason}}

    return ScriptedModel(script)


def call(name, tool_use_id, **args):
    return {"toolUse": {"name": name, "toolUseId": tool_use_id, "input": args}}


# ---------------------------------------------------------------------------
# 3. Real strands agent runs with the listener registered
# ---------------------------------------------------------------------------


def run(ledger: pathlib.Path, anchor_url: str) -> None:
    from strands import Agent, tool
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

    from capsule_emit.adapters.strands_listener import StrandsCapsuleListener

    listener = StrandsCapsuleListener(
        operator="acme-co",
        developer="purchasing-agent@v1",
        ledger=ledger,
        anchor=True,
        anchor_url=anchor_url,
        anchor_wait=10.0,  # block for the real (stubbed) confirmation
    )

    runs = {"get_price": 0}

    @tool
    def get_price(sku: str) -> str:
        """Return the current price for a SKU."""
        runs["get_price"] += 1
        return f"price for {sku}: 12.00 USD"

    @tool
    def get_stock(sku: str) -> str:
        """Return the stock level for a SKU."""
        return f"stock for {sku}: 4 units"

    @tool
    def submit_order(po: str) -> str:
        """Submit a purchase order to the order gateway."""
        raise RuntimeError("order gateway down")

    def agent_for(script, tools, extra_hooks=()):
        return Agent(
            model=scripted_model(script),
            tools=list(tools),
            hooks=[listener, *extra_hooks],
            callback_handler=None,  # quiet: this demo prints its own transcript
        )

    # -- run 1: two tools, one turn, concurrent executor --------------------
    agent_for(
        [[call("get_price", "tu-price", sku="SKU-9"), call("get_stock", "tu-stock", sku="SKU-9")],
         [{"text": "SKU-9 is 12.00 USD with 4 in stock."}]],
        [get_price, get_stock],
    )("price and stock for SKU-9?")
    print("run 1  get_price + get_stock (concurrent)  -> planned+confirmed x2")

    # -- run 2: the tool raises --------------------------------------------
    agent_for(
        [[call("submit_order", "tu-order", po="PO-7")], [{"text": "The order gateway is down."}]],
        [submit_order],
    )("submit PO-7")
    print("run 2  submit_order raises                 -> planned+failed (errors are evidence)")

    # -- run 3: a DIFFERENT hook cancels in path ---------------------------
    class DenyOrders:
        """Stands in for a gate layer. The capsule listener never does this."""

        def register_hooks(self, registry, **_kw):
            registry.add_callback(BeforeToolCallEvent, self.deny)

        def deny(self, event):
            event.cancel_tool = "purchase orders require human approval"

    agent_for(
        [[call("submit_order", "tu-denied", po="PO-8")], [{"text": "That needs approval."}]],
        [submit_order],
        [DenyOrders()],
    )("submit PO-8")
    print("run 3  submit_order cancelled by a gate    -> planned+blocked (refusal recorded)")

    # -- run 4: the executor's retry loop -----------------------------------
    class RetryOnce:
        def __init__(self):
            self.done = False

        def register_hooks(self, registry, **_kw):
            registry.add_callback(AfterToolCallEvent, self.maybe_retry)

        def maybe_retry(self, event):
            if not self.done:
                self.done = True
                event.retry = True

    agent_for(
        [[call("get_price", "tu-retry", sku="SKU-1")], [{"text": "SKU-1 is 12.00 USD."}]],
        [get_price],
        [RetryOnce()],
    )("price for SKU-1?")
    print("run 4  a hook forces one retry             -> 2 attempts, second pair marked")
    print(
        f"\nthe get_price body ran {runs['get_price']} times: once in run 1, twice in run 4 "
        "(the executor\nre-entered its retry loop), and the repeat capsules carry "
        "strands_attempt / strands_retry_of\nrather than looking like two independent calls.\n"
    )


# ---------------------------------------------------------------------------
# 4. Offline verification + evidence render
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
            verdict = cap.get("disposition", {}).get("verdict_class", "-")
            chained = "chained" if cap.get("chain", {}).get("parent_capsule_id") else "  --  "
            compute = cap.get("model_attestation", {}).get("compute_attestation", {})
            note = ""
            if compute.get("strands_retry_of"):
                note = f" attempt-{compute['strands_attempt']}-of-{compute['strands_retry_of'][:8]}"
            elif compute.get("strands_cancelled_by_hook"):
                note = " cancelled-by-another-hook"
            print(
                f"  {'PASS' if v.ok else 'FAIL'}  {cap['action_id'][:34]:36s}"
                f" {status:9s} {verdict:9s} {chained}  {cap['capsule_id'][:12]}…{note}"
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
