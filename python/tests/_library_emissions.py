# SPDX-License-Identifier: Apache-2.0
"""One record from each capsule-emit helper that writes a registry-governed
token of its own (a literal, not a value the caller passed in).

Shared by ``test_verdict_class_registered.py`` (main suite, against the
installed agent-action-capsule's REGISTRY.md) and
``test_registry_conformance_aac.py`` (the ``registry-conformance`` CI job,
against agent-action-capsule's registry-conformance check).

Not covered here: the framework adapters, ``gate_and_emit``, the holds
builders and settlement legs. A read of their source finds only registered
literal verdict_class / decision values (``executed``, ``blocked``,
``denied``, ``errored``, ``accept``, ``reject``), but nothing here checks it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from capsule_emit import log
from capsule_emit.adjudication import VERDICT_CORROBORATED, seal_adjudication
from capsule_emit.approval import seal_approval
from capsule_emit.bilateral import seal_action, seal_bilateral, seal_ghost, seal_request
from capsule_emit.core import _emit_capsule


def library_emissions(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Return ``{producer name: sealed capsule dict}``."""
    ledger = tmp_path / "ledger.jsonl"
    common = {"anchor": False, "ledger": ledger}
    out: dict[str, dict[str, Any]] = {}

    out["seal (defaults)"] = _emit_capsule(
        action="write_order", operator="example-org", developer="agent@v1", **common
    ).capsule
    out["log"] = log(b"an event", ledger=ledger, witness=False).capsule

    half_a = _emit_capsule(action="serve_a", operator="example-org", developer="agent@v1", **common)
    half_b = _emit_capsule(action="serve_b", operator="example-org", developer="agent@v1", **common)
    out["seal_adjudication"] = seal_adjudication(
        half_a.capsule_id,
        half_b.capsule_id,
        VERDICT_CORROBORATED,
        margin=1.0,
        margin_tau=1.0,
        operator="example-org",
        developer="referee@v1",
        **common,
    ).capsule

    request = seal_request("org-a", "agent@v1", "write_order", "d" * 64, **common)
    out["bilateral.seal_request"] = request.capsule
    action = seal_action("org-b", "agent@v1", "write_order", "d" * 64, request.capsule_id, **common)
    out["bilateral.seal_action"] = action.capsule
    out["bilateral.seal_bilateral"] = seal_bilateral(
        "org-a", "agent@v1", "write_order", "d" * 64, action.capsule_id, **common
    ).capsule
    out["bilateral.seal_ghost"] = seal_ghost(
        "org-a", "agent@v1", "write_order", "d" * 64, request.capsule_id, **common
    ).capsule

    for decision in ("approve", "deny"):
        blocked = _emit_capsule(
            action="write_order",
            operator="example-org",
            developer="agent@v1",
            verdict="blocked",
            effect={"type": "write_order", "status": "planned"},
            **common,
        )
        out[f"seal_approval ({decision})"] = seal_approval(
            blocked.capsule_id,
            "approver@example.org",
            decision,
            "d" * 64,
            operator="example-org",
            developer="approver@v1",
            **common,
        ).capsule
    return out
