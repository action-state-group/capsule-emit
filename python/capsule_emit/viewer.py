# SPDX-License-Identifier: Apache-2.0
"""Capsule-native ledger viewer — zero engine imports.

Ports the display semantics from the hermes demo's viewer_transform.py
as pure capsule-dict functions.  Works directly on JSONL ledger records.

Public API
----------
render_table(capsules, *, verify_results=None, out=None, path="") -> None
render_html(capsules, *, verify_results=None, ledger_path="") -> str
"""
from __future__ import annotations

import html as _html
import sys
from typing import Any

__all__ = ["render_table", "render_html"]

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


# ---------------------------------------------------------------------------
# HTML renderer — single-file, no external deps
# ---------------------------------------------------------------------------

_HTML_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d1117; color: #c9d1d9;
    font-family: ui-monospace, 'Cascadia Code', 'Fira Code', monospace;
    font-size: 13px; padding: 20px;
  }
  h1 { color: #58a6ff; font-size: 16px; margin-bottom: 4px; }
  .meta { color: #8b949e; font-size: 11px; margin-bottom: 16px; }
  table { width: 100%; border-collapse: collapse; }
  th {
    background: #161b22; color: #8b949e; font-weight: 600;
    padding: 6px 10px; text-align: left; border-bottom: 1px solid #21262d;
    white-space: nowrap;
  }
  td { padding: 5px 10px; border-bottom: 1px solid #21262d; vertical-align: top; }
  tr:hover td { background: #161b22; }
  .refusal td { background: #2d1a1a; }
  .refusal td:first-child { border-left: 3px solid #f85149; }
  .refusal:hover td { background: #3d1f1f; }
  .tag {
    display: inline-block; padding: 1px 6px; border-radius: 10px;
    font-size: 10px; font-weight: 600; white-space: nowrap;
  }
  .tag-blocked  { background: #f8514933; color: #f85149; }
  .tag-executed { background: #23863633; color: #3fb950; }
  .tag-pending  { background: #d2992133; color: #d29922; }
  .tag-denied   { background: #f8514933; color: #f85149; }
  .tag-other    { background: #21262d;   color: #8b949e; }
  .verify-ok   { color: #3fb950; }
  .verify-fail { color: #f85149; }
  .verify-na   { color: #8b949e; }
  .cid { color: #8b949e; font-size: 11px; }
  .actor { color: #c9d1d9; }
  .actor-refused { color: #f85149; }
  .chain-label { color: #8b949e; font-size: 11px; }
  .gate-check { margin-top: 3px; font-size: 10px; color: #8b949e; }
  .gate-pass { color: #3fb950; }
  .gate-fail { color: #f85149; }
  .ts { color: #8b949e; font-size: 11px; }
"""

_TAG_CLASS = {
    "executed": "tag-executed",
    "blocked": "tag-blocked",
    "denied": "tag-denied",
    "pending": "tag-pending",
}


def _verdict_tag(verdict: str) -> str:
    if not verdict:
        return ""
    cls = _TAG_CLASS.get(verdict, "tag-other")
    return f'<span class="tag {cls}">{_html.escape(verdict)}</span>'


def _actor_html(actor: str, is_refusal: bool) -> str:
    cls = "actor-refused" if is_refusal else "actor"
    return f'<span class="{cls}">{_html.escape(actor)}</span>'


def _gate_checks_html(checks: list) -> str:
    if not checks:
        return ""
    parts = []
    for c in checks:
        name = _html.escape(str(c.get("name", "")))
        passed = c.get("passed", True)
        reason = c.get("reason") or ""
        icon = "✓" if passed else "✗"
        cls = "gate-pass" if passed else "gate-fail"
        line = f'<span class="{cls}">{icon} {name}</span>'
        if reason:
            line += f' <span style="color:#8b949e">({_html.escape(reason)})</span>'
        parts.append(line)
    return '<div class="gate-check">' + " &nbsp; ".join(parts) + "</div>"


def render_html(
    capsules: list[dict],
    *,
    verify_results: list | None = None,
    ledger_path: str = "",
) -> str:
    """Render a read-only single-file HTML browse of the ledger.

    Includes per-capsule verify status, actor lineage labels, and
    refusal highlighting.  Zero external dependencies — all CSS is inline.
    """
    vmap = _verify_map(capsules, verify_results)
    rows = [_row(cap, vmap.get(cap.get("capsule_id", ""))) for cap in capsules]

    label = _html.escape(ledger_path or "capsule ledger")

    body_rows = []
    for row in rows:
        tr_cls = ' class="refusal"' if row["is_refusal"] else ""

        cid = row["capsule_id"]
        cid_html = (
            f'<span class="cid">{_html.escape(cid[:16])}</span>'
            if cid != "?"
            else "<span>?</span>"
        )

        actor_h = _actor_html(row["actor"], row["is_refusal"])

        effect_parts = []
        if row["effect_type"]:
            effect_parts.append(_html.escape(row["effect_type"]))
        if row["effect_status"]:
            effect_parts.append(_html.escape(row["effect_status"]))
        effect_h = '<span style="color:#8b949e">:</span>'.join(effect_parts)

        gate_h = _gate_checks_html(row["gate_checks"])
        chain_h = f'<span class="chain-label">{_html.escape(row["chain_label"])}</span>' if row["chain_label"] else ""

        vok = row["verify_ok"]
        if vok is True:
            v_h = '<span class="verify-ok">✓ valid</span>'
        elif vok is False:
            v_h = '<span class="verify-fail">✗ invalid</span>'
        else:
            v_h = '<span class="verify-na">—</span>'

        ts_h = f'<span class="ts">{_html.escape(str(row["ts"])[:19])}</span>' if row["ts"] else ""

        verdict_h = _verdict_tag(row["verdict"])

        body_rows.append(
            f"  <tr{tr_cls}>"
            f"<td>{cid_html}</td>"
            f"<td>{_html.escape(row['action'])}</td>"
            f"<td>{actor_h}{gate_h}</td>"
            f"<td>{verdict_h}</td>"
            f"<td>{effect_h}</td>"
            f"<td>{chain_h}</td>"
            f"<td>{ts_h}</td>"
            f"<td>{v_h}</td>"
            f"</tr>"
        )

    n = len(rows)
    refusals = sum(1 for r in rows if r["is_refusal"])
    meta_str = f"{n} record(s)"
    if refusals:
        meta_str += f" &nbsp;·&nbsp; {refusals} refusal(s)"

    rows_html = "\n".join(body_rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{label}</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<h1>capsule ledger</h1>
<div class="meta">{label} &nbsp;·&nbsp; {meta_str}</div>
<table>
  <thead>
    <tr>
      <th>capsule id</th>
      <th>action</th>
      <th>actor</th>
      <th>verdict</th>
      <th>effect</th>
      <th>chain</th>
      <th>timestamp</th>
      <th>verify</th>
    </tr>
  </thead>
  <tbody>
{rows_html}
  </tbody>
</table>
</body>
</html>
"""
