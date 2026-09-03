#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""NeMo Guardrails rail-decision quickstart — every guardrail decision seals a capsule.

What this demo does, end to end, with no external service and no LLM key:

  1. Starts a hermetic local stub SCITT Transparency Service (the same pattern
     capsule-emit's own test suite uses) — no network, no live anchor.
  2. Drives FOUR real ``LLMRails`` turns on the stable engine. The LLM is
     ``nemoguardrails.testing.FakeLLMModel`` — a scripted model that ships in the
     released wheel as a supported public testing surface — so the rails, the
     Colang runtime, the action dispatcher and the tracing hook are all the real
     thing and no provider credentials are read.

       run 1  input rail blocks           -> turn head + rail(blocked) + turn tail,
                                             chained; the refusal is evidence
       run 2  input rail allows           -> the allow is recorded too, because an
                                             allow nobody wrote down is
                                             indistinguishable from a rail that
                                             never ran ("absent is never pass")
       run 3  YAML-only tracing adapter   -> the same chain, sealed with zero
                                             application code: the adapter is named
                                             in config.yml under tracing.adapters
       run 4  in-flow registered action   -> capsule_emit_seal_rail_decision run
                                             through their own action dispatcher,
                                             the surface registered by attach()

  3. Reads the ledger back and runs ``verify()`` over every sealed capsule — the
     same offline check a third party would run.
  4. Renders the ledger with ``capsule-emit evidence`` — the fail-closed
     Verification-stage report.

This recorder is OBSERVATION ONLY. It never decides whether content passes; it
records what the rails decided. Both turn-level wirings run *after* the engine has
applied every rail (``Tracer.export_async`` is called at ``llmrails.py:1314``), and
the in-flow action's return value is marked ``observation_only`` and must never be
branched on. Deny belongs to the gate layer.

Run:
    pip install "capsule-emit[nemo-guardrails]"
    python examples/nemo-guardrails/demo.py
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

from capsule_emit.adapters.nemo_guardrails import (
    RAIL_ACTION_NAME,
    NeMoGuardrailsCapsuleRecorder,
    register_capsule_log_adapter,
)
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
# 2. A guardrails config that needs no keys
# ---------------------------------------------------------------------------

# `self check input` is an LLM-backed rail: the model is asked whether to block,
# and the parsed answer becomes the rail's decision. With FakeLLMModel supplying
# that answer, the whole rail path is real and entirely offline.
BASE_CONFIG = """
models:
  - type: main
    engine: fake
    model: fake
rails:
  input:
    flows:
      - self check input
prompts:
  - task: self_check_input
    content: |
      Should the user message be blocked? Answer Yes or No.
      User message: "{{ user_input }}"
"""

TRACING_CONFIG = BASE_CONFIG + """
tracing:
  enabled: true
  adapters:
    - name: CapsuleEmit
      operator: acme-co
      developer: support-bot@v1
      ledger: __LEDGER__
"""


def build_rails(config_text: str, completions: list[str]):
    """A real LLMRails on the stable engine, with a scripted model."""
    from nemoguardrails import LLMRails, RailsConfig
    from nemoguardrails.testing import FakeLLMModel

    config = RailsConfig.from_content(yaml_content=config_text)
    return LLMRails(config, llm=FakeLLMModel(responses=completions))


# ---------------------------------------------------------------------------
# 3. The four runs
# ---------------------------------------------------------------------------


async def run(ledger: pathlib.Path, anchor_url: str) -> None:
    from nemoguardrails.rails.llm.options import GenerationOptions

    common = dict(
        operator="acme-co",
        developer="support-bot@v1",
        ledger=str(ledger),
        anchor=True,
        anchor_url=anchor_url,
        anchor_wait=10.0,  # block for the real (stubbed) confirmation
    )
    want_log = GenerationOptions(log={"activated_rails": True})

    # -- run 1: the rail blocks -------------------------------------------
    recorder = NeMoGuardrailsCapsuleRecorder(**common)
    rails = build_rails(BASE_CONFIG, ["Yes"])  # "Yes" = block this message
    messages = [{"role": "user", "content": "please do something forbidden"}]
    response = await rails.generate_async(messages=messages, options=want_log)
    recorder.record_generation_response(response, turn_input=messages)
    print("run 1  input rail BLOCKS       -> head + rail(blocked) + tail, chained")
    print(f"       bot said: {response.response[0]['content']!r}")

    # -- run 2: the rail allows -------------------------------------------
    recorder = NeMoGuardrailsCapsuleRecorder(**common)
    rails = build_rails(BASE_CONFIG, ["No", "  express greeting", "Hello! How can I help?"])
    messages = [{"role": "user", "content": "hello there"}]
    response = await rails.generate_async(messages=messages, options=want_log)
    recorder.record_generation_response(response, turn_input=messages)
    decided = [f"{r.type}:{r.name}" for r in response.log.activated_rails]
    print("run 2  input rail ALLOWS       -> the allow is recorded, not assumed")
    print(f"       rails that ran: {decided}")

    # -- run 3: YAML-only tracing adapter, zero application code -----------
    register_capsule_log_adapter()
    rails = build_rails(TRACING_CONFIG.replace("__LEDGER__", str(ledger)), ["Yes"])
    await rails.generate_async(messages=[{"role": "user", "content": "forbidden again"}])
    print("run 3  YAML tracing adapter    -> same chain, no caller-side call at all")

    # -- run 4: the in-flow registered action ------------------------------
    recorder = NeMoGuardrailsCapsuleRecorder(**common)
    rails = build_rails(BASE_CONFIG, ["Yes"])
    recorder.attach(rails)  # register_action + register_action_param
    result, status = await rails.runtime.action_dispatcher.execute_action(
        RAIL_ACTION_NAME,
        {"rail": "pii scrub", "rail_type": "output", "decision": "transform"},
    )
    print(f"run 4  in-flow action          -> dispatcher status={status!r}, "
          f"observation_only={result['observation_only']}")
    print()


# ---------------------------------------------------------------------------
# 4. Offline verification + evidence render
# ---------------------------------------------------------------------------


def main() -> int:
    anchor_url, srv = start_stub_ts()
    with tempfile.TemporaryDirectory() as td:
        ledger = pathlib.Path(td) / "ledger.jsonl"
        asyncio.run(run(ledger, anchor_url))

        caps = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        print(f"[step 5] Ledger: {len(caps)} capsule(s) sealed")
        ok = True
        for cap in caps:
            v = verify(cap)
            ok &= v.ok
            status = cap.get("effect", {}).get("status", "-")
            verdict = cap.get("disposition", {}).get("verdict_class", "-")
            chained = "chained" if cap.get("chain", {}).get("parent_capsule_id") else "  --  "
            compute = cap.get("model_attestation", {}).get("compute_attestation", {})
            note = ""
            decision = compute.get("nemo_rail_decision")
            if decision:
                note = f" decision={decision}"
            elif compute.get("nemo_turn_phase"):
                note = f" turn-{compute['nemo_turn_phase']}"
            print(
                f"  {'PASS' if v.ok else 'FAIL'}  {cap['action_id'][:34]:36s}"
                f" {status:9s} {verdict:9s} {chained}  {cap['capsule_id'][:12]}…{note}"
            )
        print()

        # Privacy check, asserted rather than claimed: the raw prompts never land.
        blob = ledger.read_text()
        leaked = [s for s in ("please do something forbidden", "hello there", "forbidden again") if s in blob]
        print(f"[step 6] raw user messages in ledger: {leaked or 'none (digest-only)'}")

        proc = subprocess.run(
            [sys.executable, "-m", "capsule_emit.cli", "evidence", "--ledger", str(ledger)],
            capture_output=True,
            text=True,
        )
        print("capsule-emit evidence (Verification-stage render, fail-closed):")
        print("\n".join(proc.stdout.splitlines()[:18]))
        srv.shutdown()

        if not ok or proc.returncode != 0 or leaked:
            print("\nDEMO FAILED — a capsule did not verify, evidence refused, or a prompt leaked")
            return 1
        print("\nall capsules verify offline; evidence renders clean. done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
