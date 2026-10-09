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

When ``action_class`` is given, the sum is additionally scoped to that
class: the cap the caller is about to check is per ``(developer,
action_class)`` (``holds/scope.py``'s lock is keyed the same way), so the
aggregate that decision is evaluated against must agree, or the read is
checking a different scope than the one the lock and the cap serialize —
exactly the gap a concurrent caller in a *different* class could otherwise
read straight through (closed 2026-08-11, cross-class TOCTOU). Only
``hold.reserve`` records carry ``hold_scope.action_class`` directly;
release/expire/reconcile records chain back to their reserve via
``chain.parent_capsule_id`` and inherit its class from there.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

__all__ = ["active_exposure_minor"]


def _reserve_action_class_by_id(records: Iterable[dict]) -> dict[str, str | None]:
    by_id: dict[str, str | None] = {}
    for capsule in records:
        action_id = capsule.get("action_id") or ""
        if not action_id.startswith("hold.reserve/"):
            continue
        capsule_id = capsule.get("capsule_id")
        if not capsule_id:
            continue
        payload = capsule.get("asg_payload") or {}
        scope = payload.get("hold_scope") or {}
        by_id[capsule_id] = scope.get("action_class")
    return by_id


_RFC3339 = re.compile(r"(\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d)(?:\.(\d+))?([Zz]|[+-]\d\d:\d\d)")


def _instant(value: object) -> datetime | None:
    """``value`` (an RFC 3339 time) as an aware instant, or ``None`` when it
    does not parse or carries no offset."""
    if not isinstance(value, str):
        return None
    match = _RFC3339.fullmatch(value)
    if match is None:
        return None
    # Any fraction length, as microseconds (fromisoformat on Python 3.9/3.10
    # takes only 3 or 6 digits).
    fraction = (match.group(2) or "")[:6].ljust(6, "0")
    offset = "+00:00" if match.group(3) in ("Z", "z") else match.group(3)
    try:
        return datetime.fromisoformat(f"{match.group(1).upper()}.{fraction}{offset}")
    except ValueError:
        return None


def _record_action_class(capsule: dict, reserve_class_by_id: dict[str, str | None]) -> str | None:
    action_id = capsule.get("action_id") or ""
    if action_id.startswith("hold.reserve/"):
        payload = capsule.get("asg_payload") or {}
        return (payload.get("hold_scope") or {}).get("action_class")
    parent_id = (capsule.get("chain") or {}).get("parent_capsule_id")
    return reserve_class_by_id.get(parent_id)


def active_exposure_minor(
    records: Iterable[dict], subject: str, *, action_class: str | None = None, as_of: str | None = None,
) -> int:
    """Independently recomputable from the raw ledger — a fresh pass over
    *records* reproduces the same cited aggregate byte-exactly, with no
    hidden state carried between calls.

    Args:
        records: Capsule dicts, in ledger order.
        subject: The ``developer`` this exposure is scoped to.
        action_class: When given, only records belonging to this hold scope
            count (matching the lock/cap granularity). ``None`` (default)
            sums across every class for *subject* -- unscoped, dev-wide.
        as_of: When given, only records whose ``timestamp`` is at or before
            the instant ``as_of`` names count. Times are compared as parsed
            instants, never as strings: ``…23:59:59Z`` and
            ``…23:59:59.999999Z`` are different spellings that string order
            gets wrong. A record whose ``timestamp`` does not parse does not
            count. ``None`` (default) includes every record.
    """
    records = list(records)
    cutoff = _instant(as_of) if as_of is not None else None
    if as_of is not None and cutoff is None:
        raise ValueError(f"as_of is not an RFC 3339 time: {as_of!r}")
    reserve_class_by_id = _reserve_action_class_by_id(records) if action_class is not None else {}

    total = 0
    for capsule in records:
        if capsule.get("developer") != subject:
            continue
        if cutoff is not None:
            stamped = _instant(capsule.get("timestamp"))
            if stamped is None or stamped > cutoff:
                continue
        disposition = capsule.get("disposition") or {}
        if disposition.get("decision") != "accept":
            continue
        action_id = capsule.get("action_id") or ""
        if not action_id.startswith("hold."):
            continue
        if action_class is not None and _record_action_class(capsule, reserve_class_by_id) != action_class:
            continue
        payload = capsule.get("asg_payload") or {}
        amount = payload.get("amount_minor")
        if isinstance(amount, bool) or not isinstance(amount, int):
            continue
        total += amount
    return total
