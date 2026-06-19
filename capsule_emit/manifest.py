# SPDX-License-Identifier: BSD-3-Clause
"""Manifest parser — declare-only, engine-free.

Reads a ``flows/<wicket>/manifest.md`` file and returns a :class:`ManifestDeclaration`
with the declared wicket_id, autonomy (default ``"narrate"``), effect type, and
constraint names.

``capsule-emit`` reads manifests to *declare* — no enforcement, no engine, no gate.
``gopher-ai`` reads the same file and *enforces*. This is the same-file upgrade path.

Safe-autonomy default: if ``autonomy`` is not declared, it defaults to ``"narrate"``
(the safest; the agent describes what it would do but does not execute).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ManifestDeclaration", "load_manifest", "find_manifest"]

_AUTONOMY_RE = re.compile(
    r"autonomy\s+[`'\"]?(\w+)[`'\"]?",
    re.IGNORECASE,
)
_EFFECT_RE = re.compile(
    r"^`(\w+)`\s*—\s*autonomy\s+[`'\"]?(\w+)[`'\"]?",
    re.IGNORECASE | re.MULTILINE,
)
_CONSTRAINT_ID_RE = re.compile(r"`([\w_]+)`")


@dataclass
class ManifestDeclaration:
    """The declared metadata from a manifest.md file (no enforcement)."""

    wicket_id: str
    title: str = ""
    autonomy: str = "narrate"
    effect_type: str = ""
    constraint_names: list[str] = field(default_factory=list)
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)


def _parse_yaml_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract simple YAML front matter (--- ... ---). No PyYAML dependency."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fm: dict[str, Any] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            # Strip inline comments
            if "#" in val:
                val = val[: val.index("#")].strip()
            # Unquote
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            # Coerce obvious types
            if val.lower() == "true":
                fm[key] = True
            elif val.lower() == "false":
                fm[key] = False
            elif val.isdigit():
                fm[key] = int(val)
            else:
                fm[key] = val
    return fm, body


def _extract_effect(body: str) -> tuple[str, str]:
    """Return (effect_type, autonomy) from the ## Effect section."""
    effect_section = re.search(r"##\s+Effect\s*\n(.*?)(?:\n##|\Z)", body, re.DOTALL)
    if not effect_section:
        return "", "narrate"
    section_text = effect_section.group(1)
    m = _EFFECT_RE.search(section_text)
    if m:
        return m.group(1), m.group(2).lower()
    # Fallback: look for backtick-quoted identifier on the first non-empty line
    for line in section_text.splitlines():
        line = line.strip()
        if not line:
            continue
        ids = _CONSTRAINT_ID_RE.findall(line)
        if ids:
            autonomy_m = _AUTONOMY_RE.search(line)
            return ids[0], autonomy_m.group(1).lower() if autonomy_m else "narrate"
        break
    return "", "narrate"


def _extract_constraints(body: str) -> list[str]:
    """Extract constraint id strings from the ## Constraints table."""
    constraints_section = re.search(
        r"##\s+Constraints.*?\n(.*?)(?:\n##|\Z)", body, re.DOTALL | re.IGNORECASE
    )
    if not constraints_section:
        return []
    names: list[str] = []
    for line in constraints_section.group(1).splitlines():
        if "|" not in line:
            continue
        # Table rows: | `id` | description | ... |
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if not cells:
            continue
        ids = _CONSTRAINT_ID_RE.findall(cells[0])
        if ids and not ids[0].startswith("-"):
            names.append(ids[0])
    return names


def load_manifest(path: str | Path) -> ManifestDeclaration:
    """Parse a manifest.md file and return a :class:`ManifestDeclaration`.

    Never raises on missing or malformed files — returns a safe default with
    ``autonomy="narrate"``.
    """
    p = Path(path)
    if not p.exists():
        wicket_id = p.parent.name if p.parent.name != "." else "unknown"
        return ManifestDeclaration(wicket_id=wicket_id)

    text = p.read_text(encoding="utf-8")
    fm, body = _parse_yaml_frontmatter(text)

    wicket_id = str(fm.get("wicket_id", p.parent.name))
    title = str(fm.get("title", ""))

    # Front matter can declare autonomy; Effect section takes precedence.
    fm_autonomy = str(fm.get("autonomy", "narrate")).lower()
    effect_type, body_autonomy = _extract_effect(body)
    autonomy = body_autonomy if body_autonomy != "narrate" else fm_autonomy
    constraint_names = _extract_constraints(body)

    return ManifestDeclaration(
        wicket_id=wicket_id,
        title=title,
        autonomy=autonomy,
        effect_type=effect_type,
        constraint_names=constraint_names,
        raw_frontmatter=fm,
    )


def find_manifest(flows_dir: str | Path, wicket_id: str) -> ManifestDeclaration | None:
    """Locate and load ``flows/<wicket_id>/manifest.md`` under *flows_dir*.

    Returns ``None`` if no such file exists (safe — caller can proceed without it).
    """
    p = Path(flows_dir) / wicket_id / "manifest.md"
    if not p.exists():
        return None
    return load_manifest(p)
