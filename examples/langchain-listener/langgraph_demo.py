#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""LangGraph coverage quickstart — the LangChain listener, unmodified, sealing
tool calls a compiled ``StateGraph`` routes through ``ToolNode``.

What this demo does, end to end, with no external service and no LLM key:

  1. Starts a hermetic local stub SCITT Transparency Service (the same
     pattern capsule-emit's own test suite uses) — no network, no live anchor.
  2. Compiles a ``StateGraph`` (``chatbot`` node with a scripted
     ``GenericFakeChatModel`` -> ``ToolNode`` -> back to ``chatbot``) and
     registers ``LangChainCapsuleListener`` the same way you would on any
     LangChain runnable: ``config={"callbacks": [listener]}`` on
     ``graph.invoke()``. Nothing in the listener or the adapter package knows
     LangGraph exists — ``Pregel.invoke`` threads that ``config`` down through
     ``RunnableCallable`` into ``ToolNode``, which calls ``tool.invoke(args,
     config)`` exactly like any other LangChain caller.

       turn 1  get_price + get_stock, in ONE turn (parallel ToolNode)
               -> two planned + two confirmed, each chained to its OWN
                  planned capsule (pairing is by LangChain's run_id, so
                  interleaved starts/ends from concurrent execution are safe)
       turn 2  submit_order raises          -> planned + failed (chained)

  3. Reads the ledger back and runs ``verify_store_signed()`` over every
     sealed capsule — the same offline check a third party would run.
  4. Renders the ledger with ``capsule-emit evidence`` — the fail-closed
     Verification-stage report.

This demo does NOT exercise a checkpointer or ``interrupt()`` — see the
"What it does *not* do" section of docs/adapters/langchain.md for what
happens to these capsules across a checkpoint replay (short version: a
rewound-and-resumed tool call is a genuine second execution and seals a
second, correctly independent planned/confirmed pair; an ``interrupt()``
raised from inside a tool is indistinguishable in the ledger from that tool
erroring, and resuming re-runs any tool-body code that ran before the
interrupt point).

Run:
    pip install "capsule-emit[langchain]" "langgraph>=1.2,<2" langgraph-prebuilt
    python examples/langchain-listener/langgraph_demo.py
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

from capsule_emit.signing import verify_store_signed

# Keep the demo hermetic: no checkpoint ever leaves this process.
os.environ.setdefault("CAPSULE_WITNESS", "off")


def _tristate(result) -> str:
    """VALID / INVALID / UNSIGNED(warning) — digest+signature, not payload alone."""
    if not result.ok:
        return "INVALID"
    if any(f.code == "producer_signature_unclaimed" for f in result.findings):
        return "UNSIGNED(warning)"
    return "VALID"


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
# 2. A real, compiled LangGraph StateGraph with the listener registered
# ---------------------------------------------------------------------------


def run(ledger: pathlib.Path, anchor_url: str) -> None:
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    from capsule_emit.adapters.langchain_listener import LangChainCapsuleListener

    listener = LangChainCapsuleListener(
        operator="acme-co",
        developer="langgraph-agent@v1",
        ledger=ledger,
        anchor=True,
        anchor_url=anchor_url,
        anchor_wait=10.0,  # block for the real (stubbed) confirmation
        include_lifecycle=False,  # isolate the tool-call capsules for this demo
    )
    config = {"callbacks": [listener]}

    @tool
    def get_price(sku: str) -> str:
        """Return the current price for a SKU."""
        return f"price for {sku}: 12.00 USD"

    @tool
    def get_stock(sku: str) -> str:
        """Return current stock count for a SKU."""
        return f"stock for {sku}: 41 units"

    @tool
    def submit_order(po: str) -> str:
        """Submit a purchase order to the order gateway."""
        raise RuntimeError("order gateway down")

    def make_graph(tool_list, tool_calls, final_text):
        model = GenericFakeChatModel(
            messages=iter([AIMessage(content="", tool_calls=tool_calls), AIMessage(content=final_text)])
        )

        def chatbot(state):
            return {"messages": [model.invoke(state["messages"])]}

        graph = StateGraph(MessagesState)
        graph.add_node("chatbot", chatbot)
        graph.add_node("tools", ToolNode(tool_list))
        graph.add_edge(START, "chatbot")
        graph.add_conditional_edges("chatbot", tools_condition)
        graph.add_edge("tools", "chatbot")
        return graph.compile()

    # Turn 1: two tool calls in one AI turn -> ToolNode runs both concurrently.
    parallel_app = make_graph(
        [get_price, get_stock],
        [
            {"name": "get_price", "args": {"sku": "SKU-9"}, "id": "call_price", "type": "tool_call"},
            {"name": "get_stock", "args": {"sku": "SKU-9"}, "id": "call_stock", "type": "tool_call"},
        ],
        "SKU-9 is 12.00 USD, 41 in stock.",
    )
    result1 = parallel_app.invoke(
        {"messages": [HumanMessage(content="Price and stock for SKU-9?")]}, config=config
    )
    print(f"parallel-turn result: {result1['messages'][-1].content!r}")

    # Turn 2: a tool that raises -> ToolNode propagates it, listener seals `failed`.
    error_app = make_graph(
        [submit_order],
        [{"name": "submit_order", "args": {"po": "PO-7"}, "id": "call_order", "type": "tool_call"}],
        "unreachable — submit_order raises before this AIMessage is used",
    )
    try:
        error_app.invoke({"messages": [HumanMessage(content="Submit PO-7")]}, config=config)
    except RuntimeError as exc:
        print(f"error-turn result: graph raised {exc!r} (expected — order gateway down)\n")


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
        for cap, v in zip(caps, verify_store_signed(caps)):
            ok &= v.ok
            status = cap.get("effect", {}).get("status", "-")
            chained = "chained" if cap.get("chain", {}).get("parent_capsule_id") else "  --  "
            print(
                f"  {_tristate(v):17s}  {cap['action_id'][:34]:36s}"
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
