# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.otel -- the OTel processor v0.

Covers, in order: the allow-list tables and their leak-mutant (the acceptance
line's "a mutant that leaks one attribute goes red"), Signal 1 read off a
span (the taxonomy reference implementation this task adds), the
``org.agentactioncapsule.otel`` block builder, outcome-context tagging, the
full ``process_span_facts`` pipeline including the acceptance line's literal
"zero content bytes" ledger scan, and a real OpenTelemetry SDK
round-trip through ``CapsuleOTelSpanExporter``.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from capsule_emit import read_ledger
from capsule_emit.connector import Classification
from capsule_emit.otel.allowlist import (
    NEVER_ENTERS_SEMCONV,
    RESOURCE_ATTRS,
    SEMCONV_ATTRS,
    SPAN_ID_RE,
    TRACE_ID_RE,
    Tier,
)
from capsule_emit.otel.block import (
    OTEL_BLOCK_KEY,
    OUTCOME_CONTEXT_KEY,
    build_otel_block,
    build_outcome_context_block,
)
from capsule_emit.otel.processor import (
    REVERSE_JOIN_ATTRIBUTE,
    CapsuleOTelSpanExporter,
    SpanFacts,
    process_span_facts,
    stamp_reverse_join,
)
from capsule_emit.otel.signal import classify_span_signal_1
from capsule_emit.verification import verify_capsule

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
PARENT_SPAN_ID = "0102030405060708"

# ---------------------------------------------------------------------------
# allowlist
# ---------------------------------------------------------------------------


def test_trace_id_regex_accepts_the_draft_example():
    assert TRACE_ID_RE.match(TRACE_ID)


def test_span_id_regex_rejects_wrong_length():
    assert not SPAN_ID_RE.match("00f0")


def test_never_enters_semconv_keys_are_absent_from_the_allowlist():
    """R4: the allow-list and the never-enters set must never overlap -- if
    they did, tier_for_semconv_attribute would return a real tier for a
    forbidden key and the whole default-deny posture would be silently
    broken."""
    assert SEMCONV_ATTRS.keys().isdisjoint(NEVER_ENTERS_SEMCONV)


def test_never_enters_semconv_prefix_family_has_no_matching_allowlist_key():
    """The exact-key disjointness test above says nothing about the PREFIX
    family (gen_ai.prompt.variable.*/enduser.*/user.*, allowlist.py's
    NEVER_ENTERS_SEMCONV_PREFIXES) -- a future SEMCONV_ATTRS entry like
    "user.email" would pass the exact-set check above while still being a
    never-enters row under its prefix form. §7b cold review flagged this
    exact gap."""
    from capsule_emit.otel.allowlist import NEVER_ENTERS_SEMCONV_PREFIXES

    for key in SEMCONV_ATTRS:
        assert not key.startswith(NEVER_ENTERS_SEMCONV_PREFIXES), (
            f"{key!r} is allow-listed but matches a never-enters prefix"
        )


def test_resource_attrs_never_include_the_drafts_should_omit_identifiers():
    for identifier in ("service.instance.id", "host.name", "host.id", "container.id"):
        assert identifier not in RESOURCE_ATTRS


# ---------------------------------------------------------------------------
# signal.classify_span_signal_1 -- docs/whats-consequential.md worked examples
# ---------------------------------------------------------------------------


def test_db_select_is_observation():
    """Worked example 3."""
    event = classify_span_signal_1("query", {"db.operation.name": "SELECT"})
    assert event is Classification.OBSERVATION


def test_db_insert_is_effect_regardless_of_any_ancestor_span():
    """Worked example 4's point, exercised at the unit level:
    ``classify_span_signal_1`` takes one span's own name+attributes and
    nothing else -- no parent parameter exists on this function -- so an
    INSERT span classifies EFFECT purely from its own attributes. This test
    does not construct an actual parent/child span pair (nothing here could
    -- the function has no way to see one); it documents that the isolation
    worked example 4 relies on is structural, not merely behavioral."""
    child = classify_span_signal_1("insert_row", {"db.operation.name": "INSERT"})
    assert child is Classification.EFFECT


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_http_request_method_safe_verbs_are_observation(method):
    assert classify_span_signal_1("h", {"http.request.method": method}) is Classification.OBSERVATION


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_http_request_method_unsafe_verbs_are_effect(method):
    assert classify_span_signal_1("h", {"http.request.method": method}) is Classification.EFFECT


def test_http_request_method_takes_priority_over_db_operation():
    attrs = {"http.request.method": "GET", "db.operation.name": "INSERT"}
    assert classify_span_signal_1("h", attrs) is Classification.OBSERVATION


@pytest.mark.parametrize("op", ["create", "send", "settle"])
def test_messaging_write_ops_are_effect(op):
    assert classify_span_signal_1("m", {"messaging.operation.type": op}) is Classification.EFFECT


@pytest.mark.parametrize("op", ["receive", "process"])
def test_messaging_read_ops_are_observation(op):
    assert classify_span_signal_1("m", {"messaging.operation.type": op}) is Classification.OBSERVATION


def test_rpc_method_unambiguous_verbs():
    assert classify_span_signal_1("r", {"rpc.method": "CreateOrder"}) is Classification.EFFECT
    assert classify_span_signal_1("r", {"rpc.method": "GetOrder"}) is Classification.OBSERVATION


def test_rpc_method_ambiguous_name_falls_to_fail_safe():
    assert classify_span_signal_1("r", {"rpc.method": "Frobnicate"}) is Classification.EFFECT


def test_no_recognized_attribute_present_is_fail_safe_effect():
    """The taxonomy doc's "span kind CLIENT alone, no attribute above
    present" row -- ``classify_span_signal_1`` does not read span kind at
    all (it has no parameter for it); an empty attributes dict is the
    closest this function's own inputs get to that row, and is the actual
    case this test exercises. R4: the mutant this guards is a future change
    that treats a bare/unrecognized span as evidence of a read. Flip
    commit_step_present's caller to default False instead of None and this
    test goes red."""
    assert classify_span_signal_1("mystery", {}) is Classification.EFFECT


# ---------------------------------------------------------------------------
# block.build_otel_block
# ---------------------------------------------------------------------------


def test_trace_and_span_ids_default_to_digest_only():
    block = build_otel_block("op", trace_id=TRACE_ID, span_id=SPAN_ID)
    assert block["trace_id"] != TRACE_ID
    assert block["trace_id"] == hashlib.sha256(TRACE_ID.encode()).hexdigest()
    assert block["span_id"] == hashlib.sha256(SPAN_ID.encode()).hexdigest()


def test_clear_trace_context_opt_in_emits_literal_ids():
    block = build_otel_block("op", trace_id=TRACE_ID, span_id=SPAN_ID, clear_trace_context=True)
    assert block["trace_id"] == TRACE_ID
    assert block["span_id"] == SPAN_ID


def test_malformed_trace_id_raises():
    with pytest.raises(ValueError):
        build_otel_block("op", trace_id="not-hex", span_id=SPAN_ID)


def test_tracestate_is_always_digested_even_with_clear_trace_context():
    block = build_otel_block(
        "op", trace_id=TRACE_ID, span_id=SPAN_ID, tracestate="vendor=opaque-value", clear_trace_context=True
    )
    assert "tracestate" not in block
    assert block["tracestate_digest"] == hashlib.sha256(b"vendor=opaque-value").hexdigest()


def test_parent_span_id_present_only_when_given():
    block = build_otel_block("op", trace_id=TRACE_ID, span_id=SPAN_ID)
    assert "parent_span_id" not in block
    block2 = build_otel_block("op", trace_id=TRACE_ID, span_id=SPAN_ID, parent_span_id=PARENT_SPAN_ID)
    assert block2["parent_span_id"] == hashlib.sha256(PARENT_SPAN_ID.encode()).hexdigest()


def test_resource_subset_drops_unlisted_keys():
    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        resource_attributes={"service.name": "support-agent", "host.name": "box-42.internal"},
    )
    assert block["resource"] == {"service.name": "support-agent"}


