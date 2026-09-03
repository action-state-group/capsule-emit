#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Microsoft Agent Framework middleware quickstart — one list, every run and tool call seals.

What this demo does, end to end, with no external service and no LLM key:

  1. Starts a hermetic local stub SCITT Transparency Service (the same pattern
     capsule-emit's own test suite uses) — no network, no live anchor.
  2. Drives FOUR real ``agent_framework.Agent`` runs with the capsule middleware
     registered through the public ``Agent(..., middleware=[...])`` kwarg. No fork,
     no monkeypatch, no model-provider credentials: the chat client is a scripted
     ``BaseChatClient`` composed with the framework's own
     ``FunctionInvocationLayer``/``ChatMiddlewareLayer``, so the agent run loop, the
     function-calling loop, and both middleware pipelines are all the real thing.

       run 1  get_price + get_stock in one turn
              → run planned/confirmed + two tool planned/confirmed pairs, each
                chained to its OWN planned capsule
       run 2  submit_order raises           → planned + failed (chained)
       run 3  a SECOND middleware denies the call in path with MiddlewareFailure
              → planned + blocked (chained) — a refusal that took effect,
                recorded, and marked as somebody else's refusal
       run 4  the same deny raised as MiddlewareTermination (the graceful stop)
              → planned + blocked, with the effect status marked unobservable

  3. Reads the ledger back and runs ``verify()`` over every sealed capsule —
     the same offline check a third party would run.
  4. Renders the ledger with ``capsule-emit evidence`` — the fail-closed
     Verification-stage report.

This middleware is OBSERVATION ONLY. The seam it rides is in-path — a middleware here
can substitute ``context.result``, mutate the live tool list, or raise
``MiddlewareTermination``/``MiddlewareFailure`` — but this one never does: it reads,
seals, and always calls ``call_next()``. Runs 3 and 4 show it *recording* a refusal that
some other middleware performed.

Run:
    pip install "capsule-emit[msft-agent-framework]"
    python examples/msft-agent-framework/demo.py
"""
from __future__ import annotations

import asyncio
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
# 2. A scripted chat client — the no-LLM-key path
# ---------------------------------------------------------------------------


def scripted_client(script):
    """A keyless chat client that replays fixed assistant turns.

    ``BaseChatClient`` alone has no function-calling loop — ``Agent`` logs "the provided
    chat client does not support function invoking" and tools never run
    (``agent_framework/_agents.py:874``). Composing the framework's own
    ``FunctionInvocationLayer`` and ``ChatMiddlewareLayer`` over it (both public exports)
    gives the real loop with a scripted transport, which is what makes this demo
    keyless without being a mock of the parts under test.

    ``script`` is a list of turns; each is either a ``(tool_name, args, call_id)`` list
    (the assistant asking for tool calls) or a string (the final answer).
    """
    from agent_framework import (
        BaseChatClient,
        ChatMiddlewareLayer,
        ChatResponse,
        Content,
        FunctionInvocationLayer,
        Message,
    )

    class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
        # The framework reads the provider name off the class for OTel
        # (``_clients.py:271``); the capsule middleware reads the same field.
        OTEL_PROVIDER_NAME = "scripted"

        def __init__(self, turns, **kw):
            super().__init__(**kw)
            # ``Agent`` reads the model id off ``client.model`` (``_agents.py:902``).
            self.model = "scripted-demo-model"
            self.turns = list(turns)
            self.index = 0

        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            async def _turn():
                turn = self.turns[min(self.index, len(self.turns) - 1)]
                self.index += 1
                if isinstance(turn, str):
                    contents = [Content.from_text(turn)]
                else:
                    contents = [
                        Content.from_function_call(call_id=cid, name=name, arguments=args)
                        for name, args, cid in turn
                    ]
                return ChatResponse(
                    messages=[Message(role="assistant", contents=contents)],
                    response_id=f"scripted-{self.index}",
                )

            return _turn()

    return ScriptedChatClient(script)


# ---------------------------------------------------------------------------
# 3. Four real agent runs
# ---------------------------------------------------------------------------

PRICES = {"SKU-1": "12.00 USD", "SKU-2": "31.50 USD"}


def get_price(sku: str) -> str:
    """Look up the list price for a SKU."""
    return PRICES.get(sku, "unknown")


def get_stock(sku: str) -> str:
    """Look up on-hand stock for a SKU."""
    return f"{len(sku) * 7} units"


def submit_order(po: str) -> str:
    """Submit a purchase order (this one is wired to fail)."""
    raise RuntimeError(f"order system rejected {po}")


async def drive(ledger: pathlib.Path, anchor_url: str) -> None:
    from agent_framework import (
        Agent,
        FunctionMiddleware,
        MiddlewareFailure,
        MiddlewareTermination,
    )

    from capsule_emit.adapters.msft_agent_framework import capsule_middleware

    def agent_for(script, tools, extra=()):
        mw = capsule_middleware(
            operator="acme-co",
            developer="msft-af-demo@v1",
            ledger=str(ledger),
            anchor=True,
            anchor_url=anchor_url,
        )
        return Agent(
            scripted_client(script),
            "you are a procurement assistant",
            name="procurement",
            tools=list(tools),
            # capsule middleware FIRST: it wraps everything registered after it, so a
            # refusal raised by the gate below is on the record.
            middleware=[*mw, *extra],
        )

    # -- run 1: two tool calls in one turn -----------------------------------
    await agent_for(
        [
            [("get_price", {"sku": "SKU-1"}, "c1"), ("get_stock", {"sku": "SKU-1"}, "c2")],
            "SKU-1 is 12.00 USD, 35 units on hand.",
        ],
        [get_price, get_stock],
    ).run("price and stock for SKU-1?")
    print("run 1  get_price + get_stock in one turn   -> run pair + 2 tool pairs, each chained")

    # -- run 2: the tool raises ---------------------------------------------
    await agent_for(
        [[("submit_order", {"po": "PO-7"}, "c3")], "That order failed."],
        [submit_order],
    ).run("submit PO-7")
    print("run 2  submit_order raises                 -> planned+failed (chained)")

    # -- run 3: another middleware denies fail-closed ------------------------
    class DenyOrders(FunctionMiddleware):
        async def process(self, context, call_next):
            if context.function.name == "submit_order":
                raise MiddlewareFailure("purchase orders require human approval")
            await call_next()

    try:
        await agent_for(
            [[("submit_order", {"po": "PO-8"}, "c4")], "That needs approval."],
            [submit_order],
            [DenyOrders()],
        ).run("submit PO-8")
    except MiddlewareFailure as exc:
        print(f"run 3  denied with MiddlewareFailure       -> planned+blocked ({exc})")

    # -- run 4: the graceful stop -------------------------------------------
    class StopOrders(FunctionMiddleware):
        async def process(self, context, call_next):
            if context.function.name == "submit_order":
                raise MiddlewareTermination("policy: stopping the loop")
            await call_next()

    await agent_for(
        [[("submit_order", {"po": "PO-9"}, "c5")], "Stopped."],
        [submit_order],
        [StopOrders()],
    ).run("submit PO-9")
    print("run 4  stopped with MiddlewareTermination  -> planned+blocked, effect unobservable")

    print(
        "\nthe submit_order body never ran in runs 3 and 4 — but that is not something the\n"
        "middleware seam can see, so those capsules keep effect.status='planned' and carry\n"
        "agent_framework_effect_unobservable rather than claiming a dispatch.\n"
    )


# ---------------------------------------------------------------------------
# 4. Offline verification + evidence render
# ---------------------------------------------------------------------------


def main() -> int:
    anchor_url, srv = start_stub_ts()
    with tempfile.TemporaryDirectory() as td:
        ledger = pathlib.Path(td) / "ledger.jsonl"
        asyncio.run(drive(ledger, anchor_url))

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
            if compute.get("agent_framework_blocked_by"):
                note = f" blocked-by-{compute['agent_framework_blocked_by']}"
            elif compute.get("agent_framework_seam") == "agent":
                note = " run-seam"
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
