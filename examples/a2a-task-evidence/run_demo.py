#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the full a2a-task-evidence reference flow end to end and write the
positive vector to ``vectors/positive_vector.json``.

Independent proposal; not an official A2A extension. Run:

    pip install "capsule-emit[examples]" a2a-sdk fastapi uvicorn
    python examples/a2a-task-evidence/run_demo.py

Everything here is offline and deterministic: the "Transparency Service" is
a hermetic loopback double (``local_ts.py``), not the public witness -- see
that module's docstring and ``docs/a2a-extension/README.md`` for why a
receipt from it must never be presented as independently witnessed.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from client import run_flow  # noqa: E402
from evidence_extension import EXTENSION_URI  # noqa: E402
from local_ts import start_local_ts  # noqa: E402
from server import build_app  # noqa: E402
from verifier import ObservedTaskContext, TrustConfig, verify_evidence_bundle  # noqa: E402


def _sep(title: str) -> None:
    print("\n" + "=" * 72 + f"\n  {title}\n" + "=" * 72)


class _ServerThread:
    def __init__(self, app, host: str, port: int) -> None:
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        while not self._server.started:
            time.sleep(0.02)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


async def _main_async(ledger_path: str, ts_url: str, ts_pubkey_pem: bytes, port: int) -> dict:
    app = build_app(ledger_path=ledger_path, ts_url=ts_url)
    srv = _ServerThread(app, "127.0.0.1", port)
    srv.start()
    endpoint = f"http://127.0.0.1:{port}"
    try:
        _sep("A2A flow — client activates a2a-task-evidence/v1, requirement=REQUIRED")
        async with httpx.AsyncClient(timeout=60) as hc:
            result = await run_flow(endpoint, httpx_client=hc)

        print("task_id           :", result.task_id)
        ref = result.task_evidence_reference
        print("evidenceStatus    :", ref.get("evidenceStatus"))
        print("evidenceId        :", ref.get("evidenceId"))
        print("coreManifestDigest:", ref.get("coreManifestDigest"))

        _sep("Offline verification of the returned bundle (§13 properties)")
        bundle = result.evidence_bundle
        assert bundle is not None, "no evidence bundle artifact on the terminal Task"

        payload = bundle["taskEvidencePayload"]
        observed = ObservedTaskContext(
            authority=payload["authority"],
            task_id=payload["taskId"],
            context_id=payload["contextId"],
            terminal_state=payload["terminalState"],
            non_evidence_artifact_ids=frozenset(
                a["artifactId"] for a in payload["nonEvidenceArtifacts"]
            ),
        )
        # Trust configuration is the VERIFIER's own, never read from the
        # bundle (§11). The demo pins the local TS's key (obtained out of
        # band, the way an operator would configure a real deployment) and
        # authorizes the one signer key_id the reference server used.
        trust = TrustConfig(
            ts_pubkeys={ts_url: ts_pubkey_pem},
            authorized_signers={payload["authority"]: frozenset({bundle["capsule"]["key_id"]})},
        )
        verification = verify_evidence_bundle(bundle, observed_task=observed, trust=trust)
        for name, prop in verification.to_dict().items():
            print(f"  {name:26s} {prop['state']:12s} {prop['detail']}")

        return {
            "classification": "reference-demo (loopback A2A server + hermetic local-ts, non-independent)",
            "endpoint": f"{endpoint}/a2a",
            "extensionUri": EXTENSION_URI,
            "firstTask": result.first_task_dict,
            "finalTask": result.final_task_dict,
            "evidenceBundle": bundle,
            "verification": verification.to_dict(),
            "trustConfig": {
                "tsPubkeysB64": {url: __import__("base64").b64encode(pem).decode() for url, pem in trust.ts_pubkeys.items()},
                "authorizedSigners": {k: sorted(v) for k, v in trust.authorized_signers.items()},
            },
        }
    finally:
        srv.stop()


def main() -> None:
    ledger_path = str(Path(tempfile.mkdtemp(prefix="a2a-task-evidence-")) / "ledger.jsonl")
    ts = start_local_ts()
    try:
        vector = asyncio.run(_main_async(ledger_path, ts.url, ts.public_key_pem, port=8099))
    finally:
        ts.stop()

    out_path = _ROOT / "vectors" / "positive_vector.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(vector, indent=2, sort_keys=True) + "\n")
    _sep(f"Wrote positive vector -> {out_path}")


if __name__ == "__main__":
    main()