def test_semconv_clear_safe_passes_through_and_digest_only_is_hashed():
    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        attributes={
            "gen_ai.provider.name": "local",
            "gen_ai.response.id": "resp-abc123",
            "gen_ai.usage.input_tokens": 412,
        },
    )
    assert block["semconv"]["gen_ai.provider.name"] == "local"
    assert block["semconv"]["gen_ai.usage.input_tokens"] == 412
    assert block["semconv"]["gen_ai.response.id"] == hashlib.sha256(b"resp-abc123").hexdigest()


def test_semconv_unlisted_attribute_is_dropped_default_deny():
    block = build_otel_block(
        "op", trace_id=TRACE_ID, span_id=SPAN_ID, attributes={"some.custom.unlisted.attr": "value"}
    )
    assert "some.custom.unlisted.attr" not in json.dumps(block)


def test_semconv_never_enters_key_is_dropped_by_the_real_allowlist():
    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        attributes={"gen_ai.input.messages": [{"role": "user", "content": "the secret prompt"}]},
    )
    assert "gen_ai.input.messages" not in json.dumps(block)
    assert "secret prompt" not in json.dumps(block)


def test_LEAK_MUTANT_never_enters_key_promoted_to_clear_safe_leaks_through_real_builder():
    """R4, the acceptance line's own words: 'a mutant that leaks one
    attribute goes red'. This constructs the mutant allow-list (one of the
    draft's own never-enters rows, remapped to CLEAR_SAFE) and runs it
    through the REAL, unmodified build_otel_block -- proving the allow-list
    lookup is actually load-bearing, not a check that could never fail.

    Restore-and-reverify: test_semconv_never_enters_key_is_dropped_by_the_real_allowlist
    (above) is the green twin -- same input, the real (unbroken) table,
    asserting no leak."""
    leaky_table = dict(SEMCONV_ATTRS)
    poisoned_key = next(iter(NEVER_ENTERS_SEMCONV))
    leaky_table[poisoned_key] = Tier.CLEAR_SAFE

    block = build_otel_block(
        "op",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        attributes={poisoned_key: "THIS-MUST-NEVER-SURVIVE-A-CORRECT-ALLOWLIST"},
        semconv_table=leaky_table,
    )
    # Red under the mutant table: the forbidden key's value is right there.
    assert block["semconv"][poisoned_key] == "THIS-MUST-NEVER-SURVIVE-A-CORRECT-ALLOWLIST"

    # Green under the real table: same call, no semconv_table override.
    clean_block = build_otel_block(
        "op", trace_id=TRACE_ID, span_id=SPAN_ID, attributes={poisoned_key: "THIS-MUST-NEVER-SURVIVE-A-CORRECT-ALLOWLIST"}
    )
    assert "THIS-MUST-NEVER-SURVIVE-A-CORRECT-ALLOWLIST" not in json.dumps(clean_block)


