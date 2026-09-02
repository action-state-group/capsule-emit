#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""LiteLLM listener quickstart — out-of-tree, loaded the way the proxy loads it.

What this demo does, end to end, with no external service, no network and no
LLM key:

  1. Starts a hermetic local stub SCITT Transparency Service (the same pattern
     capsule-emit's own test suite uses) — no live anchor.
  2. Loads the listener through **litellm's own config-callback path**: the same
     ``get_instance_fn`` + ``initialize_callbacks_on_proxy`` that
     ``litellm_settings.callbacks: [...]`` in a ``config.yaml`` goes through.
     Nothing is forked, monkeypatched, or registered by a private API — the
     dotted string below is the whole integration:

         litellm_settings:
           callbacks: ["capsule_emit.adapters.litellm_listener.proxy_handler_instance"]

  3. Drives REAL ``litellm.acompletion`` calls with ``mock_response``, which is
     litellm's own keyless test path — every layer under test is the real SDK;
     only the wire is scripted:

       call 1  plain completion              → planned + confirmed (chained)
       call 2  behind a redaction callback   → planned + confirmed, and the
                                               digest commits to the REDACTED
                                               prompt, not the raw one
       call 3  upstream raises               → the proxy failure hook seals
                                               planned + failed (chained), with
                                               the prompt WITHHELD and the
                                               reason stamped

  4. Reads the ledger back and verifies every capsule offline — the same check a
     third party would run.
  5. Renders the ledger with ``capsule-emit evidence``.

Run:
    pip install "capsule-emit[litellm]"
    python examples/litellm-listener/demo.py
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

os.environ.setdefault("LITELLM_LOG", "ERROR")

from capsule_emit.verification import verify_capsule as verify  # noqa: E402

DOTTED = "capsule_emit.adapters.litellm_listener.proxy_handler_instance"

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
            self._send_json(
                200,
                {
                    "receipt_b64": base64.b64encode(b"stub-receipt-not-a-real-cose-receipt").decode(),
                    "entry_hash": hashlib.sha256(statement_bytes).hexdigest(),
                },
            )
        else:
            self.send_response(404)
            self.end_headers()


def start_stub_ts() -> tuple[str, http.server.ThreadingHTTPServer]:
    handler = type("_Bound", (_StubTSHandler,), {"pubkey_hex": os.urandom(32).hex()})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


# ---------------------------------------------------------------------------
# 2. Load through litellm's own config-callback path
# ---------------------------------------------------------------------------


def load_like_the_proxy(config_path: pathlib.Path):
    """Resolve DOTTED exactly as ``litellm_settings.callbacks`` does."""
    import litellm
    from litellm.proxy.types_utils.utils import get_instance_fn

    try:
        from litellm.proxy.common_utils.callback_utils import initialize_callbacks_on_proxy

        litellm.callbacks = []
        initialize_callbacks_on_proxy(
            value=[DOTTED],
            premium_user=False,
            config_file_path=str(config_path),
            litellm_settings={},
        )
        listener = next(
            c for c in litellm.callbacks if type(c).__name__ == "LiteLLMCapsuleListener"
        )
        print(f"loaded via initialize_callbacks_on_proxy  <- {DOTTED}")
        return listener
    except Exception as exc:  # litellm[proxy] extras not installed
        print(f"(litellm[proxy] unavailable: {type(exc).__name__}) — using get_instance_fn directly")
        listener = get_instance_fn(DOTTED, str(config_path))
        print(f"loaded via get_instance_fn               <- {DOTTED}")
        return listener


# ---------------------------------------------------------------------------
# 3. Real, keyless litellm calls
# ---------------------------------------------------------------------------

SECRET_PROMPT = "refund order 8817, my card ends 4242"


def drive(ledger: pathlib.Path, config_path: pathlib.Path) -> None:
    import litellm
    from litellm.integrations.custom_logger import CustomLogger

    listener = load_like_the_proxy(config_path)

    class Redactor(CustomLogger):
        """Stands in for the operator's PII redaction (presidio-shaped)."""

        async def async_logging_hook(self, kwargs, result, call_type):
            new_kwargs = dict(kwargs)
            new_kwargs["messages"] = [{"role": "user", "content": "[REDACTED]"}]
            return new_kwargs, result

    async def call(callbacks, prompt, mock):
        litellm.callbacks = list(callbacks)
        litellm.success_callback = []
        litellm._async_success_callback = []
        out = await litellm.acompletion(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            mock_response=mock,
        )
        await asyncio.sleep(1.5)  # litellm schedules the success handler
        return out

    async def script():
        r1 = await call([listener], "what is our refund window?", "30 days")
        print(f"call 1  plain completion       -> caller got: {r1.choices[0].message.content!r}")

        r2 = await call([Redactor(), listener], SECRET_PROMPT, "refund issued")
        print(f"call 2  behind a redactor      -> caller got: {r2.choices[0].message.content!r}")

        # call 3: the proxy failure hook, driven directly — this is the seam
        # ProxyLogging.post_call_failure_hook dispatches on an upstream error.
        returned = await listener.async_post_call_failure_hook(
            request_data={
                "model": "gpt-3.5-turbo",
                "call_type": "acompletion",
                "messages": [{"role": "user", "content": SECRET_PROMPT}],
                "litellm_call_id": "demo-call-3",
            },
            original_exception=RuntimeError("upstream provider returned 503"),
            user_api_key_dict=None,
            traceback_str="Traceback (most recent call last): ...",
        )
        print(
            f"call 3  upstream 503           -> failure hook returned {returned!r} "
            "(never rewrites the client's error)"
        )

    asyncio.run(script())
    print()


# ---------------------------------------------------------------------------
# 4/5. Offline verification + evidence render
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        import litellm  # noqa: F401
    except ImportError:
        print('this demo needs litellm: pip install "capsule-emit[litellm]"')
        return 1

    anchor_url, srv = start_stub_ts()
    with tempfile.TemporaryDirectory() as td:
        ledger = pathlib.Path(td) / "ledger.jsonl"
        config_path = pathlib.Path(td) / "config.yaml"
        config_path.write_text(
            "litellm_settings:\n"
            f'  callbacks: ["{DOTTED}"]\n'
        )
        os.environ["CAPSULE_EMIT_OPERATOR"] = "acme-co"
        os.environ["CAPSULE_EMIT_DEVELOPER"] = "support-gateway@v1"
        os.environ["CAPSULE_EMIT_LEDGER"] = str(ledger)
        os.environ["AAC_ANCHOR_URL"] = anchor_url
        os.environ["CAPSULE_ANCHOR"] = "legacy-on"

        drive(ledger, config_path)

        caps = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        print(f"sealed {len(caps)} capsules:")
        ok = True
        digests = {}
        for cap in caps:
            v = verify(cap)
            ok &= v.ok
            status = cap.get("effect", {}).get("status", "-")
            chained = "chained" if (cap.get("chain") or {}).get("parent_capsule_id") else "  --  "
            compute = cap.get("model_attestation", {}).get("compute_attestation", {})
            note = ""
            if compute.get("request_payload_withheld"):
                note = " prompt-withheld"
            elif compute.get("request_record_provenance"):
                note = " post-hoc"
            if compute.get("agent_input_digest"):
                digests.setdefault(compute["agent_input_digest"], []).append(status)
            print(
                f"  {'PASS' if v.ok else 'FAIL'}  {cap['action_id'][:32]:34s}"
                f" {status:9s} {chained}  {cap['capsule_id'][:12]}…{note}"
            )

        # the redaction claim, shown rather than asserted: the raw prompt never
        # appears in the ledger, and call 2's request digest is not call 1's.
        raw_present = SECRET_PROMPT in ledger.read_text()
        print(f"\nraw prompt text present anywhere in the ledger: {raw_present}")
        print(f"distinct request digests: {len(digests)} (each call commits to its own preimage)")

        proc = subprocess.run(
            [sys.executable, "-m", "capsule_emit.cli", "evidence", "--ledger", str(ledger)],
            capture_output=True,
            text=True,
        )
        print("\ncapsule-emit evidence (Verification-stage render, fail-closed):")
        print("\n".join(proc.stdout.splitlines()[:16]))
        srv.shutdown()

        if not ok or raw_present or proc.returncode != 0:
            print("\nDEMO FAILED — a capsule did not verify, evidence refused, or text leaked")
            return 1
        print("\nall capsules verify offline; no prompt text left the process. done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
