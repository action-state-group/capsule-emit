# SPDX-License-Identifier: Apache-2.0
"""``CapsuleOTelSpanExporter`` -- the OTel processor/exporter itself.

**Python reference, not a Go collector-distribution build.** capsule-emit is
a Python package with no Go code anywhere in this repo (the Go side of this
build-plan batch, `capsulectl`, lives in the separate `capsule-cli` repo).
This composes at the OpenTelemetry *Python SDK* level -- a
``SpanExporter`` any Python app's ``TracerProvider`` can register -- the same
way every other capsule-emit adapter plugs into its host framework's own
seam, rather than a standalone `otelcol` binary. A Go collector-distribution
processor is a separate build, out of scope here; say so rather than half-do
both.

**Why ``SpanExporter``, not ``SpanProcessor.on_start``.** A capsule can only
be sealed once the action it describes is fully known -- classification,
resource/semconv attributes, and the error/ok outcome all live on the ENDED
span, never the one still running. ``SpanExporter.export()`` receives
already-ended, immutable ``ReadableSpan`` objects, which is exactly the
right shape: one call per span, after the fact, batched or not per the SDK's
own ``BatchSpanProcessor``/``SimpleSpanProcessor`` choice upstream of this
class.

**The reverse-join attribute is best-effort, and this is an OTel SDK
constraint, not a bug in this module.** A ``ReadableSpan`` is immutable by
the time an exporter sees it -- and the OpenTelemetry Python SDK's own
``Span.set_attribute()`` silently no-ops once ``end()`` has been called, on
ANY span, not just the exported one. There is no supported way for an
exporter to amend the very span it is exporting. v0 calls
``opentelemetry.trace.get_current_span().set_attribute(...)`` from inside
``export()`` on a best-effort basis -- this reaches a DIFFERENT, still-open
span (typically the parent, when ``export()`` fires synchronously inside the
child's own ``end()`` call, e.g. under ``SimpleSpanProcessor``) and is a
no-op when nothing is currently open (e.g. async/batched export on a
background thread with no active context). For a guaranteed-correct join on
the SAME span, call :func:`stamp_reverse_join` directly from application code
BEFORE that span's own ``span.end()`` -- the one place a live, mutable
``Span`` object is still available.
"""
from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExportResult

from ..adapters._base import CapsuleEmitterBase
from ..connector import Classification
from ..core import EmitResult
from .allowlist import DEFAULT_SEMCONV_SOURCE
from .attributes import SpanAttributeValue
from .block import (
    OTEL_BLOCK_KEY,
    OUTCOME_CONTEXT_KEY,
    _sha256_hex,
    build_otel_block,
    build_outcome_context_block,
)
from .signal import classify_span_signal_1

__all__ = [
    "REVERSE_JOIN_ATTRIBUTE",
    "SpanFacts",
    "process_span_facts",
    "stamp_reverse_join",
    "CapsuleOTelSpanExporter",
]

#: Provisional per the draft ("`aac.capsule_id` is a placeholder ... expect
#: rename to `gen_ai.evidence.*`" once the OpenTelemetry semantic-conventions
#: registry assigns a real name). A single named constant so the eventual
#: rename is a one-line change here, not a grep-and-replace.
REVERSE_JOIN_ATTRIBUTE = "aac.capsule_id"