# ---------------------------------------------------------------------------
# block.build_outcome_context_block -- baggage-derived, allow-list empty by default
# ---------------------------------------------------------------------------


def test_outcome_context_empty_allowlist_tags_nothing():
    """v0's shipped default: no Area 16 design note names real baggage keys
    yet (see the outbox Needs decision), so the allow-list a deployment has
    not configured is empty and this returns None regardless of baggage
    content -- 'do not invent keys' means literally nothing is read."""
    result = build_outcome_context_block({"anything": "value"}, allowed_keys=frozenset())
    assert result is None


def test_outcome_context_only_tags_allowlisted_keys_and_always_digests():
    result = build_outcome_context_block(
        {"exchange.state": "MATCHED", "other.baggage": "ignored"},
        allowed_keys=frozenset({"exchange.state"}),
    )
    assert result == {"exchange.state": hashlib.sha256(b"MATCHED").hexdigest()}
    assert "other.baggage" not in result
    assert "ignored" not in json.dumps(result)


def test_outcome_context_key_shape_is_structurally_separate_from_the_block():
    """build_otel_block has no baggage parameter at all -- it cannot receive
    baggage content even by caller error -- so the key-name assertions here
    are a structural sanity check, not proof of non-contamination on the
    real merge path. See
    test_outcome_context_never_appears_inside_the_real_otel_block below for
    that proof, exercised through process_span_facts (the actual place the
    two blocks are merged into one compute_attestation dict)."""
    otel_block = build_otel_block("op", trace_id=TRACE_ID, span_id=SPAN_ID)
    assert "outcome_context" not in otel_block
    assert OUTCOME_CONTEXT_KEY != OTEL_BLOCK_KEY
    assert not OUTCOME_CONTEXT_KEY.startswith(OTEL_BLOCK_KEY)


def test_outcome_context_never_appears_inside_the_real_otel_block(tmp_path):
    """The draft's own privacy rule, proven on the REAL merge path
    (process_span_facts assembling extra_compute from both
    build_otel_block and build_outcome_context_block): a baggage value
    tagged into ext.otel.outcome_context must never also show up inside
    org.agentactioncapsule.otel, the block the draft forbids baggage from
    entering at all."""
    ledger = tmp_path / "l.jsonl"
    baggage_value = "MARKER-VALUE-THAT-MUST-STAY-OUT-OF-THE-OTEL-BLOCK"
    facts = _facts(baggage={"exchange.state": baggage_value})
    result = process_span_facts(
        facts,
        operator="acme",
        developer="agent@v1",
        ledger=str(ledger),
        outcome_context_baggage_keys=frozenset({"exchange.state"}),
    )
    compute_attestation = result.capsule["model_attestation"]["compute_attestation"]
    # Proves the fixture is actually populated (not a vacuous pass on an
    # empty structure): the tagged digest DOES appear, in the sibling key.
    assert compute_attestation[OUTCOME_CONTEXT_KEY] == {
        "exchange.state": hashlib.sha256(baggage_value.encode()).hexdigest()
    }
    otel_block_json = json.dumps(compute_attestation[OTEL_BLOCK_KEY])
    assert "outcome_context" not in otel_block_json
    assert baggage_value not in otel_block_json
    assert hashlib.sha256(baggage_value.encode()).hexdigest() not in otel_block_json


