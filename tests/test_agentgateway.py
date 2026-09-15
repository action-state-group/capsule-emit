# SPDX-License-Identifier: Apache-2.0
"""Tests for the agentgateway ExtMcp gRPC adapter.

Every ``tools/call`` seals TWO capsules: a **planned** one at ``CheckRequest``
and an **outcome** one (confirmed / failed) at ``CheckResponse``, chained to
the planned one when the pairing is unambiguous.  A call whose response never
arrives (rejected by an earlier guardrail processor, upstream transport error)
leaves its planned capsule as the record.
"""
from __future__ import annotations

import json
import socket

import grpc
import pytest

from capsule_emit import read_ledger
from capsule_emit.adapters import ext_mcp_pb2
from capsule_emit.adapters.agentgateway import (
    BACKENDS_KEY,
    PAIRING_KEY,
    UNCHAINED_REASON,
    CapsuleEmitServicer,
    _make_server,
)
from capsule_emit.verification import verify_capsule


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


def _request(req_stub, *, tool_name: str, arguments: dict, backends=("test-backend",)):
    req_stub(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=list(backends),
        mcp_request=json.dumps({"name": tool_name, "arguments": arguments}).encode(),
    ))


def _response(resp_stub, *, tool_result: dict, backends=("test-backend",)):
    return resp_stub(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=list(backends),
        mcp_response=json.dumps(tool_result).encode(),
    ))


def _call(req_stub, resp_stub, *, tool_name: str, arguments: dict, tool_result: dict):
    _request(req_stub, tool_name=tool_name, arguments=arguments)
    _response(resp_stub, tool_result=tool_result)


def _list(req_stub, resp_stub):
    req_stub(ext_mcp_pb2.McpRequest(method="tools/list", service_names=["test-backend"]))
    resp_stub(ext_mcp_pb2.McpResponse(
        method="tools/list",
        service_names=["test-backend"],
        mcp_response=b'{"tools":[]}',
    ))


def _compute(record: dict) -> dict:
    return record["model_attestation"]["compute_attestation"]


def _tool(record: dict) -> str:
    return record["action_id"].split("/")[0]


def _start_server(tmp_path, **servicer_kw):
    ledger = tmp_path / "capsules.jsonl"
    port = _free_port()
    servicer = CapsuleEmitServicer(
        operator="test-org", developer="test-agent@v1", ledger=str(ledger), anchor=False,
        **servicer_kw,
    )
    srv = _make_server(servicer, port, workers=2)
    srv.start()
    channel, req, resp = _grpc_stubs(port)
    return ledger, srv, channel, req, resp


@pytest.fixture()
def server_and_stubs(tmp_path):
    ledger, srv, channel, req, resp = _start_server(tmp_path)
    yield ledger, req, resp
    channel.close()
    srv.stop(grace=0)


# ---------------------------------------------------------------------------
# Core: consequential vs read
# ---------------------------------------------------------------------------


def test_tools_call_seals_planned_and_confirmed(server_and_stubs):
    """tools/call → a planned capsule, then a confirmed capsule chained to it."""
    ledger, req, resp = server_and_stubs
    _call(req, resp,
          tool_name="submit_order",
          arguments={"vendor": "Frobozz", "amount": "99.9"},
          tool_result={"status": "dispatched"})
    planned, outcome = read_ledger(ledger)
    assert _tool(planned) == "submit_order"
    assert planned["effect"] == {"type": "submit_order", "status": "planned"}
    assert "agent_input_digest" in _compute(planned)
    assert "agent_output_digest" not in _compute(planned)
    assert _tool(outcome) == "submit_order"
    assert (outcome["effect"]["type"], outcome["effect"]["status"]) == ("submit_order", "confirmed")
    assert "response_digest" in outcome["effect"]
    assert outcome["chain"]["parent_capsule_id"] == planned["capsule_id"]
    assert _compute(outcome)[PAIRING_KEY] == {"status": "paired"}


