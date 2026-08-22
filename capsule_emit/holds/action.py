# SPDX-License-Identifier: Apache-2.0
"""``Action``: the hold engine's own input contract.

Not a capsule. An ``Action`` is the thing a caller wants to do, *before* any
reservation decision has been made about it — what ``HoldEngine.
evaluate_and_reserve`` evaluates and turns into a decision, which is what
becomes a capsule (``holds/capsules.py``).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


def _new_action_id(verb: str) -> str:
    return f"{verb}/{uuid.uuid4()}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Action:
    """One candidate action, as presented to the hold engine for a decision.

    ``action_class`` is the caller's own label for which cap/tolerance
    configuration applies (looked up directly in the ``cap_minor``/
    ``tolerance_minor`` dicts passed to ``HoldEngine`` — no built-in
    taxonomy). ``amount_minor``/``currency`` are integer-minor-units money
    fields (never floats). ``target`` is an optional free-form discriminator
    (e.g. a counterparty or recipient reference) carried through to the
    sealed capsule's payload for audit purposes. ``expires_at`` is an
    optional ISO-8601 deadline the caller intends this reservation to lapse
    by — recorded on the reserve capsule (``holds/capsules.py``) purely so
    an auditor can tell whether a later ``expire()`` call was timely or
    arbitrary; the engine never reads or enforces it itself (expiry stays
    caller-invoked, per ``HoldEngine.expire``'s own docstring).
    """

    verb: str
    operator: str
    developer: str
    action_class: str | None = None
    action_id: str | None = None
    timestamp: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    target: str | None = None
    expires_at: str | None = None

    def resolved_action_id(self) -> str:
        return self.action_id or _new_action_id(self.verb)

    def resolved_timestamp(self) -> str:
        return self.timestamp or _utc_now()