# ---------------------------------------------------------------------------
# processor.process_span_facts
# ---------------------------------------------------------------------------


def _facts(**overrides):
    base = dict(
        name="write_order",
        attributes={"http.request.method": "POST"},
        resource_attributes={"service.name": "checkout"},
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        parent_span_id=None,
        trace_flags="01",
        tracestate=None,
        status_is_error=False,
        baggage=None,
    )
    base.update(overrides)
    return SpanFacts(**base)


def test_observation_span_is_not_sealed(tmp_path):
    ledger = tmp_path / "l.jsonl"
    facts = _facts(name="read_order", attributes={"http.request.method": "GET"})
    result = process_span_facts(facts, operator="acme", developer="agent@v1", ledger=str(ledger))
    assert result is None
    assert not ledger.exists() or list(read_ledger(ledger)) == []


def test_effect_span_is_sealed_with_the_otel_block(tmp_path):
    ledger = tmp_path / "l.jsonl"
    facts = _facts()
    result = process_span_facts(facts, operator="acme", developer="agent@v1", ledger=str(ledger))
    assert result is not None
    otel_block = result.capsule["model_attestation"]["compute_attestation"][OTEL_BLOCK_KEY]
    assert otel_block["trace_id"] == hashlib.sha256(TRACE_ID.encode()).hexdigest()
    assert result.capsule["effect"]["status"] == "confirmed"
    assert verify_capsule(result.capsule).ok


def test_REGRESSION_span_name_never_reaches_action_id_or_effect_type_in_clear(tmp_path):
    """§7b cold review finding: an earlier version passed the raw span name
    straight through as action_id/effect.type -- both plain, undigested
    fields -- regardless of clear_trace_context, while the identical name
    was correctly gated inside otel_block["span_name"]. This is exactly the
    draft's "clear-safe, conditional ... never user-derived" span-name
    warning: real OTel instrumentations do put dynamic data in span names
    (e.g. an unrendered HTTP route). Fixed in processor.py's action_label;
    this pins the fix."""
    ledger = tmp_path / "l.jsonl"
    sensitive_name = "process_order_for_jane.doe@example.com"
    facts = _facts(name=sensitive_name)
    result = process_span_facts(facts, operator="acme", developer="agent@v1", ledger=str(ledger))
    assert "jane.doe" not in result.capsule["action_id"]
    assert "jane.doe" not in result.capsule["effect"]["type"]
    assert "jane.doe" not in ledger.read_bytes().decode()

    digested = hashlib.sha256(sensitive_name.encode()).hexdigest()
    assert result.capsule["effect"]["type"] == digested

    # Opt-in: clear_trace_context=True is the explicit escape hatch, and the
    # name legitimately appears there -- proves this isn't just "the name
    # never appears," it's "the name is gated by the same flag everywhere."
    ledger2 = tmp_path / "l2.jsonl"
    facts2 = _facts(name=sensitive_name)
    result2 = process_span_facts(
        facts2, operator="acme", developer="agent@v1", ledger=str(ledger2), clear_trace_context=True
    )
    assert result2.capsule["effect"]["type"] == sensitive_name
    assert sensitive_name in result2.capsule["action_id"]


def test_error_status_span_seals_failed_effect(tmp_path):
    ledger = tmp_path / "l.jsonl"
    facts = _facts(status_is_error=True)
    result = process_span_facts(facts, operator="acme", developer="agent@v1", ledger=str(ledger))
    assert result.capsule["effect"]["status"] == "failed"


def test_malformed_span_ids_warn_and_return_none_never_raise(tmp_path):
    ledger = tmp_path / "l.jsonl"
    facts = _facts(trace_id="not-valid-hex")
    with pytest.warns(RuntimeWarning):
        result = process_span_facts(facts, operator="acme", developer="agent@v1", ledger=str(ledger))
    assert result is None


