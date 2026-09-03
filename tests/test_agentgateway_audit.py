# SPDX-License-Identifier: Apache-2.0
"""Tests for the #3042 audit-metadata consumer leg of the agentgateway adapter.

Every upstream behaviour asserted here is cited to agentgateway at commit
a2f9b89 (2026-09-01):

* ``crates/protos/proto/ext_mcp.proto`` — ``metadata_context`` is a
  ``google.protobuf.Struct``, "CEL-evaluated context from gateway config, one
  field per config key", present on both ``McpRequest`` and ``McpResponse``.
* ``crates/agentgateway/src/mcp/guardrails/client.rs`` — ``build_metadata``
  evaluates the processor's ``metadata`` CEL map per request and **drops** any
  key whose expression errors; ``check_response`` re-evaluates the same map on
  the response hook.
* ``crates/agentgateway/src/mcp/guardrails/mod.rs`` — ``Remote.metadata`` is
  ``HashMap<String, Arc<cel::Expression>>``.
* ``crates/agentgateway/src/mcp/mcp_tests.rs`` —
  ``mcp_guardrails_metadata_cel_evaluated_per_request`` uses the literal config
  key ``"tenant.io"`` (dots in keys) mapped to a CEL **map** literal (nested
  values), and the mutation test round-trips a float tool argument.
"""
from __future__ import annotations

import json
import socket

import grpc
import pytest
from google.protobuf import struct_pb2

from capsule_emit import read_ledger
from capsule_emit.adapters import ext_mcp_pb2
from capsule_emit.adapters.agentgateway import CapsuleEmitServicer, _make_server
from capsule_emit.adapters.agentgateway_audit import (
    ABSENT,
    AUDIT_SLOTS,
    DEFAULT_KEY_MAP,
    KEY_MAP_ENV,
    SCHEMA,
    build_authority_block,
    key_map_from_env,
    metadata_from_message,
    normalize_key_map,
    struct_to_dict,
    token_shaped,
)


def _struct(d: dict) -> struct_pb2.Struct:
    s = struct_pb2.Struct()
    s.update(d)
    return s


# ---------------------------------------------------------------------------
# Struct decoding — the wire carrier
# ---------------------------------------------------------------------------


def test_struct_to_dict_handles_every_value_kind():
    """Struct decodes to plain Python across all six Value kinds."""
    got = struct_to_dict(_struct({
        "s": "text", "b": True, "n": 7, "f": 2.5,
        "nested": {"inner": "v"}, "list": ["a", 1], "null": None,
    }))
    assert got == {
        "s": "text", "b": True, "n": 7, "f": 2.5,
        "nested": {"inner": "v"}, "list": ["a", 1], "null": None,
    }


def test_struct_integral_double_becomes_int():
    """Struct has no integer type; an exact integral double recovers as int."""
    got = struct_to_dict(_struct({"n": 10.0}))
    assert got["n"] == 10
    assert isinstance(got["n"], int)


def test_struct_to_dict_tolerates_none_and_dict():
    assert struct_to_dict(None) == {}
    assert struct_to_dict({"already": "plain"}) == {"already": "plain"}


def test_absent_metadata_context_is_distinct_from_empty():
    """Field unset (no `metadata` configured) != present-but-empty (CEL failed).

    build_metadata returns None when the config map is empty, and a Struct with
    no fields when every expression errored. Collapsing the two would report a
    broken CEL expression as an operator who never asked for the field.
    """
    unset = ext_mcp_pb2.McpRequest(method="tools/call")
    assert metadata_from_message(unset) is ABSENT

    empty = ext_mcp_pb2.McpRequest(method="tools/call", metadata_context=_struct({}))
    assert metadata_from_message(empty) == {}
    assert metadata_from_message(empty) is not ABSENT

    block_unset = build_authority_block(metadata_from_message(unset), ABSENT)
    block_empty = build_authority_block(metadata_from_message(empty), ABSENT)
    assert block_unset["metadata_keys"]["request"] is None
    assert block_empty["metadata_keys"]["request"] == []


# ---------------------------------------------------------------------------
# Lookup — literal keys vs dotted paths
# ---------------------------------------------------------------------------


