# SPDX-License-Identifier: Apache-2.0
"""A2A client-side reference flow for the ``a2a-task-evidence`` extension.

Activates the extension, sends a ``REQUIRED`` evidence requirement,
verifies the server's explicit acceptance on the FIRST returned Task (§7,
§8), polls to a terminal state, retrieves the evidence Artifact via
``GetTask``, and returns everything a verifier needs.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
from a2a.client import ClientConfig, create_client  # noqa: E402
from a2a.types import a2a_pb2 as pb  # noqa: E402
from evidence_extension import EXTENSION_URI, RECORD_PROFILE_URI  # noqa: E402
from google.protobuf.json_format import MessageToDict, ParseDict  # noqa: E402


class ExtensionRejectedError(RuntimeError):
    """The server did not carry explicit ``evidenceAcceptance`` on the
    first Task even though the requirement was ``REQUIRED`` (§7: "the
    client must not send a consequential REQUIRED request unless the
    current Agent Card advertises the exact URI ... The client must still
    verify explicit acceptance in the response")."""


@dataclass
class TaskEvidenceFlowResult:
    task_id: str
    first_task_dict: dict[str, Any]
    final_task_dict: dict[str, Any]
    evidence_bundle: dict[str, Any] | None
    task_evidence_reference: dict[str, Any] | None


async def run_flow(
    endpoint: str,
    *,
    minimum_evidence: str = "EXTERNALLY_REGISTERED",
    requirement: str = "REQUIRED",
    text: str = "seal this exchange",
    httpx_client: httpx.AsyncClient | None = None,
) -> TaskEvidenceFlowResult:
    owns_client = httpx_client is None
    hc = httpx_client or httpx.AsyncClient(timeout=60)
    try:
        cfg = ClientConfig(
            streaming=False,
            polling=True,
            httpx_client=hc,
            supported_protocol_bindings=["JSONRPC"],
        )
        client = await create_client(endpoint, client_config=cfg)

        msg = pb.Message(
            message_id="msg-task-evidence-001",
            role=pb.Role.ROLE_USER,
            parts=[pb.Part(text=text)],
            extensions=[EXTENSION_URI],
        )
        ParseDict(
            {
                EXTENSION_URI: {
                    "requirement": requirement,
                    "recordProfile": RECORD_PROFILE_URI,
                    "minimumEvidence": minimum_evidence,
                    "deliveryModes": ["EVIDENCE_ARTIFACT"],
                    "maximumRegistrationDelaySeconds": 60,
                    "requireTask": True,
                }
            },
            msg.metadata,
        )
        req = pb.SendMessageRequest(message=msg)

        first_task = None
        async for resp in client.send_message(req):
            t = getattr(resp, "task", None)
            if t is not None and t.id:
                first_task = t
                break
        if first_task is None:
            raise ExtensionRejectedError("server returned a direct Message, not a Task (§7 requireTask)")

        first_task_dict = MessageToDict(first_task)
        acceptance = first_task_dict.get("metadata", {}).get(EXTENSION_URI, {}).get("evidenceAcceptance")
        if requirement == "REQUIRED" and (not acceptance or acceptance.get("state") != "ACCEPTED"):
            raise ExtensionRejectedError(
                f"requirement=REQUIRED but first Task carries no ACCEPTED evidenceAcceptance: {acceptance!r}"
            )

        task_id = first_task.id
        final_task = None
        for _ in range(50):
            ft = await client.get_task(pb.GetTaskRequest(id=task_id))
            if ft.status.state in (pb.TaskState.TASK_STATE_COMPLETED, pb.TaskState.TASK_STATE_FAILED):
                final_task = ft
                break
            await asyncio.sleep(0.1)
        if final_task is None:
            raise TimeoutError(f"task {task_id} did not reach a terminal state")

        final_task_dict = MessageToDict(final_task)
        ext_field = final_task_dict.get("metadata", {}).get(EXTENSION_URI, {})
        evidence_status = ext_field.get("evidenceStatus")
        assert evidence_status and evidence_status != "EVIDENCE_STATUS_PENDING", (
            f"terminal Task must never carry EVIDENCE_STATUS_PENDING: {evidence_status!r}"
        )

        evidence_bundle = None
        for artifact in final_task_dict.get("artifacts", []):
            if EXTENSION_URI in artifact.get("extensions", []):
                for part in artifact.get("parts", []):
                    raw_b64 = part.get("raw")
                    if raw_b64:
                        import base64
                        import json

                        evidence_bundle = json.loads(base64.b64decode(raw_b64))
                break

        return TaskEvidenceFlowResult(
            task_id=task_id,
            first_task_dict=first_task_dict,
            final_task_dict=final_task_dict,
            evidence_bundle=evidence_bundle,
            task_evidence_reference=ext_field or None,
        )
    finally:
        if owns_client:
            await hc.aclose()
