#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Messaging guard — fail the build on premature "unsigned/log-first"
repositioning copy in public materials.

[verify-entry-authorship-tristate-and-log] GATE, modeled on the neutrality
gate's (``.github/neutrality_scan.py``) fail-closed, span-based
allow-phrase design. Before this ticket, ``verify_bundle``/
``verify_store_signed`` graded an unsigned entry INVALID (indistinguishable
from a forged one) — so any public copy repositioning capsule-emit as
"just logging, now provable" would ship a promise the verifier itself
contradicted, and the first unsigned adopter would hit INVALID on contact.

This gate does NOT scan the whole repo — engineering docstrings and the
CHANGELOG legitimately name these phrases to explain the fix (this very
file does, in this docstring). It scans only the PITCH surfaces a
prospective adopter reads to evaluate positioning: the README, the
top-level narrative docs, and the public-facing explainer/translation
pages. See ``SCAN_PATHS`` below.

Unlike ``neutrality_scan.py``, the forbidden phrases here are not
confidential — they are named in this ticket's own text — so they are
hardcoded rather than supplied via a secret.

Usage: python .github/messaging_guard.py [ROOT=.]
       python .github/messaging_guard.py --self-test
Exit 0 = clean; 1 = forbidden repositioning phrase found (prints file:line).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

#: The pitch surfaces a prospective adopter reads — NOT the whole repo.
#: Engineering docstrings (capsule_emit/**) and CHANGELOG.md may legitimately
#: name these phrases to explain the fix; only positioning copy is gated.
SCAN_PATHS = (
    "README.md",
    "ADOPT.md",
    "TRANSLATION.md",
    "docs/",
)

#: Forbidden ONLY as a repositioning claim — see the GATE text in
#: [verify-entry-authorship-tristate-and-log]. Matched case-insensitively.
FORBIDDEN_PHRASES = (
    "just logging",
    "just-logging",
    "your log, now provable",
)

#: Already-public sentences that legitimately name a forbidden phrase to
#: explain or reference the gate itself (e.g. this file's own docstring, or
#: a doc discussing the history of the fix) — exempt ONLY within the span
#: of one of these phrases on the same line, not the whole line.
ALLOW_PHRASES = (
    'repositioning capsule-emit as "just logging',
    "unsigned/log-first",
)

_PATTERN = re.compile("|".join(re.escape(p) for p in FORBIDDEN_PHRASES), re.IGNORECASE)


def _line_offenders(line: str) -> list[str]:
    line_lower = line.lower()
    allow_spans: list[tuple[int, int]] = []
    for phrase in ALLOW_PHRASES:
        phrase_lower = phrase.lower()
        start = 0
        while True:
            pos = line_lower.find(phrase_lower, start)
            if pos < 0:
                break
            allow_spans.append((pos, pos + len(phrase_lower)))
            start = pos + 1

    hits: list[str] = []
    for m in _PATTERN.finditer(line):
        ms, me = m.start(), m.end()
        if any(s <= ms and me <= e for s, e in allow_spans):
            continue
        hits.append(m.group(0))
    return hits


def _candidate_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in SCAN_PATHS:
        p = root / rel
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(f for f in p.rglob("*.md") if f.is_file()))
    return files


def scan(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for token in _line_offenders(line):
                offenders.append(f"{path.relative_to(root)}:{i}: {token!r}")
    return offenders


def _run_self_tests() -> None:
    errors: list[str] = []

    hits = _line_offenders('We support "just logging" workflows.')
    if hits != ["just logging"]:
        errors.append(f"expected a bare mention to be flagged, got {hits!r}")

    hits = _line_offenders(
        'This gate exists because early copy risked repositioning capsule-emit as '
        '"just logging, now provable" before the verifier caught up.'
    )
    if hits:
        errors.append(f"expected the allow-phrase span to exempt this sentence, got {hits!r}")

    hits = _line_offenders("Your log, now provable — try it today.")
    if hits != ["Your log, now provable"]:
        errors.append(f"expected 'your log, now provable' to be flagged, got {hits!r}")

    if errors:
        print("messaging_guard self-test FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)
    print("messaging_guard self-test passed.")


def main(argv: list[str]) -> int:
    if argv[1:2] == ["--self-test"]:
        _run_self_tests()
        return 0

    root = Path(argv[1]) if len(argv) > 1 else Path(".")
    offenders = scan(root)
    if offenders:
        print(
            "messaging guard: forbidden repositioning phrase(s) found in public "
            "materials -- see [verify-entry-authorship-tristate-and-log]:",
            file=sys.stderr,
        )
        for o in offenders:
            print(f"  {o}", file=sys.stderr)
        return 1
    print("messaging guard: clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