def test_nested_object_wiring_resolves():
    """One config key carrying a whole object (`metadata: {backendAuth: ...}`)."""
    md = {"backendAuth": {
        "subject": "user@corp.example",
        "idJag": {"jti": "jag-1", "audience": "https://mcp.example"},
        "resourceToken": {"jti": "rt-1"},
    }}
    block = build_authority_block(md, ABSENT)
    assert block["absent"] == []
    assert block["fields"]["subject"]["value"] == "user@corp.example"
    assert block["fields"]["resource_token"]["value"] == "rt-1"
    assert block["grants"] == [{"jti": "jag-1", "aud": "https://mcp.example"}]


def test_flat_dotted_config_keys_resolve():
    """The same fields wired as flat literal keys with dots in the name."""
    md = {
        "backendAuth.subject": "user@corp.example",
        "backendAuth.idJag.jti": "jag-1",
        "backendAuth.idJag.audience": "https://mcp.example",
        "backendAuth.resourceToken.jti": "rt-1",
    }
    block = build_authority_block(md, ABSENT)
    assert block["absent"] == []
    assert block["fields"]["idjag_jti"]["key"] == "backendAuth.idJag.jti"


def test_literal_key_beats_path_walk():
    """A literal key wins over a nested path of the same name.

    Upstream keys are free-form strings that may contain dots (`"tenant.io"` in
    agentgateway's own guardrails test), so a literal match must not be
    shadowed by a same-named nested traversal.
    """
    md = {
        "backendAuth.subject": "literal-wins",
        "backendAuth": {"subject": "path-loses"},
    }
    block = build_authority_block(md, ABSENT)
    assert block["fields"]["subject"]["value"] == "literal-wins"


def test_dotted_key_from_upstream_test_does_not_crash_lookup():
    """`tenant.io`-style keys alongside ours resolve without interference."""
    md = {"tenant.io": {"path": "/mcp"}, "backendAuth.subject": "u"}
    block = build_authority_block(md, ABSENT)
    assert block["fields"]["subject"]["value"] == "u"
    assert block["metadata_keys"]["request"] == ["backendAuth.subject", "tenant.io"]


# ---------------------------------------------------------------------------
# Absence — "absent is never pass"
# ---------------------------------------------------------------------------


def test_no_metadata_at_all_names_every_slot_absent():
    """Today's gateway (#3042 unimplemented) → all four slots explicitly absent."""
    block = build_authority_block(ABSENT, ABSENT)
    assert block["schema"] == SCHEMA
    assert block["absent"] == sorted(AUDIT_SLOTS)
    assert block["fields"] == {}
    assert block["grants"] == []
    assert block["metadata_keys"] == {"request": None, "response": None}


def test_partial_metadata_names_only_the_missing_slots():
    block = build_authority_block({"backendAuth": {"subject": "u"}}, ABSENT)
    assert block["absent"] == ["idjag_aud", "idjag_jti", "resource_token"]
    assert list(block["fields"]) == ["subject"]


def test_null_valued_key_counts_as_absent():
    """A CEL expression resolving to null is not a reference."""
    block = build_authority_block({"backendAuth": {"subject": None}}, ABSENT)
    assert "subject" in block["absent"]


# ---------------------------------------------------------------------------
# Phase — @howardjohn's ordering objection on #3042
# ---------------------------------------------------------------------------


def test_response_phase_wins_and_is_labelled():
    """Guardrail runs before ID-JAG, so a grant can only appear response-side."""
    req = {"backendAuth": {"subject": "u"}}
    resp = {"backendAuth": {"subject": "u", "idJag": {"jti": "jag-1"}}}
    block = build_authority_block(req, resp)
    assert block["fields"]["subject"]["phase"] == "response"
    assert block["fields"]["idjag_jti"]["phase"] == "response"
    assert block["grants"] == [{"jti": "jag-1"}]


def test_request_only_field_survives_a_response_that_drops_it():
    """A key the response hook's CEL failed on is kept from the request hook."""
    block = build_authority_block({"backendAuth": {"subject": "u"}}, {})
    assert block["fields"]["subject"]["phase"] == "request"


