# SPDX-License-Identifier: Apache-2.0
"""Capsule-native ledger viewer — zero engine imports.

Ports the display semantics from the hermes demo's viewer_transform.py
as pure capsule-dict functions.  Works directly on JSONL ledger records.

The terminal table ``capsule-emit ledger view`` draws. The HTML page
(``render_html`` and ``ledger view --html``) moved to capsule-viewer, as
``python -m capsule_viewer.ledger``.

Public API
----------
render_table(capsules, *, verify_results=None, out=None, path="") -> None
"""
from __future__ import annotations

import sys
from typing import Any

__all__ = ["render_table"]

# ---------------------------------------------------------------------------
# Vocabulary bridges
# ---------------------------------------------------------------------------

_EFFECT_MODE: dict[str, str] = {
    "effect_applied": "confirmed",
    "effect_dispatched": "dispatched_unconfirmed",
    "not_applicable": "not_applicable",
}

_STATUS: dict[str, str] = {
    "confirmed": "applied",
    "dispatched": "applied",
    "planned": "pending",
    "failed": "rejected",
}

# ---------------------------------------------------------------------------
# Category + actor — capsule-native
# ---------------------------------------------------------------------------


def _category(effect_type: str, chain_relation: str | None, disp_decision: str) -> str:
    if effect_type == "memory_write" and chain_relation is None:
        return "rule_change"
    if chain_relation == "memory_delta":
        return "acknowledge_info_only"
    return "apply_candidate"


def _actor(
    cap: dict,
    effect_status: str | None,
    disp_decision: str,
    chain_relation: str | None,
    chain_parent: str | None,
) -> str:
    short = (chain_parent or "")[:8]
    if chain_parent is None and (cap.get("effect") or {}).get("type") == "memory_write":
        return f"{cap.get('operator', 'operator')} (authority-grant)"
    if effect_status == "planned" and disp_decision == "denied":
        return f"agent [↑ grant:{short}] — REFUSED (over-authority)"
    if chain_relation == "acted_under_authority":
        return f"agent [↑ grant:{short}]"
    if chain_relation == "confirms":
        return f"agent [↑ dispatched:{short}] (confirmed)"
    if chain_relation == "memory_delta":
        return "agent (memory-delta)"
    return cap.get("developer", "agent")


def _row(cap: dict, verify_ok: bool | None = None) -> dict:
    """Flatten a capsule dict into display fields."""
    effect_d: dict = cap.get("effect") or {}
    disp_d: dict = cap.get("disposition") or {}
    chain_d: dict = cap.get("chain") or {}

    effect_status = effect_d.get("status")
    effect_type = effect_d.get("type", "")
    disp_decision = disp_d.get("decision", "")
    verdict = disp_d.get("verdict_class", "") or disp_d.get("decision", "")
    chain_relation = chain_d.get("relation")
    chain_parent = chain_d.get("parent_capsule_id")

    category = _category(effect_type or "unknown", chain_relation, disp_decision)
    actor = _actor(cap, effect_status, disp_decision, chain_relation, chain_parent)

    status_display = _STATUS.get(effect_status or "", effect_status or "")
    if disp_decision == "denied":
        status_display = "rejected"

    is_refusal = disp_decision == "denied" or verdict in ("blocked", "denied")

    action_id = cap.get("action_id", "?")
    action = action_id.split("/")[0] if "/" in action_id else action_id

    chain_label = ""
    if chain_parent:
        short_parent = chain_parent[:8]
        chain_label = f"{chain_relation}→{short_parent}…" if chain_relation else f"→{short_parent}…"

    gate_checks = []
    ca = (cap.get("model_attestation") or {}).get("compute_attestation") or {}
    if ca.get("gate_checks"):
        gate_checks = ca["gate_checks"]

    return {
        "capsule_id": cap.get("capsule_id", "?"),
        "action": action,
        "action_id": action_id,
        "actor": actor,
        "category": category,
        "effect_type": effect_type,
        "effect_status": status_display,
        "verdict": verdict,
        "is_refusal": is_refusal,
        "chain_label": chain_label,
        "chain_parent": chain_parent or "",
        "chain_relation": chain_relation or "",
        "ts": cap.get("ts", cap.get("timestamp", "")),
        "operator": cap.get("operator", ""),
        "developer": cap.get("developer", ""),
        "gate_checks": gate_checks,
        "verify_ok": verify_ok,
    }


def _verify_map(capsules: list[dict], verify_results: list | None) -> dict[str, bool | None]:
    """Build capsule_id → verify_ok mapping from an ordered verify_results list."""
    vmap: dict[str, bool | None] = {}
    if not verify_results:
        return vmap
    for i, vr in enumerate(verify_results):
        cid = ""
        if hasattr(vr, "capsule_id") and vr.capsule_id:
            cid = vr.capsule_id
        elif hasattr(vr, "capsule") and vr.capsule:
            cid = (vr.capsule or {}).get("capsule_id", "")
        if not cid and i < len(capsules):
            cid = capsules[i].get("capsule_id", "")
        if cid:
            vmap[cid] = bool(vr.ok)
    return vmap


# ---------------------------------------------------------------------------
# CLI table renderer
# ---------------------------------------------------------------------------

_COL_ID = 14
_COL_ACTOR = 42
_COL_VERDICT = 12
_COL_EFFECT = 20


def render_table(
    capsules: list[dict],
    *,
    verify_results: list | None = None,
    out: Any = None,
    path: str = "",
) -> None:
    """Render enhanced L1 table: actor lineage labels, refusal flags, verify status."""
    if out is None:
        out = sys.stdout

    if not capsules:
        label = path or "<ledger>"
        print(f"ledger: {label} — empty or not found", file=out)
        return

    vmap = _verify_map(capsules, verify_results)
    rows = [_row(cap, vmap.get(cap.get("capsule_id", ""))) for cap in capsules]

    label = path or "<ledger>"
    print(f"\ncapsule ledger: {label}  ({len(rows)} record(s))\n", file=out)

    header = (
        f"  {'capsule_id':<{_COL_ID}}  "
        f"{'actor':<{_COL_ACTOR}}  "
        f"{'verdict':<{_COL_VERDICT}}  "
        f"{'effect':<{_COL_EFFECT}}  "
        f"chain  "
        f"  verify"
    )
    sep = "-" * len(header)
    print(header, file=out)
    print(sep, file=out)

    for row in rows:
        cid = row["capsule_id"][:_COL_ID]
        actor = row["actor"][:_COL_ACTOR]
        verdict = row["verdict"][:_COL_VERDICT]
        effect_parts = []
        if row["effect_type"]:
            effect_parts.append(row["effect_type"])
        if row["effect_status"]:
            effect_parts.append(row["effect_status"])
        effect_str = (":".join(effect_parts))[:_COL_EFFECT]
        chain = row["chain_label"]
        vok = row["verify_ok"]
        v_str = "✓" if vok is True else ("✗" if vok is False else "—")
        marker = "⊛" if row["is_refusal"] else " "
        print(
            f"{marker} {cid:<{_COL_ID}}  "
            f"{actor:<{_COL_ACTOR}}  "
            f"{verdict:<{_COL_VERDICT}}  "
            f"{effect_str:<{_COL_EFFECT}}  "
            f"{chain:<10}  "
            f"{v_str}",
            file=out,
        )
    print(file=out)
