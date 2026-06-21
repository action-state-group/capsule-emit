# SPDX-License-Identifier: Apache-2.0
"""Ledger read/write utilities and the ledger view renderer.

The ledger is a newline-delimited JSON (JSONL) file — one capsule dict per line.

Four rendering levels:

- ``view()``        — L1 one-line-per-capsule summary table (default)
- ``view_chains()`` — L2 tree grouped by chain.parent_capsule_id
- ``show()``        — L3 full single-capsule two-tier layout
- JSON passthrough  — L4 via CLI ``--json`` flag (not a function here)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = ["append_to_ledger", "read_ledger", "view", "view_chains", "show"]


def append_to_ledger(capsule: dict, path: str | os.PathLike = "ledger.jsonl") -> None:
    """Append a sealed capsule dict as a single JSON line."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(capsule, separators=(",", ":")) + "\n")


def read_ledger(path: str | os.PathLike) -> list[dict]:
    """Read all capsule records from a JSONL ledger file."""
    p = Path(path)
    if not p.exists():
        return []
    records = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# L1 — flat summary table
# ---------------------------------------------------------------------------

def view(path: str | os.PathLike, *, out: Any = None) -> None:
    """L1: one-line-per-capsule summary table.

    Args:
        path: Path to the JSONL ledger file.
        out: File-like object for output (defaults to stdout).
    """
    import sys

    if out is None:
        out = sys.stdout

    records = read_ledger(path)
    if not records:
        print(f"ledger: {path} — empty or not found", file=out)
        return

    col_id = 14
    col_action = 22
    col_op = 14
    col_effect = 22
    col_verdict = 12

    header = (
        f"{'capsule_id':<{col_id}}  "
        f"{'action':<{col_action}}  "
        f"{'operator':<{col_op}}  "
        f"{'effect/status':<{col_effect}}  "
        f"{'verdict':<{col_verdict}}  "
        f"{'chain'}"
    )
    print(f"\ncapsule-emit ledger: {path}  ({len(records)} record(s))\n", file=out)
    print(header, file=out)
    print("-" * len(header), file=out)

    for cap in records:
        cid = cap.get("capsule_id", "?")[:col_id]
        action_id = cap.get("action_id", "?")
        action = action_id.split("/")[0] if "/" in action_id else action_id
        action = action[:col_action]
        operator = cap.get("operator", "")[:col_op]

        eff = cap.get("effect", {}) or {}
        eff_str = ""
        if eff:
            eff_str = f"{eff.get('type', '')}:{eff.get('status', '')}"
        eff_str = eff_str[:col_effect]

        disp = cap.get("disposition", {}) or {}
        verdict = disp.get("verdict_class", "")[:col_verdict]

        chain = cap.get("chain", {}) or {}
        chain_str = ""
        if chain:
            parent = chain.get("parent_capsule_id", "")
            rel = chain.get("relation", "")
            chain_str = f"{rel}→{parent[:8]}…" if parent else ""

        print(
            f"{cid:<{col_id}}  "
            f"{action:<{col_action}}  "
            f"{operator:<{col_op}}  "
            f"{eff_str:<{col_effect}}  "
            f"{verdict:<{col_verdict}}  "
            f"{chain_str}",
            file=out,
        )
    print(file=out)


# ---------------------------------------------------------------------------
# L2 — chain tree
# ---------------------------------------------------------------------------

