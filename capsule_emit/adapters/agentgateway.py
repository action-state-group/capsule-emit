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
            # CEL expressions evaluated per request and delivered as
            # McpRequest/McpResponse.metadata_context.  These are the audit
            # references consumed by agentgateway_audit -- identifiers only,
            # never token material (agentgateway#3042).
            metadata:
              backendAuth.subject: jwt.sub
            # An empty `allowed` list forwards EVERY header to this processor,
            # `authorization` included.  This adapter never reads headers, but
            # drop it at the gateway so the credential is not on the wire.
            requestHeaders:
              disallowed: [authorization]

Audit metadata (agentgateway#3042)
----------------------------------
Whatever the processor's ``metadata`` config resolves arrives as
``metadata_context`` on both hooks.  :mod:`capsule_emit.adapters.agentgateway_audit`
turns it into the authority-chain block sealed under
``compute_attestation["ext.agentgateway.authority"]``: the subject, the ID-JAG
``jti``/audience, and the resource-token reference, each labelled with the
config key and the hook phase it came from, plus an explicit ``absent`` list for
every reference that did not arrive.  Field names are remappable at runtime --
see ``CAPSULE_AG_AUDIT_KEYS``.

Environment variables::

    CAPSULE_LEDGER          Path to JSONL ledger file (default: ledger.jsonl)
    CAPSULE_OPERATOR        Tenant / org identifier stamped on every capsule
    CAPSULE_DEVELOPER       Agent name + version
    CAPSULE_PORT            gRPC server port (default: 50051)
    CAPSULE_AG_AUDIT_KEYS   JSON object remapping audit slots to
                            metadata_context keys, e.g.
                            {"idjag_jti": "backendAuth.idJag.jti"}

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

from capsule_emit.core import _emit_capsule
from capsule_emit.numbers import canonicalize_for_digest

from . import ext_mcp_pb2
from .agentgateway_audit import (
    build_authority_block,
    key_map_from_env,
    metadata_from_message,
    normalize_key_map,
)

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
        audit_keys: dict[str, object] | None = None,
    ) -> None:
        self._operator = operator
        self._developer = developer
        self._ledger = ledger
        self._anchor = anchor
        self._pending: collections.deque = collections.deque()
        self._lock = threading.Lock()
        # Slot -> metadata_context key. #3042 is still open, so the field names
        # it lands on are not final; this is the seam that makes following them
        # a config change rather than a code change.
        self._audit_keys = (
            normalize_key_map(audit_keys) if audit_keys is not None else key_map_from_env()
        )

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
            # Read metadata_context on this hook too: it is evaluated
            # separately per hook, and a reference that only resolves at request
            # time would otherwise be lost. Headers are deliberately not read --
            # see the module docstring.
            request_md = metadata_from_message(request)
            with self._lock:
                self._pending.append((tool_name, arguments, request_md))
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
            tool_name, arguments, request_md = self._pending.popleft()

        try:
            tool_result = json.loads(request.mcp_response) if request.mcp_response else {}
        except Exception:
            tool_result = {}

        # The authority chain behind this call, cited by reference. Built from
        # both hooks: on #3042 @howardjohn noted the MCP guardrail runs before
        # ID-JAG, so a grant reference can only appear on the response-phase
        # evaluation -- which is why absence is recorded, never assumed.
        authority = build_authority_block(
            request_md, metadata_from_message(request), key_map=self._audit_keys
        )

        try:
            # Calls the internal _emit_capsule primitive directly, like every
            # other capsule-emit adapter (via adapters/_base.py) -- the
            # public seal() verb's canonical shape is seal(payload); an
            # adapter that needs the full flat kwarg set (operator=,
            # developer=, agent_output=, verdict=, effect=, ...) reaches for
            # the primitive those verbs themselves wrap, not the public verb
            # in v3's old shape (frozen surface §1/§9 clean break).
            _emit_capsule(
                action=tool_name,
                # Canonicalized for the same reason every _base adapter does
                # it: a tool argument typed float is a §5.1 error at the digest
                # layer, and agentgateway's own guardrails tests round-trip
                # float tool arguments (mcp_tests.rs). Float-free payloads pass
                # through byte-identical, so no existing digest moves.
                agent_input=canonicalize_for_digest(arguments, field="agent_input"),
                operator=self._operator,
                developer=self._developer,
                agent_output=canonicalize_for_digest(tool_result, field="agent_output"),
                verdict="executed",
                effect={"type": tool_name, "status": "dispatched"},
                anchor=self._anchor,
                ledger=self._ledger,
                runtime="agentgateway",
                extra_compute={"ext.agentgateway.authority": authority},
            )
            _log.debug("CheckResponse: sealed capsule for %s", tool_name)
        except Exception as exc:
            _log.error("CheckResponse: seal failed for %s: %s", tool_name, exc)

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
    audit_keys: dict[str, object] | None = None,
) -> grpc.Server:
    """Start the ExtMcp gRPC server and return it (non-blocking, already started)."""
    servicer = CapsuleEmitServicer(
        operator=operator,
        developer=developer,
        ledger=ledger,
        anchor=anchor,
        audit_keys=audit_keys,
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