@dataclass(frozen=True)
class SpanFacts:
    """The subset of a ``ReadableSpan`` this module needs, as plain data --
    lets :func:`process_span_facts` and every test exercise the full
    classify -> allow-list -> seal path with zero OpenTelemetry SDK
    dependency; :class:`CapsuleOTelSpanExporter` is the only piece of this
    module that touches the real SDK types, translating a ``ReadableSpan``
    into exactly this shape at the boundary.

    Attributes:
        name: Span name.
        attributes: The span's own attributes -- never a parent's or a
            child's (see ``signal.classify_span_signal_1``'s per-span note).
        resource_attributes: The span's ``Resource`` attributes.
        trace_id: 32-lowercase-hex W3C trace ID.
        span_id: 16-lowercase-hex W3C span ID.
        parent_span_id: 16-lowercase-hex W3C span ID of the parent, or
            ``None`` for a root span.
        trace_flags: 2-lowercase-hex W3C trace flags, or ``None``.
        tracestate: The raw ``tracestate`` header value, or ``None`` --
            digested unconditionally (:mod:`capsule_emit.otel.block`), never
            read for any other purpose.
        status_is_error: Whether the span's OTel ``Status`` is ``ERROR``.
        baggage: OpenTelemetry Baggage entries visible at export time, or
            ``None`` -- consulted ONLY for the caller-allow-listed keys in
            outcome-context tagging (:func:`capsule_emit.otel.block.build_outcome_context_block`);
            never otherwise read.
    """

    name: str
    attributes: Mapping[str, SpanAttributeValue]
    resource_attributes: Mapping[str, SpanAttributeValue] | None
    trace_id: str
    span_id: str
    parent_span_id: str | None
    trace_flags: str | None
    tracestate: str | None
    status_is_error: bool
    baggage: Mapping[str, str] | None = None


def process_span_facts(
    facts: SpanFacts,
    *,
    operator: str,
    developer: str,
    ledger: str = "ledger.jsonl",
    anchor: bool | None = None,
    clear_trace_context: bool = False,
    semconv_source: str = DEFAULT_SEMCONV_SOURCE,
    outcome_context_baggage_keys: frozenset[str] = frozenset(),
) -> EmitResult | None:
    """Classify *facts*, and seal a capsule iff it classifies ``EFFECT``.

    Returns ``None`` for an ``OBSERVATION`` -- v0 does not seal reads by
    default, the same "why reads are not sealed by default" posture
    ``docs/whats-consequential.md`` states for every other adapter (a read
    worth capturing is Signal 2's job, engine-side, direct ``seal()``, not
    this passive span tap). Returns ``None`` (warns, never raises) if sealing
    itself fails -- a broken ledger or anchor endpoint must not crash the
    host application's OTel export path, the same contract every other
    capsule-emit adapter makes.

    **The span name never reaches ``action_id``/``effect.type`` in clear
    unless *clear_trace_context* says so.** A §7b cold review caught this:
    an earlier version of this function passed the raw span name straight
    through as the emitter's ``action`` (and therefore into the sealed
    capsule's ``action_id`` and ``effect.type``, both plain, undigested
    fields) regardless of *clear_trace_context* -- while the SAME name was
    correctly gated inside ``otel_block["span_name"]``. That is exactly the
    "clear-safe, conditional ... never user-derived" span name the draft
    itself warns about (many real OTel instrumentations DO put dynamic data
    in a span name, e.g. an HTTP client span named with an unrendered
    route). ``action_label`` below applies the identical gate everywhere the
    name could leave the process.
    """
    name = facts.name or "unknown"
    classification = classify_span_signal_1(name, facts.attributes or {})
    if classification is not Classification.EFFECT:
        return None
    action_label = name if clear_trace_context else _sha256_hex(name)

    try:
        otel_block = build_otel_block(
            name,
            trace_id=facts.trace_id,
            span_id=facts.span_id,
            parent_span_id=facts.parent_span_id,
            trace_flags=facts.trace_flags,
            tracestate=facts.tracestate,
            attributes=facts.attributes,
            resource_attributes=facts.resource_attributes,
            clear_trace_context=clear_trace_context,
            semconv_source=semconv_source,
        )
    except ValueError as exc:
        warnings.warn(f"capsule-emit otel: malformed span identifiers for {name!r}: {exc}", RuntimeWarning, stacklevel=2)
        return None

    extra_compute: dict[str, Any] = {OTEL_BLOCK_KEY: otel_block}
    outcome_context = build_outcome_context_block(facts.baggage, allowed_keys=outcome_context_baggage_keys)
    if outcome_context:
        extra_compute[OUTCOME_CONTEXT_KEY] = outcome_context

    emitter = CapsuleEmitterBase(operator=operator, developer=developer, ledger=ledger, anchor=anchor)
    status = "failed" if facts.status_is_error else "confirmed"
    try:
        return emitter.emit_capsule(
            action_label,
            {"span_name": name},
            {"status": "ERROR" if facts.status_is_error else "OK"},
            effect={"type": action_label, "status": status},
            runtime="otel",
            action_type="fyi",
            extra_compute=extra_compute,
        )
    except Exception as exc:  # noqa: BLE001 -- adapter boundary: never break the host's export path
        warnings.warn(f"capsule-emit otel: seal failed for span {name!r}: {exc}", RuntimeWarning, stacklevel=2)
        return None