def view_chains(path: str | os.PathLike, *, out: Any = None) -> None:
    """L2: chain-tree view — groups capsules by their chain parent.

    Roots (capsules with no parent) are printed first; each confirmed/chained
    child is indented under its parent.  Orphaned children (parent not in
    ledger) appear at the end under an ``[orphaned]`` header.

    Args:
        path: Path to the JSONL ledger file.
        out: File-like object for output (defaults to stdout).
    """
    import sys

    if out is None:
        out = sys.stdout

    records = read_ledger(path)
    if not records:
        print(f"ledger: {path} — empty or not found", file=out)
        return

    by_id: dict[str, dict] = {c["capsule_id"]: c for c in records if "capsule_id" in c}
    children: dict[str, list[str]] = {}
    for cap in records:
        chain = cap.get("chain") or {}
        parent = chain.get("parent_capsule_id")
        if parent:
            children.setdefault(parent, []).append(cap["capsule_id"])

    printed: set[str] = set()

    def _action(cap: dict) -> str:
        aid = cap.get("action_id", "?")
        return aid.split("/")[0] if "/" in aid else aid

    def _verdict(cap: dict) -> str:
        return (cap.get("disposition") or {}).get("verdict_class", "")

    def _model(cap: dict) -> str:
        ma = cap.get("model_attestation") or {}
        mid = ma.get("model_id") or ""
        prov = ma.get("provider") or ""
        if mid:
            return f"{prov}/{mid}" if prov else mid
        return ""

    def _print_node(cid: str, depth: int) -> None:
        if cid in printed:
            return
        printed.add(cid)
        cap = by_id.get(cid, {})
        indent = "  " * depth
        connector = "└─ " if depth else ""
        action = _action(cap)
        verdict = _verdict(cap)
        model_str = _model(cap)
        short_id = cid[:12]
        chain = cap.get("chain") or {}
        rel = chain.get("relation", "")
        rel_tag = f"[{rel}] " if rel and depth else ""
        model_tag = f"  model={model_str}" if model_str else ""
        print(
            f"{indent}{connector}{short_id}…  {action}  {rel_tag}{verdict}{model_tag}",
            file=out,
        )
        for child_id in children.get(cid, []):
            _print_node(child_id, depth + 1)

    print(f"\ncapsule-emit ledger (chains): {path}  ({len(records)} record(s))\n", file=out)

    roots = [c["capsule_id"] for c in records if not (c.get("chain") or {}).get("parent_capsule_id") and "capsule_id" in c]
    for root_id in roots:
        _print_node(root_id, 0)

    orphans = [cid for cid in by_id if cid not in printed]
    if orphans:
        print("\n[orphaned — parent not in ledger]", file=out)
        for cid in orphans:
            _print_node(cid, 1)

    print(file=out)


# ---------------------------------------------------------------------------
# L3 — full single-capsule detail
# ---------------------------------------------------------------------------

def show(
    path: str | os.PathLike,
    capsule_id: str,
    *,
    out: Any = None,
) -> bool:
    """L3: two-tier detail view for a single capsule.

    Prints the top-level fields first, then the nested attestation and chain
    blocks.  Returns ``True`` when found, ``False`` when not.

    Args:
        path: Path to the JSONL ledger file.
        capsule_id: Full or prefix (≥8 chars) capsule_id to look up.
        out: File-like object for output (defaults to stdout).
    """
    import sys

    if out is None:
        out = sys.stdout

    records = read_ledger(path)
    cap = None
    for rec in records:
        rid = rec.get("capsule_id", "")
        if rid == capsule_id or rid.startswith(capsule_id):
            cap = rec
            break

    if cap is None:
        print(f"capsule {capsule_id!r} not found in {path}", file=out)
        return False

    cid = cap.get("capsule_id", "?")
    print(f"\n── capsule {cid} ──\n", file=out)

    # Tier 1: top-level identity fields
    _field(out, "format_version", cap.get("format_version"))
    _field(out, "operator", cap.get("operator"))
    _field(out, "developer", cap.get("developer"))
    action_id = cap.get("action_id", "")
    action_name = action_id.split("/")[0] if "/" in action_id else action_id
    _field(out, "action", action_name)
    _field(out, "action_id", action_id)
    _field(out, "ts", cap.get("ts"))

    # Tier 2: nested blocks
    _block(out, "disposition", cap.get("disposition"))
    _block(out, "effect", cap.get("effect"))
    _block(out, "chain", cap.get("chain"))

    ma = cap.get("model_attestation") or {}
    _field(out, "model_attestation.model_id", ma.get("model_id"))
    _field(out, "model_attestation.provider", ma.get("provider"))
    ca = ma.get("compute_attestation") or {}
    if ca:
        _block(out, "compute_attestation", ca)

    assurance = cap.get("assurance") or {}
    if assurance:
        _block(out, "assurance", assurance)

    print(file=out)
    return True


def _field(out: Any, label: str, value: Any) -> None:
    if value is None or value == "" or value == {}:
        return
    print(f"  {label:<32} {value}", file=out)


def _block(out: Any, label: str, block: Any) -> None:
    if not block:
        return
    print(f"  {label}:", file=out)
    if isinstance(block, dict):
        for k, v in block.items():
            if v is not None and v != "":
                print(f"    {k:<30} {v}", file=out)
    else:
        print(f"    {block}", file=out)
