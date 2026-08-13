# SPDX-License-Identifier: Apache-2.0
"""Demo permalink builder — withheld/bundle half.

The verify-surface viewer at ``<base-url>/v/<capsule_id>#<fragment>`` renders a
single-capsule detail view when the fragment decodes to a JSON object, and the
chain-navigation table when it decodes to a JSON array. A single-capsule link
where the presenter meant to paste the whole chain silently degrades to the
first case — that is the failure this module exists to make impossible: bundle
mode is the default whenever more than one capsule is supplied, not opt-in.

Disclosure (``--reveal``) wraps a single capsule in the Disclosure Envelope shape
the verify-surface viewer already reads (``{"capsule": <unmodified capsule>,
"disclosures": {"agent_input": ..., "agent_output": ...}}`` —
draft-mih-scitt-agent-action-capsule-disclosure-envelope-00, landed in the viewer
via scitt-cose#27/[aac-disclosure-envelope]). It is single-capsule only: the
array-fragment bundle path reads ``capsule_id``/``action_type``/``disposition``
directly off each array item and hashes the *whole* array item for the Integrity
check, so a per-item envelope wrapper there doesn't fail loud — it silently
un-recognizes the capsule_id and skips that record's Integrity/Sequence check
entirely (confirmed empirically), which is worse than a hard failure. Don't wrap
bundle items in the envelope; disclose via individual permalinks instead.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://verify.agentactioncapsule.org"


class PermalinkError(Exception):
    """Raised when a permalink cannot be safely produced."""


def _load_json_file(path: str | os.PathLike) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_capsules(
    *,
    capsule_files: list[str] | None = None,
    ledger_path: str | None = None,
    from_run: str | None = None,
) -> list[dict]:
    """Resolve exactly one of the three input modes to a flat, ledger-ordered capsule list."""
    given = [bool(capsule_files), bool(ledger_path), bool(from_run)]
    if sum(given) > 1:
        raise PermalinkError(
            "specify exactly one input: capsule JSON file(s), --ledger, or --from-run"
        )

    if capsule_files:
        capsules = [_load_json_file(p) for p in capsule_files]
    elif ledger_path:
        from .ledger import read_ledger

        capsules = read_ledger(ledger_path)
    elif from_run:
        run_dir = Path(from_run)
        ledger_candidate = run_dir / "ledger.jsonl"
        if ledger_candidate.exists():
            from .ledger import read_ledger

            capsules = read_ledger(ledger_candidate)
        else:
            json_files = sorted(run_dir.glob("*.json"))
            if not json_files:
                raise PermalinkError(
                    f"--from-run {from_run}: no ledger.jsonl and no *.json capsule files found"
                )
            capsules = [_load_json_file(p) for p in json_files]
    else:
        raise PermalinkError(
            "no capsules given — pass capsule JSON file(s), --ledger PATH, or --from-run DIR"
        )

    if not capsules:
        raise PermalinkError("no capsules found in the given source")
    return capsules


def _statements_dir_candidates(
    *,
    capsule_files: list[str] | None,
    ledger_path: str | None,
    from_run: str | None,
) -> list[Path]:
    """Directories to check for ``signed-statements/<capsule_id>.cose``.

    Matches the ``<ledger_dir>/signed-statements/`` convention (see
    ``capsule_sidecar.py``'s ``NodeState.statements_dir``): ``ledger_dir`` is
    the directory holding ``capsules.jsonl``/``ledger.jsonl``.
    """
    if ledger_path:
        return [Path(ledger_path).parent]
    if from_run:
        return [Path(from_run)]
    if capsule_files:
        # dedupe while preserving order
        seen: dict[Path, None] = {}
        for f in capsule_files:
            seen.setdefault(Path(f).parent, None)
        return list(seen)
    return []


def find_signed_statement(
    capsule: dict,
    *,
    capsule_files: list[str] | None = None,
    ledger_path: str | None = None,
    from_run: str | None = None,
) -> bytes | None:
    """Look up the COSE_Sign1 bytes for ``capsule``, if any sit on disk."""
    capsule_id = _capsule_id_of(capsule)
    for base in _statements_dir_candidates(
        capsule_files=capsule_files, ledger_path=ledger_path, from_run=from_run
    ):
        candidate = base / "signed-statements" / f"{capsule_id}.cose"
        if candidate.exists():
            return candidate.read_bytes()
    return None


def embed_signed_statements(
    capsules: list[dict],
    *,
    capsule_files: list[str] | None = None,
    ledger_path: str | None = None,
    from_run: str | None = None,
) -> tuple[list[dict], int]:
    """Return capsules with a base64 ``signed_statement`` field added wherever a
    matching ``signed-statements/<capsule_id>.cose`` file is found on disk.

    Capsules with no matching file are returned unmodified (not an error —
    ``--with-statements`` is best-effort embedding, not a requirement that
    every capsule have one). Returns ``(capsules, matched_count)``.

    Known consequence, confirmed empirically (not fixed here — it's viewer/spec
    territory, gated by [viewer-authenticity-never-passes]): ``signed_statement``
    lands as a sibling top-level key on the capsule, matching what the current
    ``checkAuthenticity`` reads (``capsules.some(c => c.signed_statement)``, a
    flat per-item check, unwrapped). ``compute_capsule_id`` / its JS twin
    ``computeCapsuleId`` hash every key except ``capsule_id``/``chain`` — they do
    NOT exempt ``signed_statement`` — so a capsule embedded this way fails its own
    digest recompute (confirmed against this repo's ``check_capsules()``: a valid
    capsule flips from ok=True to ok=False the moment ``signed_statement`` is
    added). Whoever resolves the Authenticity decision also needs to decide how
    ``signed_statement`` is excluded from the digest (extend the linkage-field
    exemption, or move to an envelope shape — see the bundle-envelope warning in
    this module's docstring, since that path has its own documented failure mode).
    Do NOT run this repo's own ``--check``/``check_capsules()`` against the
    embedded output for that reason; CLI order is check-then-embed.
    """
    out = []
    matched = 0
    for cap in capsules:
        raw = find_signed_statement(
            cap, capsule_files=capsule_files, ledger_path=ledger_path, from_run=from_run
        )
        if raw is None:
            out.append(cap)
            continue
        matched += 1
        out.append({**cap, "signed_statement": base64.b64encode(raw).decode()})
    return out, matched


def check_capsules(capsules: list[dict]) -> list[Any]:
    """Run the real, local ``agent_action_capsule.verify()`` (recompute+check) on every
    capsule, in ledger order, with store-level chain checks. No network. Returns the
    list of ``VerificationResult`` — callers decide what a failure means."""
    from agent_action_capsule import verify_store

    return verify_store(capsules)


def _capsule_id_of(capsule: dict) -> str:
    return capsule.get("capsule_id") or capsule.get("capsuleId") or "<no-capsule_id>"


def _verdict_of(capsule: dict) -> str:
    disposition = capsule.get("disposition") or {}
    return disposition.get("verdict_class") or disposition.get("decision") or "?"


def summarize(capsules: list[dict]) -> str:
    """One-line description of what the produced URL will render."""
    if len(capsules) == 1:
        cap = capsules[0]
        return f"1 capsule — {_verdict_of(cap)} ({_capsule_id_of(cap)[:8]})"
    chain = " → ".join(_verdict_of(c) for c in capsules)
    ids = " → ".join(_capsule_id_of(c)[:8] for c in capsules)
    return f"{len(capsules)} capsules — chain: {chain} ({ids})"


def build_url(
    capsules: list[dict],
    *,
    base_url: str = DEFAULT_BASE_URL,
    bundle: bool,
    disclosures: dict[str, Any] | None = None,
) -> str:
    """Build the verify-surface permalink.

    ``bundle=True`` encodes the JSON-array fragment (chain-navigation table);
    otherwise the single capsule object is encoded directly.

    ``disclosures``, when given, wraps the single capsule in the Disclosure
    Envelope shape (``{"capsule": ..., "disclosures": ...}``) instead of the
    bare capsule — e.g. ``{"agent_input": {...}, "agent_output": {...}}``.
    Requires ``bundle=False`` and exactly one capsule (see module docstring
    for why bundle-level disclosure isn't offered: it silently produces a
    vacuous, not failed, Integrity check in the current viewer).
    """
    base_url = base_url.rstrip("/")
    anchor_id = _capsule_id_of(capsules[0])
    if disclosures is not None:
        if bundle:
            raise PermalinkError(
                "disclosures require bundle=False — see build_url()/module docstring: "
                "the array-fragment bundle path doesn't read a per-item Disclosure "
                "Envelope and silently skips that record's Integrity check instead of "
                "failing loud. Use an individual (non-bundle) permalink per capsule."
            )
        if len(capsules) != 1:
            raise PermalinkError("disclosures require exactly one capsule")
        payload: Any = {"capsule": capsules[0], "disclosures": disclosures}
    else:
        payload = capsules if bundle else capsules[0]
    frag = base64.b64encode(json.dumps(payload).encode()).decode()
    return f"{base_url}/v/{anchor_id}#{frag}"
