# SPDX-License-Identifier: Apache-2.0
"""Ledger read/write utilities and the ledger view renderer.

The ledger is a newline-delimited JSON (JSONL) file — one capsule dict per line.
``view()`` renders it as a human-readable chain table.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = ["append_to_ledger", "read_ledger", "view"]


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


def view(path: str | os.PathLike, *, out: Any = None) -> None:
    """Print a human-readable chain table for the ledger at *path*.

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
        # action_id is "tool-name/<uuid>" — show just the tool part
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
