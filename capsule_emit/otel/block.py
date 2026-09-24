# SPDX-License-Identifier: Apache-2.0
"""Build the ``org.agentactioncapsule.otel`` block and the sibling
outcome-context block -- draft-palanisamy-scitt-aac-otel-00's "The
`org.agentactioncapsule.otel` Block" section, plus this task's outcome-context
tagging requirement kept deliberately OUTSIDE that block (see
:func:`build_outcome_context_block`'s docstring for why).

**Where the block lives in v0.** The draft frames `org.agentactioncapsule.otel`
as "a top-level payload member" of the Capsule, parallel to `action_id` /
`operator` / etc. -- but nothing in the currently-installed
`agent_action_capsule.emit()` (spec -04, format_version "4") accepts an
arbitrary top-level member; only `compute_attestation` (nested under
`model_attestation`) is extensible today, exactly the container
`docs/extensions/mcp-toolset-digest.md`'s `ext.mcp` already uses. v0 places
this block there too: `model_attestation.compute_attestation
["org.agentactioncapsule.otel"]`, via `_emit_capsule(extra_compute=...)`. The
draft's own editor note anticipates this ("AAC -05 is expected to state the
two extension containers explicitly ... Nothing here changes if it does") --
the field names, tiers, and shape below are unaffected by which container the
base profile eventually settles on; only the JSON path changes.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import TypedDict

from .allowlist import (
    DEFAULT_SEMCONV_SOURCE,
    RESOURCE_ATTRS,
    SPAN_ID_RE,
    TRACE_FLAGS_RE,
    TRACE_ID_RE,
    Tier,
    tier_for_semconv_attribute,
)
from .attributes import SpanAttributeValue

__all__ = [
    "OTEL_BLOCK_KEY",
    "OUTCOME_CONTEXT_KEY",
    "OTelBlock",
    "build_otel_block",
    "build_outcome_context_block",
]


class OTelBlock(TypedDict, total=False):
    """The ``org.agentactioncapsule.otel`` block's own field set -- the
    draft's field table, transcribed as keys. ``total=False``: every field is
    OPTIONAL per the draft except ``trace_id``/``span_id`` (REQUIRED, but
    typed the same way here since the required-ness is enforced by
    :func:`build_otel_block` always setting them, not by the type checker).
    """

    trace_id: str
    span_id: str
    parent_span_id: str
    trace_flags: str
    tracestate_digest: str
    span_name: str
    resource: dict[str, SpanAttributeValue]
    semconv: dict[str, SpanAttributeValue]

#: The draft's namespaced payload-member name, reused here as the
#: `compute_attestation` key -- see the module docstring's "Where the block
#: lives in v0" note.
OTEL_BLOCK_KEY = "org.agentactioncapsule.otel"

#: A SIBLING key, never nested inside ``OTEL_BLOCK_KEY``. The draft's Privacy
#: Considerations section is explicit: "no field of `org.agentactioncapsule.otel`
#: MAY carry: ... OpenTelemetry baggage entries" -- unconditionally, clear or
#: digested. Outcome-context tagging is baggage-derived by this task's own Do
#: line, so it cannot live inside that block without violating the draft it
#: is required to conform to. Naming follows this repo's existing `ext.*`
#: convention for compute_attestation extension keys (`ext.mcp`,
#: `ext.agentgateway.*`).
OUTCOME_CONTEXT_KEY = "ext.otel.outcome_context"


def _sha256_hex(value: SpanAttributeValue) -> str:
    """SHA-256 over *value* "as received" (the draft's own phrasing for
    ``tracestate_digest``) -- raw UTF-8 bytes of ``str(value)``, not this
    library's JCS-canonicalized ``agent_input_digest``/``core._digest``. The
    two digest algorithms are deliberately different: everything in this
    block digests a single scalar exactly as the span carried it, never a
    JSON structure needing canonical field order.
    """
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _validated_hex(value: str, pattern: re.Pattern[str], field: str) -> str:
    normalized = value.strip().lower()
    if not pattern.match(normalized):
        raise ValueError(
            f"otel block field {field!r} must match {pattern.pattern!r} "
            f"(32/16/2 lowercase hex per W3C Trace Context) -- got {value!r}"
        )
    return normalized


def _resource_subset(
    resource_attributes: Mapping[str, SpanAttributeValue] | None,
) -> dict[str, SpanAttributeValue]:
    if not resource_attributes:
        return {}
    return {
        key: value
        for key, value in resource_attributes.items()
        if key in RESOURCE_ATTRS and RESOURCE_ATTRS[key] is Tier.CLEAR_SAFE
    }


def _semconv_subset(
    attributes: Mapping[str, SpanAttributeValue] | None, *, table: dict[str, Tier], source: str
) -> dict[str, SpanAttributeValue]:
    semconv: dict[str, SpanAttributeValue] = {"source": source}
    if not attributes:
        return semconv
    for key, value in attributes.items():
        tier = tier_for_semconv_attribute(key, table)
        if tier is Tier.CLEAR_SAFE:
            semconv[key] = value
        elif tier is Tier.DIGEST_ONLY:
            semconv[key] = _sha256_hex(value)
        # tier is None (not allow-listed) or NEVER_ENTERS (never a value in
        # `table` under normal operation -- see allowlist.NEVER_ENTERS_SEMCONV's
        # docstring): the key is silently dropped either way. Default-deny,
        # not a per-key exception list.
    return semconv


def build_otel_block(
    span_name: str,
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None = None,
    trace_flags: str | None = None,
    tracestate: str | None = None,
    attributes: Mapping[str, SpanAttributeValue] | None = None,
    resource_attributes: Mapping[str, SpanAttributeValue] | None = None,
    clear_trace_context: bool = False,
    semconv_table: dict[str, Tier] | None = None,
    semconv_source: str = DEFAULT_SEMCONV_SOURCE,
) -> OTelBlock:
    """The ``org.agentactioncapsule.otel`` block for one span.

    *trace_id*/*span_id*/*parent_span_id* are the raw lowercase-hex W3C IDs;
    validated then either passed through clear (only when
    *clear_trace_context* is ``True`` -- see ``allowlist``'s module
    docstring for why v0 defaults this off) or reduced to
    ``SHA-256(id)``, in which case the field name keeps its `_id` suffix
    (`digest_only` values below are still keyed by the same field
    name the draft gives the clear form -- there is no separate
    `trace_id_digest` field; the shape of the value, not the key, carries
    the tier).

    *semconv_table* defaults to :data:`capsule_emit.otel.allowlist.SEMCONV_ATTRS`;
    accepting it as a parameter (rather than importing the module global
    directly) is what lets the leak-mutant test in
    ``tests/test_otel_processor.py`` pass a deliberately-broken copy through
    the REAL code path instead of monkeypatching module state.
    """
    from .allowlist import SEMCONV_ATTRS

    table = semconv_table if semconv_table is not None else SEMCONV_ATTRS

    block: OTelBlock = {}

    trace_id = _validated_hex(trace_id, TRACE_ID_RE, "trace_id")
    span_id = _validated_hex(span_id, SPAN_ID_RE, "span_id")
    block["trace_id"] = trace_id if clear_trace_context else _sha256_hex(trace_id)
    block["span_id"] = span_id if clear_trace_context else _sha256_hex(span_id)

    if parent_span_id:
        parent_span_id = _validated_hex(parent_span_id, SPAN_ID_RE, "parent_span_id")
        block["parent_span_id"] = parent_span_id if clear_trace_context else _sha256_hex(parent_span_id)

    if trace_flags:
        block["trace_flags"] = _validated_hex(trace_flags, TRACE_FLAGS_RE, "trace_flags")

    if tracestate:
        # The draft is unconditional here: "tracestate MUST NOT be carried in
        # clear (vendor entries may carry identifiers)" -- no clear_trace_context
        # escape hatch, unlike the IDs above.
        block["tracestate_digest"] = _sha256_hex(tracestate)

    if span_name:
        block["span_name"] = span_name if clear_trace_context else _sha256_hex(span_name)

    resource_subset = _resource_subset(resource_attributes)
    if resource_subset:
        block["resource"] = resource_subset

    semconv_subset = _semconv_subset(attributes, table=table, source=semconv_source)
    if len(semconv_subset) > 1:  # more than just "source"
        block["semconv"] = semconv_subset

    return block


def build_outcome_context_block(
    baggage: Mapping[str, str] | None, *, allowed_keys: frozenset[str]
) -> dict[str, str] | None:
    """The outcome-context tag, sourced from OpenTelemetry Baggage entries.
    *allowed_keys* is an explicit, caller-supplied allow-list; this function
    never reads a baggage entry whose key is not in it, and never invents key
    names of its own.

    **``allowed_keys`` is empty by default at the call site
    (:class:`capsule_emit.otel.processor.CapsuleOTelSpanExporter`).** No
    design note naming the real baggage keys to allow-list exists yet.
    Shipping a guessed key list would violate the "do not invent keys" rule,
    so v0 ships the mechanism wired to nothing; the day the real keys land,
    turning this on is a one-line config change, not a code change.

    Values are always digested, never carried clear -- baggage is
    caller-supplied string data with no allow-list of its own (unlike the
    semconv table above, ANY string can ride in a baggage entry), so this
    function applies the same digest-only default the rest of v0 uses for
    anything it cannot mechanically classify as safe.
    """
    if not baggage or not allowed_keys:
        return None
    tagged = {key: _sha256_hex(value) for key, value in baggage.items() if key in allowed_keys}
    return tagged or None
