# SPDX-License-Identifier: Apache-2.0
"""agentgateway adapter for capsule-emit.

Implements agentgateway's ``mcpGuardrails`` ``ExtMcp`` gRPC hook so that every
MCP ``tools/call`` routed through agentgateway is sealed into a verifiable
Agent Action Capsule.  Read-only MCP methods (``tools/list``, ``resources/read``,
etc.) are filtered at the **gateway config layer** — they never reach this service.

Architecture::

    LLM agent
      ↓  MCP tools/call
    agentgateway (Rust proxy)
      ↓  mcpGuardrails gRPC CheckRequest  → capsule-emit (input captured)
      ↓  forwards to upstream MCP server
      ↑  response from MCP server
      ↑  mcpGuardrails gRPC CheckResponse → capsule-emit (capsule sealed)
      ↑  response to LLM agent

Gateway config snippet (config.yaml)::

    policies:
      mcpGuardrails:
        processors:
          - kind: remote
            host: "localhost:50051"
            methods:
              "tools/call": full
            failureMode: failOpen

Environment variables::

    CAPSULE_LEDGER     Path to JSONL ledger file (default: ledger.jsonl)
    CAPSULE_OPERATOR   Tenant / org identifier stamped on every capsule
    CAPSULE_DEVELOPER  Agent name + version
    CAPSULE_PORT       gRPC server port (default: 50051)

Run::

    pip install "capsule-emit[agentgateway]"
    capsule-emit-agentgateway          # console script
    python -m capsule_emit.adapters.agentgateway
"""
from __future__ import annotations

import collections
import json
import logging
import os
import threading
from concurrent import futures

import grpc

from capsule_emit import emit

from . import ext_mcp_pb2

_log = logging.getLogger(__name__)

_LEDGER = os.environ.get("CAPSULE_LEDGER", "ledger.jsonl")
_OPERATOR = os.environ.get("CAPSULE_OPERATOR", "agentgateway-user")
_DEVELOPER = os.environ.get("CAPSULE_DEVELOPER", "agentgateway-agent@v1")
_PORT = int(os.environ.get("CAPSULE_PORT", "50051"))

_SERVICE_NAME = "agentgateway.dev.ext_mcp.ExtMcp"


def _pass_request() -> ext_mcp_pb2.McpRequestResult:
    return ext_mcp_pb2.McpRequestResult(**{"pass": ext_mcp_pb2.Pass()})


def _pass_response() -> ext_mcp_pb2.McpResponseResult:
    return ext_mcp_pb2.McpResponseResult(**{"pass": ext_mcp_pb2.Pass()})


class CapsuleEmitServicer:
    """ExtMcp servicer that seals one Agent Action Capsule per ``tools/call``.

    Correlation: ``CheckRequest`` stashes parsed tool params in a FIFO deque;
    ``CheckResponse`` pops and pairs them with the tool result before sealing.

    This is sequential-safe — correct for MCP stdio transport (one in-flight
    call per session).  For concurrent HTTP sessions, use a call-ID injected
    via agentgateway's metadata CEL config and correlate by that key instead.
    """

    def __init__(
        self,
        operator: str = _OPERATOR,
        developer: str = _DEVELOPER,
        ledger: str = _LEDGER,
        anchor: bool = False,
    ) -> None:
        self._operator = operator
        self._developer = developer
        self._ledger = ledger
        self._anchor = anchor
        self._pending: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def CheckRequest(
        self, request: ext_mcp_pb2.McpRequest, context: grpc.ServicerContext
    ) -> ext_mcp_pb2.McpRequestResult:
        if request.method == "tools/call":
            # Always push for any tools/call so the deque stays in sync with CheckResponse,
            # even when mcp_request is absent (proto marks it optional; tools with no
            # params can omit it).  Without this, a parameterless call silently skips the
            # seal in CheckResponse.
            if request.HasField("mcp_request"):
                try:
                    params = json.loads(request.mcp_request)
                    tool_name = str(params.get("name", "unknown"))
                    arguments = params.get("arguments") or {}
                    if not isinstance(arguments, dict):
                        arguments = {}
                except Exception:
                    tool_name, arguments = "unknown", {}
            else:
                tool_name, arguments = "unknown", {}
            with self._lock:
                self._pending.append((tool_name, arguments))
            _log.debug("CheckRequest: captured %s args=%s", tool_name, sorted(arguments))
        return _pass_request()

    def CheckResponse(
        self, request: ext_mcp_pb2.McpResponse, context: grpc.ServicerContext
    ) -> ext_mcp_pb2.McpResponseResult:
        if request.method != "tools/call":
            return _pass_response()

        with self._lock:
            if not self._pending:
                _log.warning("CheckResponse: queue empty for tools/call — skipping seal")
                return _pass_response()
            tool_name, arguments = self._pending.popleft()

        try:
            tool_result = json.loads(request.mcp_response) if request.mcp_response else {}
        except Exception:
            tool_result = {}

        try:
            emit(
                action=tool_name,
                operator=self._operator,
                developer=self._developer,
                agent_input=arguments,
                agent_output=tool_result,
                verdict="executed",
                effect={"type": tool_name, "status": "dispatched"},
                anchor=self._anchor,
                ledger=self._ledger,
                runtime="agentgateway",
            )
            _log.debug("CheckResponse: sealed capsule for %s", tool_name)
        except Exception as exc:
            _log.error("CheckResponse: emit failed for %s: %s", tool_name, exc)

        return _pass_response()


def _make_server(servicer: CapsuleEmitServicer, port: int, workers: int) -> grpc.Server:
    rpc_handlers = {
        "CheckRequest": grpc.unary_unary_rpc_method_handler(
            servicer.CheckRequest,
            request_deserializer=ext_mcp_pb2.McpRequest.FromString,
            response_serializer=ext_mcp_pb2.McpRequestResult.SerializeToString,
        ),
        "CheckResponse": grpc.unary_unary_rpc_method_handler(
            servicer.CheckResponse,
            request_deserializer=ext_mcp_pb2.McpResponse.FromString,
            response_serializer=ext_mcp_pb2.McpResponseResult.SerializeToString,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(_SERVICE_NAME, rpc_handlers)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    server.add_generic_rpc_handlers((generic_handler,))
    server.add_insecure_port(f"[::]:{port}")
    return server


def serve(
    port: int = _PORT,
    operator: str = _OPERATOR,
    developer: str = _DEVELOPER,
    ledger: str = _LEDGER,
    anchor: bool = False,
    workers: int = 4,
) -> grpc.Server:
    """Start the ExtMcp gRPC server and return it (non-blocking, already started)."""
    servicer = CapsuleEmitServicer(
        operator=operator,
        developer=developer,
        ledger=ledger,
        anchor=anchor,
    )
    server = _make_server(servicer, port, workers)
    server.start()
    _log.info("capsule-emit agentgateway ExtMcp server listening on port %d", port)
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = serve()
    server.wait_for_termination()


if __name__ == "__main__":
    main()
