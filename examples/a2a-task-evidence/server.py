# SPDX-License-Identifier: Apache-2.0
"""A2A server-side reference implementation of the ``a2a-task-evidence``
extension (independent proposal; not an official A2A extension).

Models ``examples/a2a-ap2/boundary-seal/server/serve_boundary_agent.py``'s
FastAPI + ``AgentExecutor`` shape, but implements the negotiation and
recording steps from the v3.1 discussion draft instead of the older
per-record anchor pattern:

- advertises the extension URI in the Agent Card (§6);
- reads the client's request-time requirement from the initiating
  Message's namespaced metadata (§7);
- the FIRST returned Task carries an explicit ``evidenceAcceptance`` (§8);
- on completion, freezes the Task Evidence payload (§12), seals + registers
  it (§10), and attaches the terminal ``TaskEvidenceReference`` plus the
  immutable bundle as an Artifact (§9).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

import uvicorn  # noqa: E402
from a2a.server.agent_execution import AgentExecutor  # noqa: E402
from a2a.server.request_handlers import DefaultRequestHandler  # noqa: E402
from a2a.server.routes import (  # noqa: E402
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater  # noqa: E402
from a2a.types import a2a_pb2 as pb  # noqa: E402
from evidence_extension import (  # noqa: E402
    EVIDENCE_STATUS_FAILED,
    EXTENSION_URI,
    RECORD_PROFILE_URI,
    build_evidence_artifact_bundle,
    build_task_evidence_payload,
    seal_task_evidence,
)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from google.protobuf.json_format import MessageToDict, ParseDict  # noqa: E402

MEDIA_TYPE = "application/vnd.agentactioncapsule.a2a-task-evidence+json"
CAPTURE_POLICY_ID = "a2a-server-boundary-v1"
SERVER_AUTHORITY = "reference-implementation-server"

_ROLE_NAME = {0: "ROLE_UNSPECIFIED", 1: "ROLE_USER", 2: "ROLE_AGENT"}


def build_agent_card(public_url: str) -> pb.AgentCard:
    card_dict = {
        "name": "A2A Task Evidence Reference Agent",
        "description": (
            "Independent reference implementation of the a2a-task-evidence "
            "extension (v3.1 discussion draft, not an official A2A "
            "extension). Every terminal Task for which the extension is "
            "activated carries a TaskEvidenceReference and an immutable "
            "evidence bundle."
        ),
        "version": "0.1.0",
        "supportedInterfaces": [
            {"url": f"{public_url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [
                {
                    "uri": EXTENSION_URI,
                    "required": False,
                    "description": "Portable evidence for terminal A2A Tasks (independent proposal).",
                    "params": {
                        "recordProfiles": [RECORD_PROFILE_URI],
                        "deliveryModes": ["EVIDENCE_ARTIFACT"],
                        "minimumEvidenceLevels": [
                            "SIGNED_RECORD",
                            "EXTERNALLY_REGISTERED",
                        ],
                    },
                }
            ],
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "task-evidence-echo",
                "name": "Task Evidence Echo",
                "description": "Echoes the request text and seals a portable evidence bundle for the exchange.",
                "tags": ["a2a", "evidence", "capsule", "reference"],
            }
        ],
        "provider": {"organization": "action-state-group", "url": "https://agentactioncapsule.org"},
        "documentationUrl": (
            "https://github.com/action-state-group/capsule-emit/tree/main/examples/a2a-task-evidence"
        ),
    }
    card = pb.AgentCard()
    ParseDict(card_dict, card)
    return card


class TaskEvidenceExecutor(AgentExecutor):
    """Negotiates and records task evidence per the v3.1 draft."""

    def __init__(self, *, ledger_path: str, ts_url: str) -> None:
        self._ledger_path = ledger_path
        self._ts_url = ts_url

    async def execute(self, context, event_queue) -> None:  # noqa: ANN001
        from a2a.helpers.proto_helpers import new_task_from_user_message

        msg = context.message
        activated = EXTENSION_URI in list(msg.extensions)
        requirement_dict = MessageToDict(msg.metadata).get(EXTENSION_URI, {}) if activated else {}
        requirement = requirement_dict.get("requirement")
        minimum_evidence = requirement_dict.get("minimumEvidence", "SIGNED_RECORD")

        is_new_task = context.current_task is None
        task = context.current_task or new_task_from_user_message(msg)

        if is_new_task and activated and requirement:
            acceptance = {
                "evidenceAcceptance": {
                    "state": "ACCEPTED",
                    "recordProfile": RECORD_PROFILE_URI,
                    "minimumEvidence": minimum_evidence,
                    "maximumRegistrationDelaySeconds": requirement_dict.get(
                        "maximumRegistrationDelaySeconds", 60
                    ),
                },
                "evidenceStatus": "EVIDENCE_STATUS_PENDING",
            }
            ParseDict({EXTENSION_URI: acceptance}, task.metadata)

        if is_new_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        text = context.get_user_input()
        role = _ROLE_NAME.get(int(getattr(msg, "role", 0)), "ROLE_UNSPECIFIED")
        response_text = text.upper()

        if not (activated and requirement):
            await updater.add_artifact([pb.Part(text=response_text)], name="response")
            await updater.complete()
            return

        seal_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        response_digest = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
        transcript = [
            {
                "sequence": 0,
                "messageId": msg.message_id,
                "role": role,
                "partCommitment": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        ]
        non_evidence_artifacts = [
            {"artifactId": "response", "name": "response", "contentCommitment": response_digest}
        ]
        payload = build_task_evidence_payload(
            authority=SERVER_AUTHORITY,
            task_id=task.id,
            context_id=task.context_id,
            transcript=transcript,
            terminal_state="TASK_STATE_COMPLETED",
            non_evidence_artifacts=non_evidence_artifacts,
            seal_time=seal_time,
            capture_policy_id=CAPTURE_POLICY_ID,
        )

        evidence_result = seal_task_evidence(payload, ledger_path=self._ledger_path, ts_url=self._ts_url)

        await updater.add_artifact([pb.Part(text=response_text)], name="response")

        bundle_dict = build_evidence_artifact_bundle(evidence_result)
        bundle_bytes = json.dumps(bundle_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        await updater.add_artifact(
            [pb.Part(raw=bundle_bytes, media_type=MEDIA_TYPE, filename="task-evidence-bundle.json")],
            artifact_id="task-evidence-bundle",
            name="task-evidence-bundle",
            metadata={EXTENSION_URI: evidence_result.task_evidence_reference},
            extensions=[EXTENSION_URI],
        )

        if evidence_result.evidence_status == EVIDENCE_STATUS_FAILED:
            await updater.update_status(
                pb.TaskState.TASK_STATE_FAILED,
                metadata={EXTENSION_URI: evidence_result.task_evidence_reference},
            )
            return

        # This reference implementation reports the evidenceStatus it
        # actually achieved even when that falls short of a REQUIRED
        # minimumEvidence, rather than unilaterally relabeling a genuine
        # SIGNED_RECORD as FAILED -- the client compares achieved vs
        # required itself (§7: it "must still verify explicit acceptance
        # in the response"). See docs/a2a-extension/README.md.
        await updater.update_status(
            pb.TaskState.TASK_STATE_COMPLETED,
            metadata={EXTENSION_URI: evidence_result.task_evidence_reference},
        )

    async def cancel(self, context, event_queue) -> None:  # noqa: ANN001
        raise NotImplementedError("task-evidence reference agent does not support cancel")


def _request_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def build_app(*, ledger_path: str, ts_url: str, public_url: str | None = None) -> FastAPI:
    card = build_agent_card(public_url or "http://localhost")
    executor = TaskEvidenceExecutor(ledger_path=ledger_path, ts_url=ts_url)
    handler = DefaultRequestHandler(executor, InMemoryTaskStore(), card)
    app = FastAPI(title="A2A Task Evidence Reference Agent")

    if public_url:
        agent_card_routes = create_agent_card_routes(card)
    else:
        agent_card_routes = []

        @app.get("/.well-known/agent-card.json", include_in_schema=False)
        def agent_card(request: Request) -> JSONResponse:
            live = build_agent_card(_request_base_url(request))
            return JSONResponse(MessageToDict(live))

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=agent_card_routes,
        jsonrpc_routes=create_jsonrpc_routes(handler, "/a2a"),
    )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8090")))
    ap.add_argument("--ledger", default=os.environ.get("LEDGER_PATH") or str(Path(tempfile.mkdtemp()) / "ledger.jsonl"))
    ap.add_argument("--ts-url", default=os.environ["TS_URL"])
    ap.add_argument("--public-url", default=os.environ.get("A2A_PUBLIC_URL"))
    args = ap.parse_args()
    uvicorn.run(
        build_app(ledger_path=args.ledger, ts_url=args.ts_url, public_url=args.public_url),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
