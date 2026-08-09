# SPDX-License-Identifier: Apache-2.0
"""Active-exposure aggregate: "evaluated aggregate = converted spend +
active reserves" falls out of plain delta summation over hold-lifecycle
capsules, rather than being special-cased anywhere.

An unwindowed running balance over signed per-record deltas, keyed by
subject (``developer``), read from accepted ``hold.*`` capsules only:

- ``hold.reserve``   contributes  ``+reserved_amount_minor``
- ``hold.reconcile``   contributes  ``executed_amount_minor - reserved_amount_minor``
- ``hold.release`` / ``hold.expire``  contribute  ``-reserved_amount_minor``

A hold's own contributions always sum to its final settled value (0 for a
release/expire, the executed amount for a reconcile) regardless of how long
ago it was reserved — this is deliberately unwindowed (not filtered by
record age): windowing would truncate a reserve's ``+`` delta while still
counting a later release/reconcile's matching ``-`` delta (or vice versa),
driving the aggregate negative or double-counted.

A record only ever contributes when ``disposition.decision == "accept"`` —
a denied/escalated/refused attempt (see ``holds/engine.py``) never lands an
accepted record, so it can never move the aggregate.
"""
from __future__ import annotations

from collections.abc import Iterable

__all__ = ["active_exposure_minor"]


def active_exposure_minor(records: Iterable[dict], subject: str, *, as_of: str | None = None) -> int:
    """Independently recomputable from the raw ledger — a fresh pass over
    *records* reproduces the same cited aggregate byte-exactly, with no
    hidden state carried between calls.

    Args:
        records: Capsule dicts, in ledger order.
        subject: The ``developer`` this exposure is scoped to.
        as_of: When given, only records with ``timestamp <= as_of`` count
            (ISO-8601 strings compare correctly lexicographically). ``None``
            (default) includes every record.
    """
    total = 0
    for capsule in records:
        if capsule.get("developer") != subject:
            continue
        if as_of is not None and (capsule.get("timestamp") or "") > as_of:
            continue
        disposition = capsule.get("disposition") or {}
        if disposition.get("decision") != "accept":
            continue
        action_id = capsule.get("action_id") or ""
        if not action_id.startswith("hold."):
            continue
        payload = capsule.get("asg_payload") or {}
        amount = payload.get("amount_minor")
        if isinstance(amount, bool) or not isinstance(amount, int):
            continue
        total += amount
    return total