def test_tools_list_seals_no_capsule(server_and_stubs):
    """tools/list → zero capsules (read-only; the service ignores it)."""
    ledger, req, resp = server_and_stubs
    _list(req, resp)
    assert len(read_ledger(ledger)) == 0


def test_only_tools_call_counted_among_mixed(server_and_stubs):
    """Mixed sequence: 1 tools/list + 2 tools/call → 2 planned + 2 outcome capsules."""
    ledger, req, resp = server_and_stubs
    _list(req, resp)
    _call(req, resp, tool_name="order_a", arguments={"n": 1}, tool_result={"ok": True})
    _list(req, resp)
    _call(req, resp, tool_name="order_b", arguments={"n": 2}, tool_result={"ok": True})
    _list(req, resp)
    records = read_ledger(ledger)
    assert [(_tool(r), r["effect"]["status"]) for r in records] == [
        ("order_a", "planned"), ("order_a", "confirmed"),
        ("order_b", "planned"), ("order_b", "confirmed"),
    ]


# ---------------------------------------------------------------------------
# Capsule correctness
# ---------------------------------------------------------------------------


def test_capsule_runtime_is_agentgateway(server_and_stubs):
    """Both capsules have runtime='agentgateway'."""
    ledger, req, resp = server_and_stubs
    _call(req, resp, tool_name="pay", arguments={}, tool_result={})
    for r in read_ledger(ledger):
        assert _compute(r)["runtime"] == "agentgateway"


def test_capsules_verify_ok(server_and_stubs):
    """Both capsules sealed via gRPC round-trip verify ok=True offline."""
    ledger, req, resp = server_and_stubs
    _call(req, resp,
          tool_name="place_order",
          arguments={"amount": 500},
          tool_result={"ref": "PO-123"})
    records = read_ledger(ledger)
    assert len(records) == 2
    for r in records:
        vr = verify_capsule(r)
        assert vr.ok, [f.detail for f in vr.findings]


def test_tampered_capsule_fails_verify(server_and_stubs):
    """One byte tampered in the outcome's output digest → verify fails."""
    import copy

    ledger, req, resp = server_and_stubs
    _call(req, resp, tool_name="send_payment", arguments={"to": "alice"}, tool_result={"tx": "abc"})
    raw = read_ledger(ledger)[1]
    tampered = copy.deepcopy(raw)
    ca = tampered["model_attestation"]["compute_attestation"]
    d = ca["agent_output_digest"]
    ca["agent_output_digest"] = d[:-1] + ("0" if d[-1] != "0" else "1")
    vr = verify_capsule(tampered)
    assert not vr.ok


def test_two_sequential_calls_pair_correctly(server_and_stubs):
    """Two sequential tools/calls → two chained pairs, each outcome to its own plan."""
    ledger, req, resp = server_and_stubs
    _call(req, resp, tool_name="alpha", arguments={"x": 1}, tool_result={"y": 10})
    _call(req, resp, tool_name="beta",  arguments={"x": 2}, tool_result={"y": 20})
    a_plan, a_out, b_plan, b_out = read_ledger(ledger)
    assert (_tool(a_plan), _tool(a_out), _tool(b_plan), _tool(b_out)) == ("alpha", "alpha", "beta", "beta")
    assert a_out["chain"]["parent_capsule_id"] == a_plan["capsule_id"]
    assert b_out["chain"]["parent_capsule_id"] == b_plan["capsule_id"]


def test_backends_recorded(server_and_stubs):
    """McpRequest.service_names is sealed on both capsules so records join the gateway log."""
    ledger, req, resp = server_and_stubs
    _request(req, tool_name="fetch", arguments={"url": "https://x"}, backends=("mcp-fetch", "mcp-other"))
    _response(resp, tool_result={"ok": True}, backends=("mcp-fetch", "mcp-other"))
    for r in read_ledger(ledger):
        assert _compute(r)[BACKENDS_KEY] == ["mcp-fetch", "mcp-other"]


