# SPDX-License-Identifier: Apache-2.0
"""Signal 1, read off an OpenTelemetry span — the reference implementation of
``docs/whats-consequential.md``'s "Reading Signal 1 off an OpenTelemetry
span" section, which the taxonomy doc promised and
[batch3-connector-interface-and-discover] did not itself add (its two
retrofits were MCP and LangChain; this task's Do line is what OTel needed).

Deliberately does NOT reimplement the priority-order/fail-safe-default rule:
:data:`capsule_emit.connector.ConnectorEvent` already models ``http_method``
and ``commit_step_present`` as two of Signal 1's four inputs, and
:func:`capsule_emit.connector.classify_signal_1` already implements
"first signal present wins, unknown defaults to EFFECT". This module's only
job is translating OTel span attribute names the connector taxonomy has no
field for (``db.operation.name``, ``messaging.operation.type``,
``rpc.method``) into that same shared rule, so the priority order and the
fail-safe default live in exactly one place.
"""
from __future__ import annotations

from collections.abc import Mapping

from ..connector import Classification, ConnectorEvent, classify_signal_1
from .attributes import SpanAttributeValue

__all__ = ["classify_span_signal_1"]

_WRITE_DB_OPS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
_READ_DB_OPS = frozenset({"SELECT"})

_WRITE_MESSAGING_OPS = frozenset({"create", "send", "settle"})
_READ_MESSAGING_OPS = frozenset({"receive", "process"})

# rpc.method carries no MCP-style hint and no HTTP verb -- only a bare method
# name (docs/whats-consequential.md, "OTel span" table: "an rpc.method name
# that does not read unambiguously as a verb" falls to the fail-safe). These
# prefixes are the unambiguous case; anything else is ambiguous by design.
_MUTATING_RPC_PREFIXES = (
    "Create", "Update", "Delete", "Put", "Post", "Insert", "Remove", "Set",
    "Write", "Patch", "Merge", "Cancel", "Submit", "Register", "Revoke", "Grant",
)
_QUERY_RPC_PREFIXES = (
    "Get", "List", "Describe", "Read", "Query", "Find", "Search", "Lookup",
    "Check", "Head", "Fetch", "Watch",
)


def _db_operation_signal(op: str) -> bool | None:
    normalized = op.strip().upper()
    if normalized in _WRITE_DB_OPS:
        return True
    if normalized in _READ_DB_OPS:
        return False
    return None


def _messaging_operation_signal(op: str) -> bool | None:
    normalized = op.strip().lower()
    if normalized in _WRITE_MESSAGING_OPS:
        return True
    if normalized in _READ_MESSAGING_OPS:
        return False
    return None


def _rpc_method_signal(method: str) -> bool | None:
    if method.startswith(_MUTATING_RPC_PREFIXES):
        return True
    if method.startswith(_QUERY_RPC_PREFIXES):
        return False
    return None


def _span_commit_step_signal(attributes: Mapping[str, SpanAttributeValue]) -> bool | None:
    """``True``/``False``/``None`` — Signal 1's ``commit_step_present`` slot,
    read off whichever of ``db.operation.name`` / ``messaging.operation.type``
    / ``rpc.method`` is present, in that priority order (the order the
    taxonomy doc lists them in). Only reached when the span carries no
    ``http.request.method`` (see :func:`classify_span_signal_1`) -- span kind
    ``CLIENT`` alone, with none of these attributes present, is "not evidence
    of a read either way" per the taxonomy doc and falls straight through to
    ``classify_signal_1``'s own fail-safe default.
    """
    db_op = attributes.get("db.operation.name")
    if isinstance(db_op, str):
        signal = _db_operation_signal(db_op)
        if signal is not None:
            return signal
    messaging_op = attributes.get("messaging.operation.type")
    if isinstance(messaging_op, str):
        signal = _messaging_operation_signal(messaging_op)
        if signal is not None:
            return signal
    rpc_method = attributes.get("rpc.method")
    if isinstance(rpc_method, str):
        signal = _rpc_method_signal(rpc_method)
        if signal is not None:
            return signal
    return None


def classify_span_signal_1(name: str, attributes: Mapping[str, SpanAttributeValue]) -> Classification:
    """Classify one span's own attributes -- never a parent's (worked example
    4: "classify per-span, not per-request" -- a caller that wants
    request-level classification must pass the request span's own
    attributes, not fold a child's into it).

    ``http.request.method`` takes priority when present (the taxonomy doc's
    table lists it first, and it is the one row :func:`classify_signal_1`
    already models directly via ``http_method``); everything else resolves to
    ``commit_step_present`` and shares that rule's priority + fail-safe
    default.
    """
    http_method = attributes.get("http.request.method")
    if isinstance(http_method, str) and http_method:
        event = ConnectorEvent(name=name, http_method=http_method)
    else:
        event = ConnectorEvent(name=name, commit_step_present=_span_commit_step_signal(attributes))
    return classify_signal_1(event)