def stamp_reverse_join(span: Any, capsule_id: str) -> None:
    """Set :data:`REVERSE_JOIN_ATTRIBUTE` on *span* directly -- the
    guaranteed-correct path, for application code that seals inline and
    still holds a live (not yet ended) ``Span`` object. See the module
    docstring for why :class:`CapsuleOTelSpanExporter`'s own attempt is only
    best-effort.
    """
    span.set_attribute(REVERSE_JOIN_ATTRIBUTE, capsule_id)


def _facts_from_readable_span(span: Any) -> SpanFacts:
    from opentelemetry.trace import format_span_id, format_trace_id

    ctx = span.context
    parent_span_id = None
    if span.parent is not None:
        parent_span_id = format_span_id(span.parent.span_id)
    tracestate = None
    if ctx.trace_state:
        tracestate = ",".join(f"{k}={v}" for k, v in ctx.trace_state.items())
    status_is_error = False
    if span.status is not None:
        status_is_error = span.status.status_code.name == "ERROR"
    return SpanFacts(
        name=span.name,
        attributes=dict(span.attributes or {}),
        resource_attributes=dict(span.resource.attributes) if span.resource is not None else None,
        trace_id=format_trace_id(ctx.trace_id),
        span_id=format_span_id(ctx.span_id),
        parent_span_id=parent_span_id,
        trace_flags=f"{int(ctx.trace_flags):02x}",
        tracestate=tracestate,
        status_is_error=status_is_error,
    )


class CapsuleOTelSpanExporter:
    """``opentelemetry.sdk.trace.export.SpanExporter`` that seals one capsule
    per span classified ``EFFECT``.

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from capsule_emit.otel import CapsuleOTelSpanExporter

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(
            CapsuleOTelSpanExporter(operator="acme-co", developer="my-agent@v1")
        ))

    Requires ``pip install "capsule-emit[otel]"`` (opentelemetry-sdk) --
    imported lazily inside :meth:`export`, never at module import time, so
    the rest of :mod:`capsule_emit.otel` (and every test that only needs
    :class:`SpanFacts`) stays importable without it.
    """

    def __init__(
        self,
        *,
        operator: str,
        developer: str,
        ledger: str = "ledger.jsonl",
        anchor: bool | None = None,
        clear_trace_context: bool = False,
        semconv_source: str = DEFAULT_SEMCONV_SOURCE,
        outcome_context_baggage_keys: frozenset[str] = frozenset(),
    ) -> None:
        self._operator = operator
        self._developer = developer
        self._ledger = ledger
        self._anchor = anchor
        self._clear_trace_context = clear_trace_context
        self._semconv_source = semconv_source
        self._outcome_context_baggage_keys = outcome_context_baggage_keys

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        from opentelemetry.sdk.trace.export import SpanExportResult
        from opentelemetry.trace import get_current_span

        for span in spans:
            facts = _facts_from_readable_span(span)
            result = process_span_facts(
                facts,
                operator=self._operator,
                developer=self._developer,
                ledger=self._ledger,
                anchor=self._anchor,
                clear_trace_context=self._clear_trace_context,
                semconv_source=self._semconv_source,
                outcome_context_baggage_keys=self._outcome_context_baggage_keys,
            )
            if result is not None:
                # Best-effort only -- see the module docstring's "reverse-join
                # attribute" note. get_current_span() may return the
                # INVALID_SPAN (no-op set_attribute) when nothing is open.
                get_current_span().set_attribute(REVERSE_JOIN_ATTRIBUTE, result.capsule_id)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002 -- SpanExporter interface
        return True