# ---------------------------------------------------------------------------
# Outcomes that are not successes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_result", [
    {"content": [{"type": "text", "text": "boom"}], "isError": True},
    {"error": {"code": -32602, "message": "Unknown tool: add"}},
])
def test_error_result_seals_failed_outcome(server_and_stubs, tool_result):
    """isError / JSON-RPC error → verdict='errored', effect.status='failed', still chained."""
    ledger, req, resp = server_and_stubs
    _call(req, resp, tool_name="add", arguments={"a": 1}, tool_result=tool_result)
    planned, outcome = read_ledger(ledger)
    assert planned["effect"]["status"] == "planned"
    assert (outcome["effect"]["type"], outcome["effect"]["status"]) == ("add", "failed")
    assert outcome["disposition"]["verdict_class"] == "errored"
    assert outcome["chain"]["parent_capsule_id"] == planned["capsule_id"]
    assert verify_capsule(outcome).ok


def test_rejected_call_leaves_planned_record(server_and_stubs):
    """A call rejected by an earlier processor gets CheckRequest but never CheckResponse.

    The planned capsule IS the record of it; nothing else is written for it.
    """
    ledger, req, _ = server_and_stubs
    _request(req, tool_name="delete_everything", arguments={"confirm": True})
    records = read_ledger(ledger)
    assert len(records) == 1
    assert _tool(records[0]) == "delete_everything"
    assert records[0]["effect"]["status"] == "planned"
    assert verify_capsule(records[0]).ok


def test_orphaned_plan_does_not_shift_next_pairing(server_and_stubs):
    """Call A: CheckRequest fires, response never arrives.  Call B: full round trip.

    With two plans pending, B's response is ambiguous.  The adapter must NOT
    chain it to A (the old FIFO bug sealed A's tool name and arguments as the
    executed call).  It seals the outcome unchained, naming both candidates,
    clears the pending plans — and the call after that pairs correctly again.
    """
    ledger, req, resp = server_and_stubs
    _request(req, tool_name="stale_tool", arguments={"x": 1})
    # agentgateway skips CheckResponse for a rejected/failed call — no resp here.

    _request(req, tool_name="real_tool", arguments={"y": 2})
    _response(resp, tool_result={"result": "done"})

    records = read_ledger(ledger)
    assert [(_tool(r), r["effect"]["status"]) for r in records] == [
        ("stale_tool", "planned"), ("real_tool", "planned"), ("unknown", "confirmed"),
    ]
    outcome = records[2]
    assert "chain" not in outcome or not outcome["chain"]
    pairing = _compute(outcome)[PAIRING_KEY]
    assert pairing["status"] == "unresolved"
    assert pairing["pending_depth"] == 2
    assert pairing["candidates"] == [records[0]["capsule_id"], records[1]["capsule_id"]]
    assert "agent_input_digest" not in _compute(outcome)

    # Queue has converged: the next call pairs cleanly.
    _call(req, resp, tool_name="next_tool", arguments={"z": 3}, tool_result={"ok": True})
    plan, out = read_ledger(ledger)[3:]
    assert (_tool(plan), _tool(out)) == ("next_tool", "next_tool")
    assert out["chain"]["parent_capsule_id"] == plan["capsule_id"]
    assert _compute(out)[PAIRING_KEY] == {"status": "paired"}


