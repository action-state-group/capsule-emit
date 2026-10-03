# SPDX-License-Identifier: Apache-2.0
"""OTel processor v0 -- digest-only export of the ``org.agentactioncapsule.otel``
correlation block (draft-palanisamy-scitt-aac-otel-00) plus taxonomy-driven
observation/effect classification of spans. See ``capsule_emit.otel.processor``
for the module docstring covering the design (Python SpanExporter, not a Go
collector build; the reverse-join attribute's best-effort nature) and
``docs/extensions/otel-correlation.md`` for the user-facing writeup.
"""
from __future__ import annotations

from .allowlist import DEFAULT_SEMCONV_SOURCE, RESOURCE_ATTRS, SEMCONV_ATTRS, Tier
from .block import OTEL_BLOCK_KEY, OUTCOME_CONTEXT_KEY, build_otel_block, build_outcome_context_block
from .processor import (
    REVERSE_JOIN_ATTRIBUTE,
    CapsuleOTelSpanExporter,
    SpanFacts,
    process_span_facts,
    stamp_reverse_join,
)
from .signal import classify_span_signal_1

__all__ = [
    "DEFAULT_SEMCONV_SOURCE",
    "RESOURCE_ATTRS",
    "SEMCONV_ATTRS",
    "Tier",
    "OTEL_BLOCK_KEY",
    "OUTCOME_CONTEXT_KEY",
    "build_otel_block",
    "build_outcome_context_block",
    "REVERSE_JOIN_ATTRIBUTE",
    "CapsuleOTelSpanExporter",
    "SpanFacts",
    "process_span_facts",
    "stamp_reverse_join",
    "classify_span_signal_1",
]