# ---------------------------------------------------------------------------
# Multiplexing — N ID-JAG flows behind one guardrail call (@howardjohn)
# ---------------------------------------------------------------------------


def test_two_grants_behind_one_call_produce_two_entries():
    md = {"backendAuth": {"idJag": {
        "jti": ["jag-1", "jag-2"],
        "audience": ["https://a.example", "https://b.example"],
    }}}
    block = build_authority_block(md, ABSENT)
    assert block["grants"] == [
        {"jti": "jag-1", "aud": "https://a.example"},
        {"jti": "jag-2", "aud": "https://b.example"},
    ]


def test_uneven_grant_lists_do_not_repeat_a_value_across_grants():
    """Two jti, one audience → the second grant carries no audience, not a copy."""
    md = {"backendAuth": {"idJag": {"jti": ["jag-1", "jag-2"], "audience": ["https://a.example"]}}}
    block = build_authority_block(md, ABSENT)
    assert block["grants"] == [{"jti": "jag-1", "aud": "https://a.example"}, {"jti": "jag-2"}]


def test_audience_without_jti_still_produces_a_grant_entry():
    md = {"backendAuth": {"idJag": {"audience": "https://a.example"}}}
    block = build_authority_block(md, ABSENT)
    assert block["grants"] == [{"aud": "https://a.example"}]
    assert "idjag_jti" in block["absent"]


# ---------------------------------------------------------------------------
# Never token material — #3042: "must never expose raw tokens"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,reason", [
    ("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlX2J5dGVz", "jws-compact"),
    ("Bearer abc123def456", "authorization-scheme"),
    ("bearer abc123def456", "authorization-scheme"),
    ("Basic dXNlcjpwYXNz", "authorization-scheme"),
    ("z" * 256, "opaque-long"),
])
def test_token_shaped_values_are_detected(value, reason):
    assert token_shaped(value) == reason


@pytest.mark.parametrize("value", [
    "user@corp.example",
    "https://mcp.atlassian.com",
    "a" * 64,                  # a sha-256 hex digest is a legitimate reference
    "0" * 300,                 # long, but hex — still an identifier
    "jag-01HQ8ZK9",
    12345,
    True,
])
def test_legitimate_references_are_not_flagged(value):
    assert token_shaped(value) is None


def test_token_shaped_value_never_reaches_the_capsule():
    """A misconfigured CEL expression resolving to a JWT is dropped, not sealed."""
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlX2J5dGVz"
    block = build_authority_block({"backendAuth": {"subject": jwt}}, ABSENT)
    assert "subject" in block["redacted"]
    assert "subject" in block["absent"], "a redacted slot is also an absent one"
    assert jwt not in json.dumps(block)


def test_redaction_inside_a_multiplexed_list_keeps_the_safe_members():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlX2J5dGVz"
    md = {"backendAuth": {"idJag": {"jti": ["jag-1", jwt, "jag-2"]}}}
    block = build_authority_block(md, ABSENT)
    assert block["fields"]["idjag_jti"]["value"] == ["jag-1", "jag-2"]
    assert "idjag_jti" in block["redacted"]
    assert jwt not in json.dumps(block)


def test_oversized_value_is_truncated_not_sealed_whole():
    long_hex = "a" * 5000
    block = build_authority_block({"backendAuth": {"subject": long_hex}}, ABSENT)
    assert len(block["fields"]["subject"]["value"]) == 512


def test_nested_object_in_a_scalar_slot_is_not_sealed():
    """A slot that resolves to an object is not an identifier; drop, don't guess."""
    block = build_authority_block({"backendAuth": {"subject": {"sub": "u", "iss": "i"}}}, ABSENT)
    assert "subject" in block["redacted"]
    assert "subject" in block["absent"]


# ---------------------------------------------------------------------------
# Floats — §5.1, and Struct has no integer type
# ---------------------------------------------------------------------------


def test_float_metadata_value_is_canonicalized_not_left_raw():
    """A CEL expression resolving to a non-integral number must not break the seal."""
    md = struct_to_dict(_struct({"backendAuth": {"subject": 2.5}}))
    block = build_authority_block(md, ABSENT)
    assert block["fields"]["subject"]["value"] == "2.5"
    assert isinstance(block["fields"]["subject"]["value"], str)