def test_expired_plan_is_dropped_from_pairing(tmp_path, monkeypatch):
    """A plan older than the TTL no longer claims the next response."""
    from capsule_emit.adapters import agentgateway as ag

    ledger, srv, channel, req, resp = _start_server(tmp_path, pending_ttl=10.0)
    try:
        clock = [1000.0]
        monkeypatch.setattr(ag.time, "monotonic", lambda: clock[0])
        _request(req, tool_name="stale_tool", arguments={"x": 1})
        clock[0] += 11.0
        _call(req, resp, tool_name="real_tool", arguments={"y": 2}, tool_result={"ok": True})
    finally:
        channel.close()
        srv.stop(grace=0)
    records = read_ledger(ledger)
    assert [(_tool(r), r["effect"]["status"]) for r in records] == [
        ("stale_tool", "planned"), ("real_tool", "planned"), ("real_tool", "confirmed"),
    ]
    assert records[2]["chain"]["parent_capsule_id"] == records[1]["capsule_id"]
    assert _compute(records[2])[PAIRING_KEY] == {"status": "paired"}


def test_check_response_without_prior_check_request(server_and_stubs):
    """Response with no plan pending → recorded as an unmatched outcome, not dropped."""
    ledger, _, resp = server_and_stubs
    _response(resp, tool_result={"ok": True})
    records = read_ledger(ledger)
    assert len(records) == 1
    assert _tool(records[0]) == "unknown"
    assert records[0]["effect"]["status"] == "confirmed"
    assert _compute(records[0])[PAIRING_KEY] == {"status": "unmatched", "pending_depth": 0}
    assert verify_capsule(records[0]).ok


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_malformed_mcp_request_bytes_does_not_crash(server_and_stubs):
    """Malformed JSON in mcp_request → server stays alive, 'unknown' tool recorded."""
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
    assert [(_tool(r), r["effect"]["status"]) for r in records] == [
        ("unknown", "planned"), ("unknown", "confirmed"),
    ]


def test_tools_call_no_mcp_request_field_still_seals(server_and_stubs):
    """tools/call with absent mcp_request (optional in proto) → both capsules sealed."""
    ledger, req, resp = server_and_stubs
    req(ext_mcp_pb2.McpRequest(method="tools/call", service_names=["backend"]))
    resp(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=["backend"],
        mcp_response=b'{"status": "ok"}',
    ))
    records = read_ledger(ledger)
    assert len(records) == 2, "parameterless tools/call must still be sealed"
    assert all(_tool(r) == "unknown" for r in records)
    assert records[1]["chain"]["parent_capsule_id"] == records[0]["capsule_id"]


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


def test_invalid_ledger_path_does_not_crash_server(tmp_path):
    """emit failure (bad ledger path) → server stays alive, returns Pass on both hooks."""
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
        r1 = req(ext_mcp_pb2.McpRequest(
            method="tools/call",
            service_names=["b"],
            mcp_request=b'{"name":"t","arguments":{}}',
        ))
        assert r1.HasField("pass")
        r2 = resp(ext_mcp_pb2.McpResponse(
            method="tools/call",
            service_names=["b"],
            mcp_response=b'{"ok":true}',
        ))
        assert r2.HasField("pass")
    finally:
        channel.close()
        srv.stop(grace=0)


def test_planned_seal_failure_marks_outcome_unchained(tmp_path, monkeypatch):
    """If the planned seal fails, the outcome is still sealed, unchained, and says why."""
    from capsule_emit.adapters import agentgateway as ag

    real_emit = ag._emit_capsule
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("ledger unwritable")
        return real_emit(**kw)

    monkeypatch.setattr(ag, "_emit_capsule", flaky)
    ledger, srv, channel, req, resp = _start_server(tmp_path)
    try:
        _call(req, resp, tool_name="pay", arguments={"amt": 1}, tool_result={"ok": True})
    finally:
        channel.close()
        srv.stop(grace=0)
    records = read_ledger(ledger)
    assert len(records) == 1
    outcome = records[0]
    assert _tool(outcome) == "pay"
    assert outcome["effect"]["status"] == "confirmed"
    assert "chain" not in outcome or not outcome["chain"]
    assert _compute(outcome)["unchained_reason"] == UNCHAINED_REASON
    assert verify_capsule(outcome).ok


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