def test_ACCEPTANCE_prompt_content_never_reaches_the_ledger_bytes(tmp_path):
    """The acceptance line, verbatim: 'a real trace with prompt/completion
    content in span attributes yields records with zero content bytes
    (asserted by scanning the ledger for any 12-char substring of the
    prompt)'."""
    prompt = (
        "The quarterly revenue figures for the northeast region were significantly "
        "higher than projected, driven primarily by the enterprise software renewals."
    )
    ledger = tmp_path / "l.jsonl"
    facts = _facts(
        attributes={
            "http.request.method": "POST",
            "gen_ai.input.messages": [{"role": "user", "content": prompt}],
            "gen_ai.system_instructions": "You are a helpful financial analyst assistant.",
            "gen_ai.tool.call.arguments": json.dumps({"query": prompt}),
            "gen_ai.provider.name": "local",  # a clear-safe control: this one SHOULD survive
        }
    )
    result = process_span_facts(facts, operator="acme", developer="agent@v1", ledger=str(ledger))
    assert result is not None

    raw_ledger_bytes = ledger.read_bytes()
    for start in range(0, len(prompt) - 12, 4):
        substring = prompt[start : start + 12]
        assert substring.encode() not in raw_ledger_bytes, f"leaked prompt substring: {substring!r}"
    assert b"financial analyst" not in raw_ledger_bytes

    # The control clear-safe field DID survive -- proves the scan above is
    # actually exercising a populated ledger, not silently passing on an
    # empty/failed write.
    assert b"local" in raw_ledger_bytes


def test_outcome_context_baggage_key_round_trips_through_a_sealed_capsule(tmp_path):
    ledger = tmp_path / "l.jsonl"
    facts = _facts(baggage={"exchange.state": "MATCHED"})
    result = process_span_facts(
        facts,
        operator="acme",
        developer="agent@v1",
        ledger=str(ledger),
        outcome_context_baggage_keys=frozenset({"exchange.state"}),
    )
    tagged = result.capsule["model_attestation"]["compute_attestation"][OUTCOME_CONTEXT_KEY]
    assert tagged == {"exchange.state": hashlib.sha256(b"MATCHED").hexdigest()}


# ---------------------------------------------------------------------------
# processor.stamp_reverse_join
# ---------------------------------------------------------------------------


class _FakeLiveSpan:
    def __init__(self):
        self.attributes: dict[str, str] = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value


def test_stamp_reverse_join_sets_the_provisional_attribute_name():
    span = _FakeLiveSpan()
    stamp_reverse_join(span, "a" * 64)
    assert span.attributes[REVERSE_JOIN_ATTRIBUTE] == "a" * 64
    assert REVERSE_JOIN_ATTRIBUTE == "aac.capsule_id"


# ---------------------------------------------------------------------------
# CapsuleOTelSpanExporter -- real OpenTelemetry SDK round-trip
# ---------------------------------------------------------------------------

otel_sdk = pytest.importorskip("opentelemetry.sdk.trace")


def test_exporter_seals_a_real_readable_span(tmp_path):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import Status, StatusCode

    ledger = tmp_path / "l.jsonl"
    exporter = CapsuleOTelSpanExporter(operator="acme", developer="agent@v1", ledger=str(ledger))
    provider = TracerProvider(resource=Resource.create({"service.name": "otel-dogfood-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("capsule-emit-otel-test")

    with tracer.start_as_current_span("write_record") as span:
        span.set_attribute("http.request.method", "POST")
        span.set_attribute("gen_ai.provider.name", "local")
        span.set_attribute("gen_ai.input.messages", "must never appear in the ledger")
        span.set_status(Status(StatusCode.OK))

    provider.shutdown()

    entries = list(read_ledger(ledger))
    assert len(entries) == 1
    otel_block = entries[0]["model_attestation"]["compute_attestation"][OTEL_BLOCK_KEY]
    assert otel_block["semconv"]["gen_ai.provider.name"] == "local"
    assert b"must never appear in the ledger" not in ledger.read_bytes()
    assert entries[0]["effect"]["status"] == "confirmed"


def test_exporter_does_not_seal_a_read_only_real_span(tmp_path):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    ledger = tmp_path / "l.jsonl"
    exporter = CapsuleOTelSpanExporter(operator="acme", developer="agent@v1", ledger=str(ledger))
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("capsule-emit-otel-test")

    with tracer.start_as_current_span("read_record") as span:
        span.set_attribute("http.request.method", "GET")

    provider.shutdown()

    assert not ledger.exists() or list(read_ledger(ledger)) == []
