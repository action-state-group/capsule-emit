#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Serve the AAC boundary-seal A2A agent (a2a-sdk 1.1.1).

An A2A JSON-RPC server whose single skill seals each task's input+output as an
AAC capsule (capsule-emit), anchors the capsule_id on the transparency log, and
returns the capsule reference (capsule_id, entry_hash, leaf_index, tree_size,
anchor root, inclusion-proof URL) as artifact metadata under the AAC extension.

- Agent Card:  GET /.well-known/agent-card.json   (extension advertised)
- JSON-RPC:    POST /a2a                            (SendMessage)
- Gate:        capsule.digest + capsule.resolve, evaluated against the LIVE
               anchor. On the DENY path (fabricated capsule_id) the task fails.

Run:  python serve_boundary_agent.py --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path

import uvicorn
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import a2a_pb2 as pb
from fastapi import FastAPI
from google.protobuf.json_format import ParseDict

ANCHOR_BASE = os.environ.get("AAC_ANCHOR_URL", "https://anchor.agentactioncapsule.org").rstrip("/")
EXT_URI = "https://agentactioncapsule.org/a2a-extension/v1"
RPC_URL = "/a2a"

_ROLE_NAME = {0: "ROLE_UNSPECIFIED", 1: "ROLE_USER", 2: "ROLE_AGENT"}


def build_agent_card(public_url: str) -> pb.AgentCard:
    card_dict = {
        "name": "AAC Boundary Seal Agent",
        "description": (
            "Reference agent for the A2A ↔ AAC boundary seal "
            "(draft-mih-scitt-agent-action-capsule-02). Every A2A task "
            "input/output is sealed as a SCITT Signed Statement and anchored "
            "on the capsule transparency log."
        ),
        "version": "0.1.0",
        "supportedInterfaces": [
            {
                "url": f"{public_url}{RPC_URL}",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [
                {
                    "uri": EXT_URI,
                    "required": False,
                    "description": (
                        "Each response artifact carries capsule_id + anchor "
                        "inclusion coordinates for independent SCITT verification. "
                        "capsule_id = SHA-256(JCS(capsule body)) per -02 §4.2."
                    ),
                }
            ],
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "boundary-seal",
                "name": "Boundary Seal",
                "description": (
                    "Seals A2A task input and output as an AAC capsule and "
                    "anchors it; the response carries the capsule reference."
                ),
                "tags": ["aac", "capsule", "scitt", "audit", "a2a"],
            }
        ],
        "provider": {
            "organization": "action-state-group",
            "url": "https://agentactioncapsule.org",
        },
        "documentationUrl": (
            "https://github.com/action-state-group/capsule-emit/tree/main/"
            "examples/a2a-ap2/boundary-seal"
        ),
    }
    card = pb.AgentCard()
    ParseDict(card_dict, card)
    return card


