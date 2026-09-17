#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Google ADK adapter quickstart — both wiring paths, a refusal in the record,
a raise in the record, offline verify.

What this demo does, end to end, with no LLM key and no live service:

  1. Path 1 (tool callbacks): drives ``ADKCapsuleEmitter.after_tool_callback``
     with a real ``google.adk.tools.FunctionTool`` — one ``executed`` capsule per
     completed call; a declared effect on the consequential tool only.
  2. The guard: ``emitter.guard(policy)`` is a ready ``before_tool_callback``.
     When the policy declines, it seals a ``blocked`` capsule and returns ADK's
     short-circuit dict, so the tool never runs — the refusal is in the record.
  3. A raise: ADK's after-callback never fires for a tool that raises, so an
     ``on_tool_error_callback`` routes it to ``emitter.emit_errored`` and returns
     ``None`` (a recovery dict would make ADK run the after-callback as well).
  4. Path 2 (event stream): feeds real ``google.adk.events.Event`` objects —
     function-call parts and function-response parts, out of order, the way a
     ``ParallelAgent`` interleaves them — through ``emitter.tap_event``; pairing
     is by function-call id.
  5. Reads the ledger back and verifies every capsule offline, then renders it
     with ``capsule-emit evidence`` (fail-closed).

Run:
    pip install "capsule-emit[adk]"
    python examples/adk-capsule/demo.py
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

os.environ.setdefault("CAPSULE_WITNESS", "off")  # zero egress for the demo; see docs "Network behavior"

from google.adk.events import Event  # noqa: E402
from google.adk.tools import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402

from capsule_emit.adapters.adk import ADKCapsuleEmitter  # noqa: E402
from capsule_emit.verification import verify_capsule as verify  # noqa: E402


def get_price(sku: str) -> dict:
    """Look up a unit price (read-only)."""
    return {"sku": sku, "unit_price_usd": "12.00"}


def write_order(vendor: str, total_usd: str) -> dict:
    """Place a purchase order (consequential)."""
    return {"po": "PO-7777", "vendor": vendor, "total_usd": total_usd}


def run(ledger: pathlib.Path) -> None:
    emitter = ADKCapsuleEmitter(
        operator="acme-co",
        developer="po-agent@v1",
        ledger=str(ledger),
        anchor=False,
        # declare the effect ONCE for the consequential tool; read-only tools stay effect-free
        effects={"write_order": {"type": "write_order", "status": "dispatched"}},
    )
    price_tool, order_tool = FunctionTool(get_price), FunctionTool(write_order)

    # -- Path 1: tool callbacks (what LlmAgent(after_tool_callback=...) calls) --
    args = {"sku": "SKU-9"}
    emitter.after_tool_callback(price_tool, args, None, get_price(**args))
    print("get_price    -> executed (read-only: no effect asserted)")

    args = {"vendor": "Frobozz Supply", "total_usd": "640.00"}
    emitter.after_tool_callback(order_tool, args, None, write_order(**args))
    print("write_order  -> executed, effect dispatched (declared effect)")

    # -- The guard: policy is yours; the record of the refusal is the adapter's --
    before = emitter.guard(
        lambda name, a: not (name == "write_order" and float(a.get("total_usd", 0)) > 1000),
        reason="over spend limit",
    )
    args = {"vendor": "Frobozz Supply", "total_usd": "1240.19"}
    short_circuit = before(order_tool, args, None)
    print(f"write_order  -> BLOCKED by policy, tool did not run; ADK gets {short_circuit}")

    # -- A raise: the after-callback never fires, so ADK's on_tool_error_callback
    #    (signature: tool, args, tool_context, error) routes it to emit_errored.
    #    Return None: a recovery dict would make ADK run the after-callback too.
    def on_error(tool, args, tool_context, error):
        emitter.emit_errored(tool, args, error, tool_context)
        return None

    # LlmAgent(..., on_tool_error_callback=on_error) — here the demo dispatches it
    # the way ADK does, with the raise the tool produced.
    try:
        raise RuntimeError("order gateway down")
    except RuntimeError as exc:
        on_error(order_tool, {"vendor": "Frobozz Supply", "total_usd": "5.00"}, None, exc)
        print(f"write_order  -> raised {exc!r}; sealed via on_tool_error_callback -> emit_errored (executed, no effect, error as output)")

    # -- Path 2: the Runner event stream, real Event objects, out of order --
    def call(cid, name, a):
        return Event(author="po-agent", content=types.Content(
            role="model", parts=[types.Part(function_call=types.FunctionCall(id=cid, name=name, args=a))]))

    def response(cid, name, r):
        return Event(author="po-agent", content=types.Content(
            role="user", parts=[types.Part(function_response=types.FunctionResponse(id=cid, name=name, response=r))]))

    stream = [
        call("c-1", "get_price", {"sku": "SKU-1"}),
        call("c-2", "write_order", {"vendor": "Zork Ltd", "total_usd": "99.00"}),
        response("c-2", "write_order", {"po": "PO-7778"}),   # arrives before c-1's response
        response("c-1", "get_price", {"unit_price_usd": "3.50"}),
    ]
    emitter.tap_stream(stream)
    print("event tap    -> 2 calls paired by function-call id despite out-of-order responses")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        ledger = pathlib.Path(td) / "ledger.jsonl"
        run(ledger)

        caps = [json.loads(line) for line in ledger.read_text().splitlines()]
        print(f"\nsealed {len(caps)} capsules:")
        ok = True
        for cap in caps:
            v = verify(cap)
            ok &= v.ok
            compute = cap.get("model_attestation", {}).get("compute_attestation", {})
            effect = (cap.get("effect") or {}).get("status", "-")
            print(
                f"  {'PASS' if v.ok else 'FAIL'}  {cap['action_id'][:30]:32s} "
                f"{cap['disposition']['verdict_class']:9s} effect={effect:11s} "
                f"observed={compute.get('observation_mode', '-'):13s} {cap['capsule_id'][:12]}…"
            )

        proc = subprocess.run(
            [sys.executable, "-m", "capsule_emit.cli", "evidence", "--ledger", str(ledger)],
            capture_output=True, text=True,
        )
        print("\ncapsule-emit evidence (Verification-stage render, fail-closed):")
        print("\n".join(proc.stdout.splitlines()[:16]))
        if not ok or proc.returncode != 0:
            print("\nDEMO FAILED — a capsule did not verify or evidence refused to render")
            return 1
        print("\nall capsules verify offline; evidence renders clean. done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
