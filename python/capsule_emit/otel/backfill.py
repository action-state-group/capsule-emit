# SPDX-License-Identifier: Apache-2.0
"""Seal capsules from an exported OTLP/JSON trace file (backfill).

:func:`seal_otlp_file` reads spans that were recorded before capsule-emit was
installed and seals the ones that classify ``EFFECT`` through the live path
(:func:`capsule_emit.otel.processor.process_span_facts`), so classification,
the allow-list and sealing are exactly the live exporter's.

**What a backfilled record claims.** Each record carries
``provenance_mode`` ``mode: "backfilled"`` and ``time_rung: "self_attested"``:
the span times are the source's own assertion, never proven capture time.

- ``source_ref``: SHA-256 over the raw ``trace_id`` and ``span_id`` bytes.
- ``source_asserted_at``: the span's start time (microsecond precision).
- ``import_batch``: SHA-256 hex of the file's bytes.
- ``imported_at``: the real seal time, never the span time (AAC rejects equal
  values as ``provenance_time_laundering_shape``).
- ``compute_attestation["ext.otel.span_time"]``: ``{start, end,
  asserted_by: "source"}``.

**Re-runs.** ``imported_at`` changes on every run, so capsule ids differ; a
span is skipped when the target ledger already holds a record with the same
``source_ref``. A span the live exporter already sealed has no ``source_ref``
and is not detected.

Input is one OTLP/JSON export document (``{"resourceSpans": [...]}``), or one
such document per line (the file exporter's JSON Lines form).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_action_capsule.contracts import ProvenanceMode, ReferenceEntry

from ..connector import Classification
from ..ledger import read_ledger
from ..ledger_layout import check_new_flat_record, resolve_ledger
from .allowlist import DEFAULT_SEMCONV_SOURCE, SPAN_ID_RE, TRACE_ID_RE
from .processor import SpanFacts, process_span_facts
from .signal import classify_span_signal_1

__all__ = ["BackfillReport", "SPAN_TIME_KEY", "SOURCE_REF_TYPE", "seal_otlp_file"]

#: Compute-attestation key for the span's own start/end, beside (not inside)
#: the ``org.agentactioncapsule.otel`` block the OTel draft owns.
SPAN_TIME_KEY = "ext.otel.span_time"
#: ``source_ref.type`` of a backfilled span record.
SOURCE_REF_TYPE = "otel-span"
_STATUS_ERROR = (2, "2", "STATUS_CODE_ERROR")
_MAX_NS = 253_402_300_799 * 10**9  # 9999-12-31T23:59:59Z, the last instant datetime holds


@dataclass(frozen=True)
class BackfillReport:
    """Outcome of one :func:`seal_otlp_file` run.

    Attributes:
        sealed: Capsule ids sealed by this run.
        skipped_observation: Spans classified ``OBSERVATION``, which seal nothing.
        failed: ``EFFECT`` spans whose seal failed (a warning says why).
        skipped_duplicate: Spans whose ``source_ref`` the ledger already held.
        skipped_invalid: Spans without usable ids or a start time after 1970.
        import_batch: Digest of the file's bytes.
    """

    sealed: tuple[str, ...]
    skipped_observation: int
    failed: int
    skipped_duplicate: int
    skipped_invalid: int
    import_batch: str


def seal_otlp_file(
    path: str | os.PathLike,
    *,
    operator: str,
    developer: str,
    ledger: str | os.PathLike | None = None,
    clear_trace_context: bool = False,
    semconv_source: str = DEFAULT_SEMCONV_SOURCE,
    admit_conditional: frozenset[str] = frozenset(),
) -> BackfillReport:
    """Seal a ``backfilled`` capsule for each ``EFFECT`` span in *path*.

    See the module docstring for the record shape and re-run behavior. The
    whole file is parsed before anything is sealed: raises ``ValueError``
    if it is not OTLP/JSON, and writes nothing. Span-level problems (bad ids,
    no usable start time) count under ``skipped_invalid``. A target the
    installed version no longer writes (a flat ledger) raises
    ``capsule_emit.ledger_layout.FlatLedgerWriteRefused``.
    """
    raw = Path(path).read_bytes()
    batch = hashlib.sha256(raw).hexdigest()
    spans = list(_spans(raw))
    resolution = resolve_ledger(ledger)
    seen = _sealed_source_refs(resolution.path)

    sealed: list[str] = []
    observed = failed = duplicate = invalid = 0
    for parsed in spans:
        if parsed is None:
            invalid += 1
            continue
        facts, start_ns, end_ns = parsed
        digest = _source_digest(facts.trace_id, facts.span_id)
        if digest in seen:
            duplicate += 1
            continue
        if classify_span_signal_1(facts.name or "unknown", facts.attributes) is not Classification.EFFECT:
            observed += 1
            continue
        check_new_flat_record(resolution.path, resolution.chosen)
        asserted_at = _rfc3339(start_ns)
        imported_at = _rfc3339(None)
        if imported_at == asserted_at:  # pragma: no cover - a span starting at this microsecond
            raise ValueError("imported_at equals source_asserted_at; refusing a laundering-shaped record")
        mode = ProvenanceMode(
            mode="backfilled",
            source_ref=ReferenceEntry(type=SOURCE_REF_TYPE, digest_alg="SHA-256", digest=digest),
            source_asserted_at=asserted_at,
            import_batch=batch,
            imported_at=imported_at,
            time_rung="self_attested",
        )
        span_time = {"start": asserted_at, "asserted_by": "source"}
        if end_ns is not None:
            span_time["end"] = _rfc3339(end_ns)
        result = process_span_facts(
            facts,
            operator=operator,
            developer=developer,
            ledger=resolution.path,
            clear_trace_context=clear_trace_context,
            semconv_source=semconv_source,
            admit_conditional=admit_conditional,
            provenance_mode=mode,
            extra_compute={SPAN_TIME_KEY: span_time},
        )
        if result is None:
            failed += 1
            continue
        seen.add(digest)
        sealed.append(result.capsule_id)
    return BackfillReport(tuple(sealed), observed, failed, duplicate, invalid, batch)


def _source_digest(trace_id: str, span_id: str) -> str:
    return hashlib.sha256(bytes.fromhex(trace_id) + bytes.fromhex(span_id)).hexdigest()


def _sealed_source_refs(ledger: str | os.PathLike) -> set[str]:
    refs: set[str] = set()
    for capsule in read_ledger(ledger):
        mode = capsule.get("provenance_mode")
        ref = mode.get("source_ref") if isinstance(mode, Mapping) else None
        if isinstance(ref, Mapping) and ref.get("type") == SOURCE_REF_TYPE and isinstance(ref.get("digest"), str):
            refs.add(ref["digest"])
    return refs


def _rfc3339(ns: int | None) -> str:
    """UTC RFC 3339 to the microsecond; ``None`` is now."""
    moment = datetime.now(timezone.utc) if ns is None else datetime.fromtimestamp(ns // 1000 / 1_000_000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _documents(raw: bytes) -> list[Mapping[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"not OTLP/JSON: {exc}") from exc
    try:
        docs: list[Any] = [json.loads(text)]
    except json.JSONDecodeError:
        try:
            docs = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise ValueError(f"not OTLP/JSON: {exc}") from exc
    for doc in docs:
        if not isinstance(doc, Mapping) or not isinstance(doc.get("resourceSpans"), list):
            raise ValueError('not OTLP/JSON: expected a "resourceSpans" list')
    return docs


def _spans(raw: bytes) -> Iterator[tuple[SpanFacts, int, int | None] | None]:
    """One entry per span: its facts and times, or ``None`` when unusable."""
    for doc in _documents(raw):
        for rs in doc["resourceSpans"]:
            if not isinstance(rs, Mapping):
                continue
            resource = _attrs((rs.get("resource") or {}).get("attributes"))
            for ss in rs.get("scopeSpans") or []:
                for span in (ss.get("spans") or []) if isinstance(ss, Mapping) else []:
                    yield _span_facts(span, resource) if isinstance(span, Mapping) else None


def _span_facts(span: Mapping[str, Any], resource: dict[str, Any]) -> tuple[SpanFacts, int, int | None] | None:
    trace_id, span_id = _id(span.get("traceId"), TRACE_ID_RE), _id(span.get("spanId"), SPAN_ID_RE)
    start, end = _nanos(span.get("startTimeUnixNano")), _nanos(span.get("endTimeUnixNano"))
    if trace_id is None or span_id is None or start is None:
        warnings.warn(
            "capsule-emit otel backfill: span without valid trace/span ids and a start time skipped",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    if end is not None and end < start:
        end = None
    flags = span.get("flags")
    status = span.get("status") or {}
    facts = SpanFacts(
        name=span.get("name") if isinstance(span.get("name"), str) else "",
        attributes=_attrs(span.get("attributes")),
        resource_attributes=resource,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=_id(span.get("parentSpanId"), SPAN_ID_RE),
        trace_flags=f"{flags & 0xFF:02x}" if isinstance(flags, int) and not isinstance(flags, bool) else None,
        tracestate=span.get("traceState") or None,
        status_is_error=isinstance(status, Mapping) and status.get("code") in _STATUS_ERROR,
    )
    return facts, start, end


def _id(value: Any, pattern: re.Pattern[str]) -> str | None:
    """A trace or span ID as lowercase hex (OTLP/JSON allows either case), or
    ``None`` when malformed or all zeros (W3C Trace Context: invalid; as a
    parent it means a root span)."""
    if not isinstance(value, str):
        return None
    value = value.lower()
    return value if pattern.match(value) and value.strip("0") else None


def _nanos(value: Any) -> int | None:
    """A Unix-nanosecond time, or ``None`` when absent, zero (protobuf's
    unset value), negative or past year 9999."""
    try:
        ns = int(value)
    except (TypeError, ValueError):
        return None
    return ns if 0 < ns <= _MAX_NS else None


def _attrs(items: Any) -> dict[str, Any]:
    """Attributes with a supported value type; others are left out rather
    than sealed as an empty value the span never had."""
    out: dict[str, Any] = {}
    for kv in items if isinstance(items, list) else []:
        if isinstance(kv, Mapping) and isinstance(kv.get("key"), str) and isinstance(kv.get("value"), Mapping):
            try:
                out[kv["key"]] = _any_value(kv["value"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _any_value(value: Mapping[str, Any]) -> Any:
    if "stringValue" in value and isinstance(value["stringValue"], str):
        return value["stringValue"]
    if "boolValue" in value and isinstance(value["boolValue"], bool):
        return value["boolValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [_any_value(v) for v in value["arrayValue"].get("values") or []]
    raise KeyError("unsupported OTLP value")
