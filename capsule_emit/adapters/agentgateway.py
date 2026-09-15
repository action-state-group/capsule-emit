# SPDX-License-Identifier: Apache-2.0
"""agentgateway adapter for capsule-emit.

Implements agentgateway's ``mcpGuardrails`` ``ExtMcp`` gRPC hook so that every
MCP ``tools/call`` routed through agentgateway is sealed into verifiable
Agent Action Capsules.  Read-only MCP methods (``tools/list``, ``resources/read``,
etc.) are filtered at the **gateway config layer** — they never reach this service.

Architecture::

    LLM agent
      ↓  MCP tools/call
    agentgateway (Rust proxy)
      ↓  mcpGuardrails gRPC CheckRequest  → capsule-emit (PLANNED capsule sealed)
      ↓  forwards to upstream MCP server
      ↑  response from MCP server
      ↑  mcpGuardrails gRPC CheckResponse → capsule-emit (outcome capsule sealed,
      ↑                                      chained to the planned one)
      ↑  response to LLM agent

Two records per call
--------------------
``CheckRequest`` seals a **planned** capsule (``effect.status="planned"``) the
moment the gateway shows us the call — before any other processor, the
upstream server, or the transport gets a say.  ``CheckResponse`` seals the
**outcome**: ``effect.status="confirmed"`` when the tool result is a normal
result, ``verdict="errored"`` / ``effect.status="failed"`` when it is a
JSON-RPC error or carries ``isError: true``; either way ``confirms``-chained
to the planned capsule.

This is the same planned → confirmed/failed shape the LangChain listener
seals, and it is what makes a call that never produces a response — a
guardrail processor listed *before* this one rejecting it, an upstream
transport error, a gateway restart — leave a receipt instead of nothing.
agentgateway runs processors in order and the first ``Reject`` short-circuits
the request phase; the response phase is never run for that call.  With a
single sealed-at-response record, such a call was invisible.  With the
planned record it is a capsule whose outcome never arrived, which is exactly
what happened.

Pairing
-------
The ExtMcp proto carries no per-call identifier, so request and response are
paired by order.  Pairing is only ever *asserted* when it is unambiguous:

* one planned call pending → the response is chained to it;
* a pending planned call older than ``CAPSULE_AG_PENDING_TTL`` seconds is
  expired first (its planned capsule is the record of it) so a dropped
  response cannot shift every later pairing by one;
* more than one pending after expiry (concurrent HTTP sessions, or a rejected
  call whose response never came and has not yet expired) → the outcome is
  sealed **unchained**, naming the candidate planned capsule ids under
  ``compute_attestation["ext.agentgateway.pairing"]``, and every pending
  entry is cleared (their planned capsules stand; none of them can claim a
  later response either) so the next call pairs cleanly.  A guessed pairing
  is a false record; an unresolved one is a true one.

Gateway config snippet (config.yaml)::

    policies:
      mcpGuardrails:
        processors:
          # List this processor AFTER any processor that can reject a call
          # if you want the planned record to reflect only calls that reached
          # the backend; list it FIRST to record every attempt, refused or not.
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
see ``CAPSULE_AG_AUDIT_KEYS``.  The planned capsule carries the request-phase
block; the outcome capsule carries both phases.

The backend(s) the gateway routed the call to (``McpRequest.service_names``)
are sealed under ``compute_attestation["ext.agentgateway.backends"]`` so a
record can be joined to the gateway's own access log.

Environment variables::

    CAPSULE_LEDGER          Path to JSONL ledger file (default: ledger.jsonl)
    CAPSULE_OPERATOR        Tenant / org identifier stamped on every capsule
    CAPSULE_DEVELOPER       Agent name + version
    CAPSULE_PORT            gRPC server port (default: 50051)
    CAPSULE_AG_AUDIT_KEYS   JSON object remapping audit slots to
                            metadata_context keys, e.g.
                            {"idjag_jti": "backendAuth.idJag.jti"}
    CAPSULE_AG_PENDING_TTL  Seconds a planned call waits for its response
                            before it is expired from pairing (default: 120)

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
import time
from concurrent import futures
from typing import Any

import grpc

from capsule_emit.core import _emit_capsule
from capsule_emit.numbers import canonicalize_for_digest

from . import ext_mcp_pb2
from .agentgateway_audit import (
    ABSENT,
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
_PENDING_TTL = float(os.environ.get("CAPSULE_AG_PENDING_TTL", "120"))

_SERVICE_NAME = "agentgateway.dev.ext_mcp.ExtMcp"

#: ``compute_attestation`` keys this adapter seals.
AUTHORITY_KEY = "ext.agentgateway.authority"
BACKENDS_KEY = "ext.agentgateway.backends"
PAIRING_KEY = "ext.agentgateway.pairing"

#: Stamped on an outcome capsule that could not be chained because its
#: planned capsule failed to seal (ledger unwritable, digest error).
UNCHAINED_REASON = (
    "planned capsule could not be sealed for this tools/call; this outcome "
    "record stands alone rather than claim a chain link that does not exist"
)


class _Pending:
    """One planned ``tools/call`` waiting for its ``CheckResponse``."""

    __slots__ = ("planned_id", "tool_name", "arguments", "request_md", "backends", "at")

    def __init__(
        self,
        planned_id: str | None,
        tool_name: str,
        arguments: dict[str, Any],
        request_md: Any,
        backends: list[str],
        at: float,
    ) -> None:
        self.planned_id = planned_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.request_md = request_md
        self.backends = backends
        self.at = at


def _pass_request() -> ext_mcp_pb2.McpRequestResult:
    return ext_mcp_pb2.McpRequestResult(**{"pass": ext_mcp_pb2.Pass()})


def _pass_response() -> ext_mcp_pb2.McpResponseResult:
    return ext_mcp_pb2.McpResponseResult(**{"pass": ext_mcp_pb2.Pass()})


def _parse_call(request: ext_mcp_pb2.McpRequest) -> tuple[str, dict[str, Any]]:
    """``(tool_name, arguments)`` from ``mcp_request`` — never raises.

    The proto marks ``mcp_request`` optional (a tool with no params can omit
    it), and the bytes are whatever the client sent; either way the call is
    still a call and must be recorded, under ``"unknown"`` if need be.
    """
    if not request.HasField("mcp_request"):
        return "unknown", {}
    try:
        params = json.loads(request.mcp_request)
        tool_name = str(params.get("name", "unknown"))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        return tool_name, arguments
    except Exception:
        return "unknown", {}


def _parse_result(response: ext_mcp_pb2.McpResponse) -> dict[str, Any]:
    if not response.mcp_response:
        return {}
    try:
        result = json.loads(response.mcp_response)
    except Exception:
        return {}
    return result if isinstance(result, dict) else {"result": result}


def _result_failed(result: dict[str, Any]) -> bool:
    """Whether the tool result reports failure.

    Two shapes reach the response hook: a JSON-RPC error object (top-level
    ``error``) and an MCP ``CallToolResult`` with ``isError: true`` — the
    tool ran and reported failure in-band.  Both are outcomes; neither is a
    successful dispatch.
    """
    if "error" in result and result["error"] is not None:
        return True
    return result.get("isError") is True


class CapsuleEmitServicer:
    """ExtMcp servicer that seals a planned + outcome capsule per ``tools/call``.

    ``CheckRequest`` seals the planned capsule and remembers it;
    ``CheckResponse`` seals the outcome and chains it to the planned capsule
    when — and only when — the pairing is unambiguous.  See the module
    docstring for the pairing rules.
    """

    def __init__(
        self,
        operator: str = _OPERATOR,
        developer: str = _DEVELOPER,
        ledger: str = _LEDGER,
        anchor: bool = False,
        audit_keys: dict[str, object] | None = None,
        pending_ttl: float = _PENDING_TTL,
    ) -> None:
        self._operator = operator
        self._developer = developer
        self._ledger = ledger
        self._anchor = anchor
        self._pending: collections.deque[_Pending] = collections.deque()
        self._lock = threading.Lock()
        self._pending_ttl = pending_ttl
        # Slot -> metadata_context key. #3042 is still open, so the field names
        # it lands on are not final; this is the seam that makes following them
        # a config change rather than a code change.
        self._audit_keys = (
            normalize_key_map(audit_keys) if audit_keys is not None else key_map_from_env()
        )

    # -- sealing -----------------------------------------------------------

    def _seal(self, **emit_kw: Any) -> str | None:
        """``_emit_capsule`` that logs instead of raising — never fail the gateway.

        Calls the internal ``_emit_capsule`` primitive directly, like every
        other capsule-emit adapter (via adapters/_base.py): the public
        ``seal()`` verb's canonical shape is ``seal(payload)``; an adapter that
        needs the full flat kwarg set reaches for the primitive those verbs
        themselves wrap (frozen surface §1/§9 clean break).
        """
        try:
            result = _emit_capsule(
                operator=self._operator,
                developer=self._developer,
                anchor=self._anchor,
                ledger=self._ledger,
                runtime="agentgateway",
                action_type="fyi",
                **emit_kw,
            )
            return result.capsule_id
        except Exception as exc:
            _log.error("seal failed for %s: %s", emit_kw.get("action"), exc)
            return None

    # -- hooks -------------------------------------------------------------

    def CheckRequest(
        self, request: ext_mcp_pb2.McpRequest, context: grpc.ServicerContext
    ) -> ext_mcp_pb2.McpRequestResult:
        if request.method != "tools/call":
            return _pass_request()

        tool_name, arguments = _parse_call(request)
        backends = list(request.service_names)
        # Read metadata_context on this hook too: it is evaluated separately
        # per hook, and a reference that only resolves at request time would
        # otherwise be lost. Headers are deliberately not read -- see the
        # module docstring.
        request_md = metadata_from_message(request)
        planned_id = self._seal(
            action=tool_name,
            # Canonicalized for the same reason every _base adapter does it: a
            # tool argument typed float is a §5.1 error at the digest layer,
            # and agentgateway's own guardrails tests round-trip float tool
            # arguments (mcp_tests.rs). Float-free payloads pass through
            # byte-identical, so no existing digest moves.
            agent_input=canonicalize_for_digest(arguments, field="agent_input"),
            effect={"type": tool_name, "status": "planned"},
            extra_compute={
                AUTHORITY_KEY: build_authority_block(
                    request_md, ABSENT, key_map=self._audit_keys
                ),
                BACKENDS_KEY: backends,
            },
        )
        # Remembered even when the seal failed (id None): the outcome needs
        # the real tool name, and needs to know a plan was attempted and lost
        # rather than never started — see UNCHAINED_REASON.
        with self._lock:
            self._pending.append(
                _Pending(planned_id, tool_name, arguments, request_md, backends, time.monotonic())
            )
        _log.debug("CheckRequest: planned %s args=%s id=%s", tool_name, sorted(arguments), planned_id)
        return _pass_request()

    def _expire_pending(self, now: float) -> None:
        """Drop planned calls whose response is overdue (caller holds the lock).

        Their planned capsule is the record of them; what is dropped here is
        only their claim on the *next* response.
        """
        while self._pending and now - self._pending[0].at > self._pending_ttl:
            stale = self._pending.popleft()
            _log.warning(
                "CheckResponse: planned %s (id=%s) never received a response within %.0fs; "
                "its planned capsule stands as the record",
                stale.tool_name, stale.planned_id, self._pending_ttl,
            )

    def _resolve(self) -> tuple[_Pending | None, dict[str, Any]]:
        """Pair this response with a pending planned call, or refuse to guess.

        Returns ``(entry, pairing)``: ``entry`` is the planned call when the
        pairing is unambiguous, else ``None``; ``pairing`` is the block sealed
        under :data:`PAIRING_KEY`.
        """
        with self._lock:
            self._expire_pending(time.monotonic())
            depth = len(self._pending)
            if depth == 1:
                return self._pending.popleft(), {"status": "paired"}
            if depth == 0:
                return None, {"status": "unmatched", "pending_depth": 0}
            # Ambiguous: more than one planned call could own this response.
            # Chain nothing — name the candidates instead — and clear them
            # all: none of them can claim a later response either (draining
            # one at a time would leave the queue one deep too many for good).
            candidates = [p.planned_id for p in self._pending]
            self._pending.clear()
            return None, {
                "status": "unresolved",
                "pending_depth": depth,
                "candidates": candidates,
            }

    def CheckResponse(
        self, request: ext_mcp_pb2.McpResponse, context: grpc.ServicerContext
    ) -> ext_mcp_pb2.McpResponseResult:
        if request.method != "tools/call":
            return _pass_response()

        entry, pairing = self._resolve()
        result = _parse_result(request)
        response_md = metadata_from_message(request)
        failed = _result_failed(result)

        if entry is not None:
            tool_name = entry.tool_name
            agent_input = canonicalize_for_digest(entry.arguments, field="agent_input")
            request_md = entry.request_md
            backends = entry.backends or list(request.service_names)
            confirms = entry.planned_id
        else:
            # No asserted pairing: the response is a real outcome that
            # happened through this gateway, so it is still recorded — but
            # under no tool name and no chain we cannot stand behind.
            tool_name = "unknown"
            agent_input = None
            request_md = ABSENT
            backends = list(request.service_names)
            confirms = None

        extra: dict[str, Any] = {
            # The authority chain behind this call, cited by reference. Built
            # from both hooks: on #3042 @howardjohn noted the MCP guardrail
            # runs before ID-JAG, so a grant reference can only appear on the
            # response-phase evaluation -- which is why absence is recorded,
            # never assumed.
            AUTHORITY_KEY: build_authority_block(
                request_md, response_md, key_map=self._audit_keys
            ),
            BACKENDS_KEY: backends,
            PAIRING_KEY: pairing,
        }
        if entry is not None and entry.planned_id is None:
            extra["unchained_reason"] = UNCHAINED_REASON

        self._seal(
            action=tool_name,
            agent_input=agent_input,
            agent_output=canonicalize_for_digest(result, field="agent_output"),
            verdict="errored" if failed else "executed",
            effect={"type": tool_name, "status": "failed" if failed else "confirmed"},
            confirms=confirms,
            extra_compute=extra,
        )
        _log.debug(
            "CheckResponse: sealed %s outcome for %s (%s)",
            "failed" if failed else "confirmed", tool_name, pairing["status"],
        )
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
    pending_ttl: float = _PENDING_TTL,
) -> grpc.Server:
    """Start the ExtMcp gRPC server and return it (non-blocking, already started)."""
    servicer = CapsuleEmitServicer(
        operator=operator,
        developer=developer,
        ledger=ledger,
        anchor=anchor,
        audit_keys=audit_keys,
        pending_ttl=pending_ttl,
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
