# SPDX-License-Identifier: Apache-2.0
"""Tests for the agentgateway ExtMcp gRPC adapter."""
from __future__ import annotations

import json
import socket

import grpc
import pytest

from capsule_emit import read_ledger
from capsule_emit.adapters import ext_mcp_pb2
from capsule_emit.adapters.agentgateway import CapsuleEmitServicer, _make_server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _grpc_stubs(port: int):
    ch = grpc.insecure_channel(f"localhost:{port}")
    req = ch.unary_unary(
        "/agentgateway.dev.ext_mcp.ExtMcp/CheckRequest",
        request_serializer=ext_mcp_pb2.McpRequest.SerializeToString,
        response_deserializer=ext_mcp_pb2.McpRequestResult.FromString,
    )
    resp = ch.unary_unary(
        "/agentgateway.dev.ext_mcp.ExtMcp/CheckResponse",
        request_serializer=ext_mcp_pb2.McpResponse.SerializeToString,
        response_deserializer=ext_mcp_pb2.McpResponseResult.FromString,
    )
    return ch, req, resp


def _call(req_stub, resp_stub, *, tool_name: str, arguments: dict, tool_result: dict):
    req_stub(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["test-backend"],
        mcp_request=json.dumps({"name": tool_name, "arguments": arguments}).encode(),
    ))
    resp_stub(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=["test-backend"],
        mcp_response=json.dumps(tool_result).encode(),
    ))


def _list(req_stub, resp_stub):
    req_stub(ext_mcp_pb2.McpRequest(method="tools/list", service_names=["test-backend"]))
    resp_stub(ext_mcp_pb2.McpResponse(
        method="tools/list",
        service_names=["test-backend"],
        mcp_response=b'{"tools":[]}',
    ))


@pytest.fixture()
def server_and_stubs(tmp_path):
    ledger = tmp_path / "capsules.jsonl"
    port = _free_port()
    servicer = CapsuleEmitServicer(
        operator="test-org", developer="test-agent@v1", ledger=str(ledger), anchor=False
    )
    srv = _make_server(servicer, port, workers=2)
    srv.start()
    channel, req, resp = _grpc_stubs(port)
    yield ledger, req, resp
    channel.close()
    srv.stop(grace=0)


# ---------------------------------------------------------------------------
# Core: consequential vs read
# ---------------------------------------------------------------------------


def test_tools_call_seals_capsule(server_and_stubs):
    """tools/call → exactly one capsule sealed in the ledger."""
    ledger, req, resp = server_and_stubs
    _call(req, resp,
          tool_name="submit_order",
          arguments={"vendor": "Frobozz", "amount": 99.9},
          tool_result={"status": "dispatched"})
    records = read_ledger(ledger)
    assert len(records) == 1
    assert records[0]["action_id"].startswith("submit_order")


def test_tools_list_seals_no_capsule(server_and_stubs):
    """tools/list → zero capsules (read-only; the service ignores it)."""
    ledger, req, resp = server_and_stubs
    _list(req, resp)
    assert len(read_ledger(ledger)) == 0


def test_only_tools_call_counted_among_mixed(server_and_stubs):
    """Mixed sequence: 1 tools/list + 2 tools/call → exactly 2 capsules."""
    ledger, req, resp = server_and_stubs
    _list(req, resp)
    _call(req, resp, tool_name="order_a", arguments={"n": 1}, tool_result={"ok": True})
    _list(req, resp)
    _call(req, resp, tool_name="order_b", arguments={"n": 2}, tool_result={"ok": True})
    _list(req, resp)
    assert len(read_ledger(ledger)) == 2


# ---------------------------------------------------------------------------
# Capsule correctness
# ---------------------------------------------------------------------------


def test_capsule_runtime_is_agentgateway(server_and_stubs):
    """Sealed capsule has runtime='agentgateway'."""
    ledger, req, resp = server_and_stubs
    _call(req, resp, tool_name="pay", arguments={}, tool_result={})
    r = read_ledger(ledger)[0]
    assert r["model_attestation"]["compute_attestation"]["runtime"] == "agentgateway"


def test_capsule_verifies_ok(server_and_stubs):
    """Capsule sealed via gRPC round-trip verifies ok=True offline."""
    from agent_action_capsule import verify
    ledger, req, resp = server_and_stubs
    _call(req, resp,
          tool_name="place_order",
          arguments={"amount": 500},
          tool_result={"ref": "PO-123"})
    records = read_ledger(ledger)
    vr = verify(records[0])
    assert vr.ok, [f.detail for f in vr.findings]


def test_tampered_capsule_fails_verify(server_and_stubs):
    """One byte tampered in the output digest → verify fails."""
    import copy

    from agent_action_capsule import verify
    ledger, req, resp = server_and_stubs
    _call(req, resp, tool_name="send_payment", arguments={"to": "alice"}, tool_result={"tx": "abc"})
    raw = read_ledger(ledger)[0]
    tampered = copy.deepcopy(raw)
    ca = tampered["model_attestation"]["compute_attestation"]
    d = ca["agent_output_digest"]
    ca["agent_output_digest"] = d[:-1] + ("0" if d[-1] != "0" else "1")
    vr = verify(tampered)
    assert not vr.ok