# ---------------------------------------------------------------------------
# Key remapping — "adapts to his field names in minutes"
# ---------------------------------------------------------------------------


def test_custom_key_map_follows_whatever_names_upstream_picks():
    md = {"xaa": {"grant_id": "jag-9", "principal": "u9"}}
    kmap = normalize_key_map({"idjag_jti": "xaa.grant_id", "subject": ["xaa.principal"]})
    block = build_authority_block(md, ABSENT, key_map=kmap)
    assert block["fields"]["idjag_jti"]["value"] == "jag-9"
    assert block["fields"]["subject"]["value"] == "u9"


def test_key_map_from_env_parses_json():
    kmap = key_map_from_env({KEY_MAP_ENV: '{"subject": "who"}'})
    assert kmap["subject"] == ("who",)
    assert kmap["idjag_jti"] == DEFAULT_KEY_MAP["idjag_jti"], "unlisted slots keep defaults"


@pytest.mark.parametrize("raw", ["not json", '["a"]', '{"bogus_slot": "x"}'])
def test_bad_key_map_env_falls_back_without_raising(raw):
    """A typo in a hurried remap costs that field, never the processor."""
    kmap = key_map_from_env({KEY_MAP_ENV: raw})
    assert kmap["subject"] == DEFAULT_KEY_MAP["subject"]


def test_empty_env_uses_defaults():
    assert key_map_from_env({}) == dict(DEFAULT_KEY_MAP)


# ---------------------------------------------------------------------------
# End-to-end over the real gRPC surface
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def audit_server(tmp_path):
    ledger = tmp_path / "capsules.jsonl"
    port = _free_port()
    servicer = CapsuleEmitServicer(
        operator="test-org", developer="test-agent@v1", ledger=str(ledger), anchor=False
    )
    srv = _make_server(servicer, port, workers=2)
    srv.start()
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
    yield ledger, req, resp
    ch.close()
    srv.stop(grace=0)


def _authority(ledger):
    rec = read_ledger(ledger)[0]
    return rec["model_attestation"]["compute_attestation"]["ext.agentgateway.authority"]


def test_grpc_round_trip_seals_the_authority_block(audit_server):
    """metadata_context on the wire → authority block inside the sealed capsule."""
    ledger, req, resp = audit_server
    req(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["atlassian-mcp"],
        mcp_request=json.dumps({"name": "create_issue", "arguments": {"p": "OPS"}}).encode(),
        metadata_context=_struct({"backendAuth": {"subject": "user@corp.example"}}),
    ))
    resp(ext_mcp_pb2.McpResponse(
        method="tools/call",
        service_names=["atlassian-mcp"],
        mcp_response=b'{"key": "OPS-1"}',
        metadata_context=_struct({"backendAuth": {
            "subject": "user@corp.example",
            "idJag": {"jti": "jag-1", "audience": "https://mcp.atlassian.com"},
            "resourceToken": {"jti": "rt-1"},
        }}),
    ))
    a = _authority(ledger)
    assert a["absent"] == []
    assert a["grants"] == [{"jti": "jag-1", "aud": "https://mcp.atlassian.com"}]
    assert a["fields"]["idjag_jti"]["phase"] == "response"
    assert a["fields"]["subject"]["phase"] == "response"


def test_grpc_round_trip_without_metadata_records_absence(audit_server):
    """Against today's gateway the block is present and says so — not omitted."""
    ledger, req, resp = audit_server
    req(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["b"],
        mcp_request=b'{"name":"t","arguments":{}}',
    ))
    resp(ext_mcp_pb2.McpResponse(method="tools/call", service_names=["b"], mcp_response=b'{}'))
    a = _authority(ledger)
    assert a["absent"] == sorted(AUDIT_SLOTS)
    assert a["metadata_keys"] == {"request": None, "response": None}
    assert a["issue"] == "agentgateway/agentgateway#3042"


