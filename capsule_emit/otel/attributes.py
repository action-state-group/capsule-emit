# SPDX-License-Identifier: Apache-2.0
"""The attribute-value type shared by every module in this package that reads
a span's or a resource's attributes.

Mirrors OpenTelemetry's own ``opentelemetry.util.types.AttributeValue``
(a scalar, or a same-typed sequence) exactly, as a local type-only alias --
so :mod:`capsule_emit.otel.signal` and :mod:`capsule_emit.otel.block` stay
importable with zero OpenTelemetry dependency (see
``capsule_emit.otel.processor``'s module docstring), while still declaring
the real, bounded union rather than ``Any``.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

__all__ = ["SpanAttributeValue"]

SpanAttributeValue = Union[
    str,
    bool,
    int,
    float,
    Sequence[str],
    Sequence[bool],
    Sequence[int],
    Sequence[float],
]
