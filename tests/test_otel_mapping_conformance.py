# SPDX-License-Identifier: Apache-2.0
"""Conformance of the ``org.agentactioncapsule.otel`` block with the OTel
mapping profile: the semconv table against the pinned conventions commit, the
conditional rows, ``span_name``, W3C ID validity, the block's closed field
set, and one negative test per must-not-enter rule.

The negative tests run the full ``process_span_facts`` path in its most
permissive configuration (``clear_trace_context=True`` and every conditional
row admitted) and scan the raw ledger bytes for the planted value and for its
SHA-256: the rules hold regardless of tier, so neither form may appear.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from capsule_emit.otel.allowlist import (
    BLOCK_FIELDS,
    DEFAULT_SEMCONV_SOURCE,
    RESOURCE_ATTRS,
    SEMCONV_ATTRS,
    Tier,
)
from capsule_emit.otel.block import OTEL_BLOCK_KEY, build_otel_block
from capsule_emit.otel.processor import SpanFacts, process_span_facts
from capsule_emit.verification import verify_capsule

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
PARENT_SPAN_ID = "0102030405060708"
SENTINEL = "sentinel-value-7f3e9a1c"
SENTINEL_DIGEST = hashlib.sha256(SENTINEL.encode()).hexdigest()

#: Every attribute key defined in ``model/gen-ai/registry.yaml`` and
#: ``model/mcp/registry.yaml`` of open-telemetry/semantic-conventions-genai at
#: commit 8c1b98a376e91d422726ccf33bef051fd9ce4b25 (the commit
#: ``DEFAULT_SEMCONV_SOURCE`` names). ``gen_ai.prompt.variable`` is a template
#: attribute: its keys are ``gen_ai.prompt.variable.<name>``.
REGISTRY_AT_PINNED_COMMIT: frozenset[str] = frozenset(
    {
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.request.max_tokens",
        "gen_ai.request.choice.count",
        "gen_ai.request.temperature",
        "gen_ai.request.top_p",
        "gen_ai.request.top_k",
        "gen_ai.request.stop_sequences",
        "gen_ai.request.frequency_penalty",
        "gen_ai.request.presence_penalty",
        "gen_ai.request.encoding_formats",
        "gen_ai.request.seed",
        "gen_ai.request.stream",
        "gen_ai.request.reasoning.level",
        "gen_ai.request.previous_response.id",
        "gen_ai.request.stream_cursor",
        "gen_ai.response.id",
        "gen_ai.response.model",
        "gen_ai.response.finish_reasons",
        "gen_ai.response.status",
        "gen_ai.response.time_to_first_chunk",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.reasoning.output_tokens",
        "gen_ai.token.type",
        "gen_ai.conversation.id",
        "gen_ai.conversation.compacted",
        "gen_ai.agent.id",
        "gen_ai.agent.name",
        "gen_ai.agent.description",
        "gen_ai.agent.version",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "gen_ai.tool.description",
        "gen_ai.tool.type",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.tool.definitions",
        "gen_ai.data_source.id",
        "gen_ai.operation.name",
        "gen_ai.output.type",
        "gen_ai.embeddings.dimension.count",
        "gen_ai.retrieval.documents",
        "gen_ai.retrieval.query.text",
        "gen_ai.retrieval.top_k",
        "gen_ai.memory.store.id",
        "gen_ai.memory.record.id",
        "gen_ai.memory.record.count",
        "gen_ai.memory.query.text",
        "gen_ai.memory.records",
        "gen_ai.system_instructions",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.evaluation.name",
        "gen_ai.evaluation.score.value",
        "gen_ai.evaluation.score.label",
        "gen_ai.evaluation.explanation",
        "gen_ai.prompt.name",
        "gen_ai.prompt.version",
        "gen_ai.prompt.variable.user_name",
        "gen_ai.workflow.name",
        "mcp.method.name",
        "mcp.session.id",
        "mcp.resource.uri",
        "mcp.protocol.version",
    }
)

CONDITIONAL_KEYS = frozenset(k for k, t in SEMCONV_ATTRS.items() if t is Tier.CLEAR_SAFE_CONDITIONAL)

#: One planted key per must-not-enter row of the mapping: content, template
#: variables, end-user and session identity, and the out-of-scope evaluation
#: and metric families.
MUST_NOT_ENTER_ATTRIBUTES = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "gen_ai.tool.definitions",
    "gen_ai.memory.records",
    "gen_ai.memory.query.text",
    "gen_ai.retrieval.query.text",
    "gen_ai.retrieval.documents",
    "gen_ai.prompt.variable.user_name",
    "gen_ai.prompt.variable.account_number",
    "gen_ai.conversation.id",
    "mcp.session.id",
    "session_id",
    "enduser.id",
    "enduser.pseudo.id",
    "user.id",
    "user.email",
    "gen_ai.evaluation.name",
    "gen_ai.evaluation.score.value",
    "gen_ai.evaluation.score.label",
    "gen_ai.evaluation.explanation",
    "gen_ai.client.token.usage",
    "gen_ai.server.request.duration",
    "mcp.client.session.duration",
)

#: Resource attributes that identify a machine, process instance, device or
#: person. The closed resource subset admits none of them, clear or digested.
INSTANCE_RESOURCE_ATTRIBUTES = (
    "service.instance.id",
    "host.name",
    "host.id",
    "host.mac",
    "host.ip",
    "container.id",
    "k8s.pod.uid",
    "process.pid",
    "device.id",
    "enduser.id",
)


def _facts(**overrides):
    base = dict(
        name="write_order",
        attributes={"http.request.method": "POST"},
        resource_attributes={"service.name": "checkout"},
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        parent_span_id=PARENT_SPAN_ID,
        trace_flags="01",
        tracestate=None,
        status_is_error=False,
        baggage=None,
    )
    base.update(overrides)
    return SpanFacts(**base)


def _seal_permissive(tmp_path, facts, **kwargs):
    ledger = tmp_path / "l.jsonl"
    result = process_span_facts(
        facts,
        operator="acme",
        developer="agent@v1",
        ledger=str(ledger),
        clear_trace_context=True,
        admit_conditional=CONDITIONAL_KEYS,
        **kwargs,
    )
    assert result is not None
    return result, ledger.read_bytes()


# ---------------------------------------------------------------------------
# the semconv table against the pinned conventions commit
# ---------------------------------------------------------------------------


def test_default_semconv_source_names_the_pinned_commit():
    assert DEFAULT_SEMCONV_SOURCE == "open-telemetry/semantic-conventions-genai@8c1b98a"


def test_every_allowlisted_semconv_name_exists_at_the_pinned_commit():
    missing = sorted(set(SEMCONV_ATTRS) - REGISTRY_AT_PINNED_COMMIT)
    assert missing == []


def test_cache_creation_counter_is_admitted_and_the_nonexistent_cache_write_is_not():
    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        attributes={
            "gen_ai.usage.cache_creation.input_tokens": 64,
            "gen_ai.usage.cache_read.input_tokens": 32,
            "gen_ai.usage.cache_write.input_tokens": 99,
        },
    )
    assert block["semconv"]["gen_ai.usage.cache_creation.input_tokens"] == 64
    assert block["semconv"]["gen_ai.usage.cache_read.input_tokens"] == 32
    assert "gen_ai.usage.cache_write.input_tokens" not in block["semconv"]


@pytest.mark.parametrize("key", sorted(REGISTRY_AT_PINNED_COMMIT - set(SEMCONV_ATTRS)))
def test_registry_attribute_the_mapping_does_not_admit_is_dropped(key):
    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        attributes={key: SENTINEL},
        clear_trace_context=True,
        admit_conditional=CONDITIONAL_KEYS | {key},
    )
    rendered = json.dumps(block)
    assert key not in rendered
    assert SENTINEL not in rendered
    assert SENTINEL_DIGEST not in rendered


# ---------------------------------------------------------------------------
# clear-safe, conditional rows
# ---------------------------------------------------------------------------


def test_the_conditional_rows_are_exactly_the_six_the_mapping_names():
    assert CONDITIONAL_KEYS == {
        "gen_ai.workflow.name",
        "gen_ai.tool.call.id",
        "gen_ai.data_source.id",
        "gen_ai.memory.store.id",
        "gen_ai.prompt.name",
        "gen_ai.prompt.version",
    }


@pytest.mark.parametrize("key", sorted(CONDITIONAL_KEYS))
def test_conditional_row_is_omitted_by_default(key):
    block = build_otel_block("op", trace_id=TRACE_ID, span_id=SPAN_ID, attributes={key: SENTINEL})
    rendered = json.dumps(block)
    assert SENTINEL not in rendered
    assert SENTINEL_DIGEST not in rendered


@pytest.mark.parametrize("key", sorted(CONDITIONAL_KEYS))
def test_conditional_row_is_carried_clear_when_admitted_by_name(key):
    block = build_otel_block(
        "op", trace_id=TRACE_ID, span_id=SPAN_ID, attributes={key: SENTINEL}, admit_conditional=frozenset({key})
    )
    assert block["semconv"][key] == SENTINEL


def test_admitting_one_conditional_row_does_not_admit_another():
    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        attributes={"gen_ai.workflow.name": "refund", "gen_ai.tool.call.id": SENTINEL},
        admit_conditional=frozenset({"gen_ai.workflow.name"}),
    )
    assert block["semconv"]["gen_ai.workflow.name"] == "refund"
    assert SENTINEL not in json.dumps(block)


def test_admit_conditional_cannot_admit_a_must_not_enter_key():
    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        attributes={"gen_ai.input.messages": SENTINEL},
        admit_conditional=frozenset({"gen_ai.input.messages"}),
    )
    assert SENTINEL not in json.dumps(block)


def test_admit_conditional_does_not_clear_a_digest_only_key():
    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        attributes={"gen_ai.response.id": SENTINEL},
        admit_conditional=frozenset({"gen_ai.response.id"}),
    )
    assert block["semconv"]["gen_ai.response.id"] == SENTINEL_DIGEST


# ---------------------------------------------------------------------------
# span_name, W3C ID validity, the closed field set
# ---------------------------------------------------------------------------


def test_span_name_is_omitted_without_clear_trace_context():
    block = build_otel_block("write_order", trace_id=TRACE_ID, span_id=SPAN_ID)
    assert "span_name" not in block


def test_span_name_is_carried_clear_with_clear_trace_context():
    block = build_otel_block("write_order", trace_id=TRACE_ID, span_id=SPAN_ID, clear_trace_context=True)
    assert block["span_name"] == "write_order"


@pytest.mark.parametrize(
    "field,kwargs",
    [
        ("trace_id", {"trace_id": "0" * 32, "span_id": SPAN_ID}),
        ("span_id", {"trace_id": TRACE_ID, "span_id": "0" * 16}),
        ("parent_span_id", {"trace_id": TRACE_ID, "span_id": SPAN_ID, "parent_span_id": "0" * 16}),
    ],
)
def test_all_zero_w3c_ids_are_rejected(field, kwargs):
    with pytest.raises(ValueError, match=field):
        build_otel_block("op", **kwargs)


def test_uppercase_ids_are_normalized_to_lowercase():
    block = build_otel_block("op", trace_id=TRACE_ID.upper(), span_id=SPAN_ID.upper(), clear_trace_context=True)
    assert block["trace_id"] == TRACE_ID
    assert block["span_id"] == SPAN_ID


def test_block_carries_only_the_mapping_fields_even_fully_populated(tmp_path):
    attributes = {key: SENTINEL for key in REGISTRY_AT_PINNED_COMMIT}
    attributes["http.request.method"] = "POST"
    attributes["some.vendor.attribute"] = SENTINEL
    resource = {key: SENTINEL for key in (*RESOURCE_ATTRS, *INSTANCE_RESOURCE_ATTRIBUTES)}
    result, _ = _seal_permissive(
        tmp_path,
        _facts(attributes=attributes, resource_attributes=resource, tracestate="vendor=opaque"),
    )
    block = result.capsule["model_attestation"]["compute_attestation"][OTEL_BLOCK_KEY]
    assert set(block) <= BLOCK_FIELDS
    assert set(block["resource"]) == set(RESOURCE_ATTRS)
    assert set(block["semconv"]) - {"source"} <= set(SEMCONV_ATTRS)
    assert verify_capsule(result.capsule).ok


# ---------------------------------------------------------------------------
# must-not-enter rules: negative tests on the sealed ledger bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", MUST_NOT_ENTER_ATTRIBUTES)
def test_must_not_enter_attribute_never_reaches_the_ledger_clear_or_digested(tmp_path, key):
    _, ledger_bytes = _seal_permissive(
        tmp_path, _facts(attributes={"http.request.method": "POST", key: SENTINEL})
    )
    assert SENTINEL.encode() not in ledger_bytes
    assert SENTINEL_DIGEST.encode() not in ledger_bytes
    assert key.encode() not in ledger_bytes


@pytest.mark.parametrize("key", INSTANCE_RESOURCE_ATTRIBUTES)
def test_instance_or_person_resource_attribute_never_reaches_the_ledger(tmp_path, key):
    _, ledger_bytes = _seal_permissive(
        tmp_path, _facts(resource_attributes={"service.name": "checkout", key: SENTINEL})
    )
    assert SENTINEL.encode() not in ledger_bytes
    assert SENTINEL_DIGEST.encode() not in ledger_bytes
    assert key.encode() not in ledger_bytes


def test_tracestate_never_reaches_the_ledger_in_clear(tmp_path):
    tracestate = f"vendor={SENTINEL}"
    result, ledger_bytes = _seal_permissive(tmp_path, _facts(tracestate=tracestate))
    assert SENTINEL.encode() not in ledger_bytes
    block = result.capsule["model_attestation"]["compute_attestation"][OTEL_BLOCK_KEY]
    assert block["tracestate_digest"] == hashlib.sha256(tracestate.encode()).hexdigest()


def test_baggage_never_reaches_the_ledger_without_an_outcome_context_allowlist(tmp_path):
    _, ledger_bytes = _seal_permissive(tmp_path, _facts(baggage={"user.id": SENTINEL, "tenant": SENTINEL}))
    assert SENTINEL.encode() not in ledger_bytes
    assert SENTINEL_DIGEST.encode() not in ledger_bytes


def test_baggage_never_enters_the_otel_block_even_when_allowlisted_for_outcome_context(tmp_path):
    result, _ = _seal_permissive(
        tmp_path,
        _facts(baggage={"exchange.state": SENTINEL}),
        outcome_context_baggage_keys=frozenset({"exchange.state"}),
    )
    rendered = json.dumps(result.capsule["model_attestation"]["compute_attestation"][OTEL_BLOCK_KEY])
    assert SENTINEL not in rendered
    assert SENTINEL_DIGEST not in rendered