def test_sealed_authority_block_verifies_and_is_tamper_evident(audit_server):
    """The block is inside the signed capsule: editing one grant breaks verify."""
    import copy

    from capsule_emit.verification import verify_capsule
    ledger, req, resp = audit_server
    req(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["b"],
        mcp_request=b'{"name":"pay","arguments":{"to":"alice"}}',
        metadata_context=_struct({"backendAuth": {"idJag": {"jti": "jag-1"}}}),
    ))
    resp(ext_mcp_pb2.McpResponse(method="tools/call", service_names=["b"], mcp_response=b'{"ok":true}'))

    rec = read_ledger(ledger)[0]
    assert verify_capsule(rec).ok, [f.detail for f in verify_capsule(rec).findings]

    tampered = copy.deepcopy(rec)
    ca = tampered["model_attestation"]["compute_attestation"]
    ca["ext.agentgateway.authority"]["grants"][0]["jti"] = "jag-FORGED"
    assert not verify_capsule(tampered).ok, "authority chain must be covered by the seal"


def test_float_tool_argument_still_seals_over_grpc(audit_server):
    """agentgateway's own guardrails tests round-trip float tool args (`ratio: 2.5`).

    Before canonicalization the adapter passed the raw float to the digest
    layer, FloatInDigestError was swallowed by the servicer's catch-all, and the
    call produced no capsule at all — a silent hole in the ledger.
    """
    ledger, req, resp = audit_server
    req(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["b"],
        mcp_request=b'{"name":"resize","arguments":{"ratio":2.5}}',
    ))
    resp(ext_mcp_pb2.McpResponse(
        method="tools/call", service_names=["b"], mcp_response=b'{"scaled":1.5}',
    ))
    records = read_ledger(ledger)
    assert len(records) == 1, "a float tool argument must not silently drop the capsule"
    from capsule_emit.verification import verify_capsule
    assert verify_capsule(records[0]).ok


def test_headers_on_the_wire_are_never_read_into_the_capsule(audit_server):
    """`requestHeaders.allowed: []` forwards every header — including authorization.

    The adapter must not put any of it in a durable record.
    """
    ledger, req, resp = audit_server
    secret = "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1In0.c2ln"
    req(ext_mcp_pb2.McpRequest(
        method="tools/call",
        service_names=["b"],
        mcp_request=b'{"name":"t","arguments":{}}',
        headers=[ext_mcp_pb2.McpHeader(key="authorization", value=secret.encode())],
    ))
    resp(ext_mcp_pb2.McpResponse(method="tools/call", service_names=["b"], mcp_response=b'{}'))
    assert secret not in json.dumps(read_ledger(ledger)[0])


def test_servicer_honours_a_custom_key_map_over_grpc(tmp_path):
    """Remapping to whatever names #3042 lands on is a constructor argument."""
    ledger = tmp_path / "l.jsonl"
    port = _free_port()
    servicer = CapsuleEmitServicer(
        operator="o", developer="d@v1", ledger=str(ledger), anchor=False,
        audit_keys={"idjag_jti": "xaa.grant_id"},
    )
    srv = _make_server(servicer, port, workers=2)
    srv.start()
    ch = grpc.insecure_channel(f"localhost:{port}")
    try:
        ch.unary_unary(
            "/agentgateway.dev.ext_mcp.ExtMcp/CheckRequest",
            request_serializer=ext_mcp_pb2.McpRequest.SerializeToString,
            response_deserializer=ext_mcp_pb2.McpRequestResult.FromString,
        )(ext_mcp_pb2.McpRequest(
            method="tools/call", service_names=["b"],
            mcp_request=b'{"name":"t","arguments":{}}',
            metadata_context=_struct({"xaa": {"grant_id": "jag-9"}}),
        ))
        ch.unary_unary(
            "/agentgateway.dev.ext_mcp.ExtMcp/CheckResponse",
            request_serializer=ext_mcp_pb2.McpResponse.SerializeToString,
            response_deserializer=ext_mcp_pb2.McpResponseResult.FromString,
        )(ext_mcp_pb2.McpResponse(method="tools/call", service_names=["b"], mcp_response=b'{}'))
    finally:
        ch.close()
        srv.stop(grace=0)
    a = read_ledger(ledger)[0]["model_attestation"]["compute_attestation"]["ext.agentgateway.authority"]
    assert a["grants"] == [{"jti": "jag-9"}]
