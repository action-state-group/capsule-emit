#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""LlamaIndex span listener quickstart — one install() call, every tool call seals.

What this demo does, end to end, with no external service and no LLM key:

  1. Starts a hermetic local stub SCITT Transparency Service (the same pattern
     capsule-emit's own test suite uses) — no network, no live anchor.
  2. Calls ``LlamaIndexCapsuleListener(...).install()`` — one line, global, no
     change to the agent — and drives REAL ``FunctionAgent`` runs:

       run 1  get_price + get_stock (parallel)  → planned + confirmed x2
       run 2  submit_order raises               → planned + failed (errors are evidence)
       run 3  the model names a tool that does  → planned + failed, and the record
              not exist                          says the tool was never found
       run 4  a return_direct tool               → planned + confirmed, marked

     Everything below the model is the real SDK: the agent workflow, the tool
     executor, the concurrent fan-out, the instrumentation dispatcher. The only
     substitute is the model itself — a ~40-line scripted ``FunctionCallingLLM``
     that emits a fixed sequence of tool calls, so the demo needs no key and no
     socket beyond 127.0.0.1.

     In an application the registration is the same one line:

         LlamaIndexCapsuleListener(operator=..., developer=...).install()

  3. Reads the ledger back and runs capsule-emit's own offline verification over
     every sealed capsule — the same check a third party would run.
  4. Renders the ledger with ``capsule-emit evidence`` — the fail-closed
     Verification-stage report.

Run:
    pip install "capsule-emit[llamaindex]"
    python examples/llamaindex-listener/demo.py
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
from typing import Any

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
# 2. A scripted model — the only thing in this demo that is not the real SDK
# ---------------------------------------------------------------------------


def build_scripted_llm(script: list[list[dict]]) -> Any:
    """A ``FunctionCallingLLM`` that replays a fixed list of tool calls per turn.

    LlamaIndex ships no public no-model test double for the agent path, so the
    demo carries the smallest one that satisfies ``FunctionAgent``: it streams a
    single assistant message per turn whose ``tool_calls`` come from the script.
    Everything the agent does with those calls is the real implementation.
    """
    from llama_index.core.base.llms.types import (
        ChatMessage,
        ChatResponse,
        CompletionResponse,
        LLMMetadata,
    )
    from llama_index.core.llms.function_calling import FunctionCallingLLM
    from llama_index.core.llms.llm import ToolSelection

    class ScriptedLLM(FunctionCallingLLM):
        script: list[list[dict]] = []
        turn: int = 0

        @property
        def metadata(self) -> LLMMetadata:
            return LLMMetadata(
                model_name="scripted-demo-model",
                is_function_calling_model=True,
                is_chat_model=True,
            )

        def _response(self) -> ChatResponse:
            calls = self.script[self.turn] if self.turn < len(self.script) else []
            self.turn += 1
            message = ChatMessage(role="assistant", content="" if calls else "done")
            message.additional_kwargs["tool_calls"] = calls
            return ChatResponse(message=message, delta=message.content or "")

        def chat(self, messages, **kw):
            return self._response()

        async def achat(self, messages, **kw):
            return self._response()

        async def astream_chat(self, messages, **kw):
            response = self._response()

            async def gen():
                yield response

            return gen()

        def complete(self, prompt, formatted=False, **kw):
            return CompletionResponse(text="done")

        async def acomplete(self, prompt, formatted=False, **kw):
            return CompletionResponse(text="done")

        def stream_chat(self, messages, **kw):
            raise NotImplementedError("demo model does not stream synchronously")

        def stream_complete(self, prompt, formatted=False, **kw):
            raise NotImplementedError("demo model does not stream synchronously")

        async def astream_complete(self, prompt, formatted=False, **kw):
            raise NotImplementedError("demo model does not stream completions")

        def _prepare_chat_with_tools(self, tools, user_msg=None, chat_history=None, **kw):
            messages = list(chat_history or [])
            if user_msg:
                messages.append(
                    ChatMessage(role="user", content=user_msg)
                    if isinstance(user_msg, str)
                    else user_msg
                )
            return {"messages": messages}

        def get_tool_calls_from_response(self, response, error_on_no_tool_call=True, **kw):
            raw = response.message.additional_kwargs.get("tool_calls") or []
            return [
                ToolSelection(tool_id=c["id"], tool_name=c["name"], tool_kwargs=c["args"])
                for c in raw
            ]

    return ScriptedLLM(script=script)


# ---------------------------------------------------------------------------
# 3. Real FunctionAgent runs with the listener installed
# ---------------------------------------------------------------------------


async def run(ledger: pathlib.Path, anchor_url: str) -> None:
    from llama_index.core.agent.workflow import FunctionAgent
    from llama_index.core.tools import FunctionTool

    from capsule_emit.adapters.llamaindex_listener import LlamaIndexCapsuleListener

    listener = LlamaIndexCapsuleListener(
        operator="acme-co",
        developer="purchasing-agent@v1",
        ledger=ledger,
        anchor=True,
        anchor_url=anchor_url,
        anchor_wait=10.0,  # block for the real (stubbed) confirmation
    ).install()

    def get_price(sku: str) -> str:
        """Return the current price for a SKU."""
        return f"{sku} is 4225 cents"

    def get_stock(sku: str) -> str:
        """Return the units on hand for a SKU."""
        return f"{sku} has 118 units on hand"

    def submit_order(sku: str, units: int) -> str:
        """Submit a purchase order. Fails while the upstream ERP is down."""
        raise RuntimeError("ERP rejected the order: purchasing window closed")

    def order_receipt(sku: str) -> str:
        """Return the receipt text straight to the caller."""
        return f"RECEIPT {sku}: filed"

    tools = [
        FunctionTool.from_defaults(get_price),
        FunctionTool.from_defaults(get_stock),
        FunctionTool.from_defaults(submit_order),
        FunctionTool.from_defaults(order_receipt, return_direct=True),
    ]

    async def drive(label: str, calls: list[dict]) -> None:
        agent = FunctionAgent(
            tools=tools,
            llm=build_scripted_llm([calls, []]),
            system_prompt="You are a purchasing agent.",
        )
        await agent.run("do the thing")
        print(f"  {label}")

    print("driving real FunctionAgent runs (scripted model, no key, no network):")
    await drive(
        "run 1  get_price + get_stock (parallel)  -> planned+confirmed x2",
        [
            {"id": "call-1", "name": "get_price", "args": {"sku": "SKU-9"}},
            {"id": "call-2", "name": "get_stock", "args": {"sku": "SKU-9"}},
        ],
    )
    await drive(
        "run 2  submit_order raises               -> planned+failed (errors are evidence)",
        [{"id": "call-3", "name": "submit_order", "args": {"sku": "SKU-9", "units": 4}}],
    )
    await drive(
        "run 3  model names a tool that does not  -> planned+failed, never-found recorded",
        [{"id": "call-4", "name": "cancel_order", "args": {"sku": "SKU-9"}}],
    )
    await drive(
        "run 4  return_direct tool                -> planned+confirmed, marked",
        [{"id": "call-5", "name": "order_receipt", "args": {"sku": "SKU-9"}}],
    )

    listener.uninstall()
    print(
        "\nthe listener is uninstalled at the end of the run — install() adds a span\n"
        "handler to the process-wide dispatcher, so a library that installs one should\n"
        "take it back off again.\n"
    )


# ---------------------------------------------------------------------------
# 4. Offline verification + evidence render
# ---------------------------------------------------------------------------


def main() -> int:
    anchor_url, srv = start_stub_ts()
    with tempfile.TemporaryDirectory() as td:
        ledger = pathlib.Path(td) / "ledger.jsonl"
        asyncio.run(run(ledger, anchor_url))

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
            marks = ""
            if compute.get("llamaindex_return_direct"):
                marks = " return-direct"
            print(
                f"  {'PASS' if v.ok else 'FAIL'}  {cap['action_id'][:34]:36s}"
                f" {status:9s} {verdict:9s} {chained}  {cap['capsule_id'][:12]}…{marks}"
            )
        print(f"\n[step 4] Ledger: {len(caps)} capsule(s) sealed")

        proc = subprocess.run(
            [sys.executable, "-m", "capsule_emit.cli", "evidence", "--ledger", str(ledger)],
            capture_output=True,
            text=True,
        )
        print("\ncapsule-emit evidence (Verification-stage render, fail-closed):")
        print("\n".join(proc.stdout.splitlines()[:18]))
        srv.shutdown()

        if not ok or proc.returncode != 0 or len(caps) == 0:
            print("\nDEMO FAILED — a capsule did not verify or evidence refused to render")
            return 1
        print("\nall capsules verify offline; evidence renders clean. done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
