#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The intension-vs-extension check, factored out so it is independently testable.

``template`` declares the expected set (the intension): which ``action_id``s, in what
order, and what counts as complete. ``observed`` is the extension: what actually
happened, cited by ``action_id`` in occurrence order. ``evaluate`` reports the diff —
never silently; a missing or out-of-order member is always a finding.
"""
from __future__ import annotations

from typing import Any


def evaluate(template: dict[str, Any], observed: list[dict[str, str]]) -> dict[str, Any]:
    """Compare ``observed`` against ``template["members"]``.

    Returns ``{"complete": bool, "findings": [...]}``. ``findings`` entries are one of:
    ``unexpected_member`` (observed an action_id the template never declared),
    ``missing_member`` (a declared action_id never observed), or ``order_violation``
    (an observed member appeared before a declared predecessor).
    """
    declared_ids = [m["action_id"] for m in template["members"]]
    declared_index = {aid: i for i, aid in enumerate(declared_ids)}
    observed_ids = [o["action_id"] for o in observed]

    findings: list[dict[str, str]] = []

    seen = set()
    for aid in observed_ids:
        if aid not in declared_index:
            findings.append({"kind": "unexpected_member", "action_id": aid})
        else:
            seen.add(aid)

    for aid in declared_ids:
        if aid not in seen:
            findings.append({"kind": "missing_member", "action_id": aid})

    last_index = -1
    for aid in observed_ids:
        idx = declared_index.get(aid)
        if idx is None:
            continue
        if idx < last_index:
            findings.append({"kind": "order_violation", "action_id": aid})
        last_index = max(last_index, idx)

    complete = not any(f["kind"] in ("missing_member", "order_violation") for f in findings)
    return {"complete": complete, "findings": findings}
