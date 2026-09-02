#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""OpenAI Agents SDK listener quickstart — every tool call seals, both surfaces.

What this demo does, end to end, with **no API key and no provider call**:

  1. Starts a hermetic local stub SCITT Transparency Service (the same pattern
     capsule-emit's own test suite uses) — no external network, no live anchor.
  2. Drives REAL ``Runner.run`` turns against ``agents.testing.ScriptedModel``,
     the SDK's own shipped deterministic test double. The runner, the tool
     executor, the tracing pipeline and the lifecycle hooks are all the real
     thing; only the model is scripted, so no key is ever read.

     run 1  two concurrent tool calls   → planned + confirmed ×2, paired exactly
     run 2  a tool that raises          → the processor seals FAILED…
     run 3  the same failing tool       → …while the hooks can only see a return
     run 4  sensitive data off          → payload absent, and RECORDED as absent

  3. Reads the ledger back and runs the offline ``verify()`` over every capsule
     — the same check a third party would run.
  4. Renders the ledger with ``capsule-emit evidence`` (fail-closed).

The point of runs 2 and 3 is the one thing a reader should take away: the two
registration surfaces see different things, and this listener says which.

  - ``OpenAIAgentsCapsuleProcessor`` is a ``TracingProcessor``, registered with
    ``add_trace_processor()``. It reads ``span.error``, so it can tell a failed
    tool call from a successful one — but the SDK assigns
    ``FunctionSpanData.input`` *after* the span starts, so its planned capsule
    commits to the tool identity, not the arguments.
  - ``OpenAIAgentsCapsuleHooks`` is a ``RunHooks``, passed to ``Runner.run``. It
    gets ``ToolContext.tool_arguments`` *before* the tool runs — but
    ``RunHooksBase`` has no ``on_tool_error``, and ``failure_error_function``
    turns a raising tool into an ordinary string, so it cannot certify success.

Neither is quietly better. Each capsule carries a note saying what its surface
could not see.

Run:
    pip install "capsule-emit[openai-agents]"
    python examples/openai-agents-listener/demo.py
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
import warnings

from capsule_emit.verification import verify_capsule as verify

# Hard fence for this demo: no checkpoint ever leaves the process.
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
# 2. Real Runner.run turns with a scripted model
# ---------------------------------------------------------------------------


def run(ledger: pathlib.Path, anchor_url: str) -> None:
    from agents import Agent, RunConfig, Runner, function_tool
    from agents.testing import ScriptedModel, assistant_message, function_call
    from agents.tracing import set_trace_processors

    from capsule_emit.adapters.openai_agents_listener import (
        OpenAIAgentsCapsuleHooks,
        OpenAIAgentsCapsuleProcessor,
    )

    @function_tool
    def get_price(symbol: str, qty: float) -> str:
        """Price a lot. Note the float — it canonicalizes and chains (#135)."""
        return f"{symbol} x{qty} = {round(qty * 10.5, 2)}"

    @function_tool
    def submit_order(po: str) -> str:
        raise ValueError("downstream ledger rejected the order")

    # This demo deliberately points every surface at ONE ledger so the two can be
    # read side by side; the listener warns about exactly that (it means two
    # chains per call). Silenced here because it is the demo's whole point.
    warnings.filterwarnings(
        "ignore", message=".*second OpenAI Agents listener.*", category=RuntimeWarning
    )

    cfg = dict(
        operator="acme-co",
        developer="purchasing-agent@v1",
        ledger=ledger,
        anchor=True,
        anchor_url=anchor_url,
        anchor_wait=10.0,  # block for the real (stubbed) confirmation
    )

    def agent(script, tools):
        return Agent(name="purchasing", model=ScriptedModel(script), tools=tools)

    def turn(*calls):
        return [
            [function_call(n, a, call_id=c) for n, a, c in calls],
            [assistant_message("done")],
        ]

    # -- run 1: two concurrent tool calls, observed by the processor ---------
    processor = OpenAIAgentsCapsuleProcessor(**cfg)
    set_trace_processors([processor])
    asyncio.run(
        Runner.run(
            agent(
                turn(
                    ("get_price", {"symbol": "ACME", "qty": 2.5}, "c1"),
                    ("get_price", {"symbol": "ZENO", "qty": 4.0}, "c2"),
                ),
                [get_price],
            ),
            "price both lots",
        )
    )
    print("run 1  two concurrent tool calls, TracingProcessor")
    print("       -> planned+confirmed x2, paired by span_id (never FIFO)")

    # -- run 2: a raising tool, observed by the processor --------------------
    asyncio.run(
        Runner.run(agent(turn(("submit_order", {"po": "PO-1"}, "e1")), [submit_order]), "order")
    )
    print("run 2  the tool raises, TracingProcessor")
    print("       -> FAILED: span.error is authoritative, the error is evidence")

    # -- run 3: the same raising tool, observed by the hooks -----------------
    set_trace_processors([])
    hooks = OpenAIAgentsCapsuleHooks(**cfg)
    asyncio.run(
        Runner.run(
            agent(turn(("submit_order", {"po": "PO-2"}, "e2")), [submit_order]),
            "order",
            hooks=hooks,
        )
    )
    print("run 3  the SAME raising tool, RunHooks")
    print("       -> CONFIRMED, because RunHooks has no on_tool_error. The capsule")
    print("          carries verdict_note saying so: it records a return, not a success.")

    # -- run 4: sensitive data off, observed by the processor ----------------
    processor2 = OpenAIAgentsCapsuleProcessor(**cfg)
    set_trace_processors([processor2])
    asyncio.run(
        Runner.run(
            agent(turn(("get_price", {"symbol": "HUSH", "qty": 1.0}, "c3")), [get_price]),
            "price quietly",
            run_config=RunConfig(trace_include_sensitive_data=False),
        )
    )
    set_trace_processors([])
    print("run 4  trace_include_sensitive_data=False, TracingProcessor")
    print("       -> the SDK never put the payload on the span; the capsule records")
    print("          payload_withheld=True. Absent is recorded as absent, not as empty.")
    print()


# ---------------------------------------------------------------------------
# 3. Offline verification + evidence render
# ---------------------------------------------------------------------------


def _marker(compute: dict) -> str:
    if compute.get("payload_withheld"):
        return " payload-withheld"
    if compute.get("args_observable") is False:
        return " args-not-yet-on-span"
    if compute.get("verdict_note"):
        return " no-error-hook"
    return ""


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
            print(
                f"  {'PASS' if v.ok else 'FAIL'}  {cap['action_id'][:30]:32s}"
                f" {status:9s} {verdict:9s} {chained}  {cap['capsule_id'][:12]}…"
                f"{_marker(compute)}"
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