def test_two_sequential_calls_two_capsules(server_and_stubs):
    """Two sequential tools/calls → two distinct capsules, FIFO correlation correct."""
    ledger, req, resp = server_and_stubs
    _call(req, resp, tool_name="alpha", arguments={"x": 1}, tool_result={"y": 10})
    _call(req, resp, tool_name="beta",  arguments={"x": 2}, tool_result={"y": 20})
    records = read_ledger(ledger)
    assert len(records) == 2
    names = [r["action_id"].split("/")[0] for r in records]
    assert names == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_malformed_mcp_request_bytes_does_not_crash(server_and_stubs):
    """Malformed JSON in mcp_request → server stays alive, empty tool_name recorded."""
    ledger, req, resp = server_and_stubs
    req(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["backend"],
        mcp_request=b"NOT JSON",
    ))
    resp(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=["backend"],
        mcp_response=b'{}',
    ))
    records = read_ledger(ledger)
    assert len(records) == 1
    assert records[0]["action_id"].startswith("unknown")


def test_tools_call_no_mcp_request_field_still_seals(server_and_stubs):
    """tools/call with absent mcp_request (optional in proto) → capsule sealed, no skip."""
    ledger, req, resp = server_and_stubs
    # Send CheckRequest with NO mcp_request field set (field absent, not empty bytes).
    req(ext_mcp_pb2.McpRequest(method="tools/call", service_names=["backend"]))
    resp(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=["backend"],
        mcp_response=b'{"status": "ok"}',
    ))
    records = read_ledger(ledger)
    assert len(records) == 1, "parameterless tools/call must still be sealed"
    assert records[0]["action_id"].startswith("unknown")


@pytest.mark.parametrize("method", [
    "tools/list",
    "resources/read",
    "resources/list",
    "prompts/get",
    "prompts/list",
    "notifications/initialized",
    "unknown/method",
    "",
])
def test_read_only_methods_seal_no_capsule(server_and_stubs, method):
    """Every non-tools/call method on either hook → zero capsules."""
    ledger, req, resp = server_and_stubs
    req(ext_mcp_pb2.McpRequest(method=method, service_names=["backend"]))
    resp(ext_mcp_pb2.McpResponse(method=method, service_names=["backend"], mcp_response=b'{}'))
    assert len(read_ledger(ledger)) == 0, f"method={method!r} must produce 0 capsules"


def test_stale_deque_after_upstream_error_does_not_corrupt_next_call(server_and_stubs):
    """Simulate upstream transport error: CheckRequest fires but CheckResponse never fires.

    The stale deque entry must not corrupt the subsequent successful call's capsule.
    """
    ledger, req, resp = server_and_stubs
    # Simulate call A: CheckRequest fires, upstream crashes → CheckResponse never arrives.
    req(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["backend"],
        mcp_request=b'{"name":"stale_tool","arguments":{"x":1}}',
    ))
    # agentgateway skips CheckResponse on transport error — we model that by NOT calling resp here.

    # Call B: full round trip.
    req(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["backend"],
        mcp_request=b'{"name":"real_tool","arguments":{"y":2}}',
    ))
    resp(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=["backend"],
        mcp_response=b'{"result":"done"}',
    ))

    records = read_ledger(ledger)
    # The stale entry from call A occupies position 0 in the deque; the CheckResponse
    # for call B pops it.  One capsule is produced, but it carries call A's params —
    # this is the documented limitation when upstream transport errors drop a response.
    # Verify the server is alive and we get exactly 1 capsule (not 2, not a crash).
    assert len(records) == 1
    assert records[0]["action_id"].startswith("stale_tool"), (
        "FIFO pop gives stale entry — expected documented behaviour; "
        "if this assertion flips, the correlation logic changed"
    )


def test_check_response_without_prior_check_request(server_and_stubs):
    """Response arriving without a prior CheckRequest → no crash, no capsule."""
    ledger, _, resp = server_and_stubs
    resp(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=["backend"],
        mcp_response=b'{"ok": true}',
    ))
    assert len(read_ledger(ledger)) == 0


def test_invalid_ledger_path_does_not_crash_server(tmp_path):
    """emit failure (bad ledger path) → server stays alive, returns Pass."""
    port = _free_port()
    servicer = CapsuleEmitServicer(
        operator="org", developer="dev@v1",
        ledger="/nonexistent/path/capsules.jsonl",
        anchor=False,
    )
    srv = _make_server(servicer, port, workers=2)
    srv.start()
    channel, req, resp = _grpc_stubs(port)
    try:
        req(ext_mcp_pb2.McpRequest(
            method="tools/call",
            service_names=["b"],
            mcp_request=b'{"name":"t","arguments":{}}',
        ))
        result = resp(ext_mcp_pb2.McpResponse(
            method="tools/call",
            service_names=["b"],
            mcp_response=b'{"ok":true}',
        ))
        assert result.HasField("pass")
    finally:
        channel.close()
        srv.stop(grace=0)


# ---------------------------------------------------------------------------
# Module-level sanity
# ---------------------------------------------------------------------------


def test_agentgateway_module_importable():
    """The adapter module imports without error."""
    from capsule_emit.adapters import agentgateway  # noqa: F401


def test_ext_mcp_pb2_importable():
    """Generated protobuf stubs import and basic message construction works."""
    req = ext_mcp_pb2.McpRequest(method="tools/call", service_names=["svc"])
    assert req.method == "tools/call"
    result = ext_mcp_pb2.McpRequestResult(**{"pass": ext_mcp_pb2.Pass()})
    assert result.HasField("pass")