def _anchor_and_resolve(capsule_id: str) -> dict:
    """Anchor the capsule_id and read back its coordinates in ONE call.

    A single ``POST /v1/digest`` both registers (anchors) the capsule_id and
    returns its CT coordinates + Receipt. Submitting exactly once is deliberate:
    two near-simultaneous submissions of the same new capsule_id can double-
    append on the anchor (its register dedup is not atomic), so the seal path
    must NOT both emit(anchor=True) AND resolve. ``ok`` is True only on HTTP 200
    with entry_hash == SHA-256(bytes.fromhex(capsule_id)).
    """
    expected = hashlib.sha256(bytes.fromhex(capsule_id)).hexdigest()
    out = {"ok": False, "entry_hash": None, "leaf_index": None, "tree_size": None,
           "inclusion_proof_url": None, "expected_entry_hash": expected}
    req = urllib.request.Request(
        f"{ANCHOR_BASE}/v1/digest",
        data=json.dumps({"capsule_id": capsule_id}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = resp.status
        body = json.loads(resp.read())
    out["entry_hash"] = body.get("entry_hash")
    out["leaf_index"] = body.get("leaf_index")
    out["tree_size"] = body.get("tree_size")
    out["ok"] = status == 200 and out["entry_hash"] == expected
    if out["leaf_index"] is not None and out["tree_size"] is not None:
        out["inclusion_proof_url"] = (
            f"{ANCHOR_BASE}/anchor/inclusion-proof-ct"
            f"?leaf_index={out['leaf_index']}&tree_size={out['tree_size']}"
        )
    return out


class BoundarySealExecutor(AgentExecutor):
    """Seals+anchors every task; returns the capsule reference under the ext."""

    async def execute(self, context, event_queue) -> None:  # noqa: ANN001
        from a2a.helpers.proto_helpers import new_task_from_user_message
        from agent_action_capsule import verify

        from capsule_emit import emit, verify_input_digest

        msg = context.message
        # The first event MUST be a Task; create it from the user message and
        # enqueue it, then drive status/artifacts through the updater.
        task = context.current_task or new_task_from_user_message(msg)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        text = context.get_user_input()
        role = _ROLE_NAME.get(int(getattr(msg, "role", 0)), "ROLE_UNSPECIFIED")

        agent_input = {
            "a2a_request": {
                "method": "SendMessage",
                "task_id": task.id,
                "message_id": msg.message_id,
                "role": role,
                "text": text,
            }
        }
        agent_output = {
            "a2a_response": {
                "task_id": task.id,
                "status": "completed",
                "artifact": {
                    "name": "equity-data",
                    "parts": [
                        {
                            "text": (
                                "AAPL | ticker: AAPL | sector: Technology | "
                                "exchange: NASDAQ | source: public — "
                                "synthetic deterministic fixture"
                            )
                        }
                    ],
                },
            }
        }

        ledger = Path(tempfile.mkdtemp(prefix="a2a-serve-")) / "ledger.jsonl"
        # Seal WITHOUT emit's fire-and-forget anchor; we anchor+resolve in a
        # single POST below so the capsule_id is submitted exactly once.
        result = emit(
            action="a2a.boundary_seal",
            operator="action-state-group",
            developer="a2a-sdk==1.1.1@86c6b0d",
            runtime="draft-mih-scitt-agent-action-capsule-02",
            agent_input=agent_input,
            agent_output=agent_output,
            model={"provider": "synthetic", "model_id": "boundary-seal-reference"},
            verdict="executed",
            effect={"type": "a2a.task_completed", "status": "confirmed", "task_id": task.id},
            anchor=False,
            ledger=ledger,
        )
        capsule_id = result.capsule_id

        # capsule.digest gate — in-process structural + digest check.
        digest_ok = verify(result.capsule).ok and verify_input_digest(result.capsule, agent_input)
        # capsule.resolve gate — single anchor+resolve round-trip to the anchor.
        resolve = _anchor_and_resolve(capsule_id)

        if not (digest_ok and resolve["ok"]):
            await updater.failed(
                message=updater.new_agent_message(
                    [pb.Part(text=f"boundary-seal DENY: digest_ok={digest_ok} resolve_ok={resolve['ok']}")]
                )
            )
            return

        compute = result.capsule["model_attestation"]["compute_attestation"]
        ext_binding = {
            "uri": EXT_URI,
            "capsule_id": capsule_id,
            "anchor": ANCHOR_BASE,
            "input_digest": compute["agent_input_digest"],
            "output_digest": compute["agent_output_digest"],
            "entry_hash": resolve["entry_hash"],
            "leaf_index": resolve["leaf_index"],
            "tree_size": resolve["tree_size"],
            "inclusion_proof_url": resolve["inclusion_proof_url"],
            "resolve_by_id_url": f"{ANCHOR_BASE}/v1/inclusion/{capsule_id}",
            "gate_results": {"capsule.digest": "PASS", "capsule.resolve": "PASS"},
        }

        out_text = agent_output["a2a_response"]["artifact"]["parts"][0]["text"]
        await updater.add_artifact(
            [pb.Part(text=out_text)],
            name="equity-data",
            metadata={EXT_URI: ext_binding},
            extensions=[EXT_URI],
        )
        await updater.complete()

    async def cancel(self, context, event_queue) -> None:  # noqa: ANN001
        raise NotImplementedError("boundary-seal agent does not support cancel")


def build_app(public_url: str) -> FastAPI:
    card = build_agent_card(public_url)
    handler = DefaultRequestHandler(BoundarySealExecutor(), InMemoryTaskStore(), card)
    app = FastAPI(title="AAC Boundary Seal A2A Agent")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, RPC_URL),
    )
    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--public-url", default=None, help="externally reachable base URL (defaults to http://host:port)")
    args = ap.parse_args()
    public_url = args.public_url or f"http://{args.host}:{args.port}"
    uvicorn.run(build_app(public_url), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
