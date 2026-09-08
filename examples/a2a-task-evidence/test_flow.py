# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for the a2a-task-evidence reference implementation.

NOT collected by the repo's main ``pytest`` run (``testpaths = ["tests"]``
in ``pyproject.toml``) -- this example depends on ``a2a-sdk``, which is
pinned to a specific ``protobuf`` version incompatible with the rest of the
repo's dependency set (see ``requirements.txt``), so it is intentionally
kept out of the shared CI dependency surface, matching
``examples/a2a-ap2/boundary-seal``'s existing precedent (its own pinned
``requirements.txt``, no top-level ``tests/`` entry).

Run in the pinned venv (see ``README.md``):

    python -m venv .venv && source .venv/bin/activate
    pip install -r examples/a2a-task-evidence/requirements.txt -e .
    pytest examples/a2a-task-evidence/test_flow.py -v
"""
from __future__ import annotations

import asyncio
import base64
import copy
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from client import run_flow  # noqa: E402
from local_ts import start_local_ts  # noqa: E402
from server import build_app  # noqa: E402
from verifier import ObservedTaskContext, TrustConfig, verify_evidence_bundle  # noqa: E402


class _ServerThread:
    def __init__(self, app, host: str, port: int) -> None:
        self._server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        while not self._server.started:
            time.sleep(0.02)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def running_flow(tmp_path):
    """Starts a local TS double + the reference A2A server, runs the client
    flow once, and yields (endpoint, ledger_path, ts) for tests that want to
    drive additional requests against the same server."""
    ledger_path = str(tmp_path / "ledger.jsonl")
    ts = start_local_ts()
    app = build_app(ledger_path=ledger_path, ts_url=ts.url)
    srv = _ServerThread(app, "127.0.0.1", 0)
    # Port 0 means the OS picks one; recover it from the underlying socket.
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    srv._server.config.port = port
    srv.start()
    endpoint = f"http://127.0.0.1:{port}"
    try:
        yield endpoint, ledger_path, ts
    finally:
        srv.stop()
        ts.stop()


def _trust_and_observed(bundle: dict, ts_url: str, ts_pubkey_pem: bytes, final_task: dict):
    payload = bundle["taskEvidencePayload"]
    trust = TrustConfig(
        ts_pubkeys={ts_url: ts_pubkey_pem},
        authorized_signers={payload["authority"]: frozenset({bundle["capsule"]["key_id"]})},
    )
    observed = ObservedTaskContext(
        authority=payload["authority"],
        task_id=final_task["id"],
        context_id=final_task["contextId"],
        terminal_state=final_task["status"]["state"],
        non_evidence_artifact_ids=frozenset(a["artifactId"] for a in payload["nonEvidenceArtifacts"]),
    )
    return trust, observed


def test_positive_flow_all_ten_properties_reported(running_flow):
    endpoint, _ledger_path, ts = running_flow

    async def _run():
        async with httpx.AsyncClient(timeout=60) as hc:
            return await run_flow(endpoint, httpx_client=hc)

    result = asyncio.run(_run())

    assert result.task_evidence_reference["evidenceStatus"] == "EVIDENCE_STATUS_EXTERNALLY_REGISTERED"
    bundle = result.evidence_bundle
    assert bundle is not None

    trust, observed = _trust_and_observed(bundle, ts.url, ts.public_key_pem, result.final_task_dict)
    verification = verify_evidence_bundle(bundle, observed_task=observed, trust=trust)
    props = verification.to_dict()

    from verifier import PROPERTIES

    assert set(props) == set(PROPERTIES), "verifier must report all ten §13 properties, never fewer"
    assert "trusted" not in verification.to_dict(), "no aggregate trusted field may ever appear (§13)"

    expect_pass = {
        "contentBinding",
        "producerSignature",
        "taskBinding",
        "localInclusion",
        "checkpointSignature",
        "externalRegistration",
        "identityAuthorityBinding",
    }
    for name in expect_pass:
        assert props[name]["state"] == "PASS", f"{name}: {props[name]}"

    # §13: "A valid bundle can have PASS for content and signatures while
    # identity, capture coverage, or outcome corroboration remains
    # unestablished" -- this base profile deliberately never claims PASS
    # for these three, even on an otherwise fully-passing bundle.
    assert props["continuity"]["state"] == "NOT_PRESENT"
    assert props["captureCoverage"]["state"] == "INCONCLUSIVE"
    assert props["outcomeCorroboration"]["state"] == "NOT_PRESENT"


def test_terminal_task_never_carries_pending_status(running_flow):
    endpoint, _ledger_path, _ts = running_flow

    async def _run():
        async with httpx.AsyncClient(timeout=60) as hc:
            return await run_flow(endpoint, httpx_client=hc)

    result = asyncio.run(_run())
    ext_uri = "https://agentactioncapsule.org/extensions/a2a-task-evidence/v1"
    status = result.final_task_dict["metadata"][ext_uri]["evidenceStatus"]
    assert status != "EVIDENCE_STATUS_PENDING"


def test_first_task_carries_explicit_acceptance(running_flow):
    endpoint, _ledger_path, _ts = running_flow

    async def _run():
        async with httpx.AsyncClient(timeout=60) as hc:
            return await run_flow(endpoint, httpx_client=hc)

    result = asyncio.run(_run())
    ext_uri = "https://agentactioncapsule.org/extensions/a2a-task-evidence/v1"
    acceptance = result.first_task_dict["metadata"][ext_uri]["evidenceAcceptance"]
    assert acceptance["state"] == "ACCEPTED"


def test_tamper_flips_contentBinding_and_only_contentBinding(running_flow):
    """The tamper-negative vector's core property, run live (not from the
    committed fixture) so this test fails if the isolation regresses."""
    endpoint, _ledger_path, ts = running_flow

    async def _run():
        async with httpx.AsyncClient(timeout=60) as hc:
            return await run_flow(endpoint, httpx_client=hc)

    result = asyncio.run(_run())
    bundle = result.evidence_bundle
    trust, observed = _trust_and_observed(bundle, ts.url, ts.public_key_pem, result.final_task_dict)

    baseline = verify_evidence_bundle(bundle, observed_task=observed, trust=trust).to_dict()
    assert baseline["contentBinding"]["state"] == "PASS"

    tampered = copy.deepcopy(bundle)
    original = tampered["taskEvidencePayload"]["transcript"][0]["partCommitment"]
    tampered["taskEvidencePayload"]["transcript"][0]["partCommitment"] = (
        ("1" if original[0] == "0" else "0") + original[1:]
    )

    mutated = verify_evidence_bundle(tampered, observed_task=observed, trust=trust).to_dict()
    assert mutated["contentBinding"]["state"] == "FAIL", "mutant check: tamper must flip contentBinding to FAIL"
    for name in ("producerSignature", "localInclusion", "checkpointSignature", "externalRegistration"):
        assert mutated[name]["state"] == baseline[name]["state"], (
            f"{name} must be unaffected by a contentBinding-only tamper"
        )


def test_committed_vectors_still_verify_as_recorded():
    """Regression guard: the committed vectors/*.json must reverify to
    exactly the recorded properties -- catches silent drift if the verifier
    or the underlying capsule-emit/cll primitives change behavior."""
    import json

    for name, expect_content_binding in (("positive_vector.json", "PASS"), ("tamper_negative_vector.json", "FAIL")):
        vector = json.loads((_ROOT / "vectors" / name).read_text())
        bundle = vector["evidenceBundle"]
        recorded = vector["verification"]

        if name == "positive_vector.json":
            trust_cfg = vector["trustConfig"]
            final_task = vector["finalTask"]
        else:
            positive = json.loads((_ROOT / "vectors" / "positive_vector.json").read_text())
            trust_cfg = positive["trustConfig"]
            final_task = positive["finalTask"]

        trust = TrustConfig(
            ts_pubkeys={u: base64.b64decode(b) for u, b in trust_cfg["tsPubkeysB64"].items()},
            authorized_signers={k: frozenset(v) for k, v in trust_cfg["authorizedSigners"].items()},
        )
        payload = bundle["taskEvidencePayload"]
        observed = ObservedTaskContext(
            authority=payload["authority"],
            task_id=final_task["id"],
            context_id=final_task["contextId"],
            terminal_state=final_task["status"]["state"],
            non_evidence_artifact_ids=frozenset(a["artifactId"] for a in payload["nonEvidenceArtifacts"]),
        )
        actual = verify_evidence_bundle(bundle, observed_task=observed, trust=trust).to_dict()
        assert actual == recorded, f"{name}: verification drifted from the committed record"
        assert actual["contentBinding"]["state"] == expect_content_binding


def test_unsupported_extension_request_is_observable():
    """§7: an extension-unaware/non-activating server must not silently
    upgrade a plain SendMessage into an evidenced one -- verifies the
    reference server's own honesty when the requirement is absent."""
    ledger_path = tempfile.mkdtemp() + "/ledger.jsonl"
    ts = start_local_ts()
    app = build_app(ledger_path=ledger_path, ts_url=ts.url)
    srv = _ServerThread(app, "127.0.0.1", 0)
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    srv._server.config.port = port
    srv.start()
    try:
        async def _run():
            from a2a.client import ClientConfig, create_client
            from a2a.types import a2a_pb2 as pb

            async with httpx.AsyncClient(timeout=30) as hc:
                cfg = ClientConfig(streaming=False, polling=True, httpx_client=hc, supported_protocol_bindings=["JSONRPC"])
                client = await create_client(f"http://127.0.0.1:{port}", client_config=cfg)
                msg = pb.Message(message_id="m-plain", role=pb.Role.ROLE_USER, parts=[pb.Part(text="hi")])
                async for resp in client.send_message(pb.SendMessageRequest(message=msg)):
                    t = getattr(resp, "task", None)
                    if t is not None and t.id:
                        return t
            return None

        task = asyncio.run(_run())
        assert task is not None
        from google.protobuf.json_format import MessageToDict

        task_dict = MessageToDict(task)
        ext_uri = "https://agentactioncapsule.org/extensions/a2a-task-evidence/v1"
        assert ext_uri not in task_dict.get("metadata", {}), (
            "server must not attach evidence extension data when the client never activated it"
        )
    finally:
        srv.stop()
        ts.stop()
