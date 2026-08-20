# SPDX-License-Identifier: Apache-2.0
"""Verification-evidence bundle — the Verification-stage comment, generated from a ledger.

Issues-first contribution workflows (goose's "Moving to issues as the new PRs",
2026-07-30, is the canonical statement) end at a **Verification** stage: a human
confirms the implementation works. Their PR rules ask the contributor to
"explain how the issue's verification plan was carried out" — but an
explanation is prose, and prose is exactly the thing the reviewer can no longer
afford to take on faith. This module generates the substrate for that stage: a
markdown evidence comment built from the ledger the agent sealed while doing
the work, so the reviewer checks records, not claims.

Fail-closed by construction: every capsule is re-verified locally
(``agent_action_capsule.verify_store`` — recompute + chain checks, no network)
at generation time, and no bundle is produced if any capsule fails. A
contributor structurally cannot hand a reviewer an evidence comment whose
records don't verify.

Honesty rules (spec §3.2 posture): the bundle reports ``attestation_mode``
exactly as each capsule carries it and makes **no anchoring claim of its own**
— if the capsules were anchored, that evidence lives in the capsules; if not,
the bundle doesn't imply otherwise.
"""

from __future__ import annotations

from typing import Any

from .permalink import (
    DEFAULT_BASE_URL,
    PermalinkError,
    build_url,
    check_capsules,
    summarize,
)

__all__ = ["EvidenceError", "build_evidence_markdown"]


class EvidenceError(Exception):
    """Raised when an evidence bundle cannot honestly be produced."""


def _short(capsule_id: str) -> str:
    return capsule_id[:8] if capsule_id else "?"


def _action_name(capsule: dict) -> str:
    """``submit_order/6f3a…`` → ``submit_order`` (the adapter's action_id idiom)."""
    action_id = capsule.get("action_id") or "?"
    return action_id.split("/", 1)[0]


def _row(index: int, capsule: dict) -> str:
    disposition = capsule.get("disposition") or {}
    effect = capsule.get("effect") or {}
    return (
        f"| {index} "
        f"| `{_action_name(capsule)}` "
        f"| {capsule.get('action_type') or '?'} "
        f"| {disposition.get('verdict_class') or disposition.get('decision') or '?'} "
        f"| {effect.get('status') or '—'} "
        f"| `{_short(capsule.get('capsule_id') or '')}` |"
    )


def build_evidence_markdown(
    capsules: list[dict[str, Any]],
    *,
    issue_url: str | None = None,
    title: str = "Verification evidence",
    base_url: str = DEFAULT_BASE_URL,
    viewer_link: bool = True,
    ledger_name: str = "ledger.jsonl",
) -> str:
    """Build the Verification-stage markdown comment from verified capsules.

    Args:
        capsules: Ledger-ordered capsule dicts (e.g. from ``read_ledger``).
        issue_url: The Ready issue this work implements; rendered as the
            ``Implements:`` line so the evidence is issue-linked the way
            issues-first policies require.
        title: Comment heading.
        base_url: Verify-surface base URL for the viewer permalink.
        viewer_link: Include the bundle permalink (set ``False`` for venues
            where an external viewer link is unwanted; the offline verify
            commands remain either way).
        ledger_name: Ledger filename to show in the offline verify commands.

    Raises:
        EvidenceError: if ``capsules`` is empty or any capsule fails local
            verification — a bundle is never produced from records that don't
            verify.
    """
    if not capsules:
        raise EvidenceError("no capsules — an empty ledger has no evidence to bundle")

    results = check_capsules(capsules)
    failures = [(c, r) for c, r in zip(capsules, results) if not r.ok]
    if failures:
        details = "; ".join(
            f"{_short(c.get('capsule_id') or '')}: "
            + ("; ".join(f"{f.check}: {f.detail}" for f in r.errors) or "verification failed")
            for c, r in failures
        )
        raise EvidenceError(
            f"{len(failures)}/{len(capsules)} capsule(s) failed verify() — "
            f"refusing to build an evidence bundle ({details})"
        )

    attestation_modes = sorted(
        {(c.get("assurance") or {}).get("attestation_mode") or "unstated" for c in capsules}
    )

    lines: list[str] = [f"## {title}", ""]
    if issue_url:
        lines += [f"Implements: {issue_url}", ""]
    lines += [
        summarize(capsules),
        "",
        "| # | action | type | verdict | effect | capsule_id |",
        "|---|--------|------|---------|--------|------------|",
    ]
    lines += [_row(i, c) for i, c in enumerate(capsules, start=1)]
    lines += [
        "",
        f"Attestation mode: {', '.join(attestation_modes)} — as carried by each capsule; "
        "this bundle adds no claims of its own.",
        "",
        "Verify offline (no network, no trust in this comment):",
        "",
        "```bash",
        f"agent-action-capsule verify --store {ledger_name}",
        f"capsule-emit permalink --ledger {ledger_name} --check",
        "```",
    ]
    if viewer_link:
        try:
            url = build_url(capsules, base_url=base_url, bundle=len(capsules) > 1)
        except PermalinkError as exc:  # pragma: no cover — inputs already validated
            raise EvidenceError(str(exc)) from exc
        lines += ["", f"[Open the capsule chain in the verify viewer]({url})"]
    lines += [
        "",
        "<sub>Generated by `capsule-emit evidence`; every capsule re-verified "
        "locally at generation time (fail-closed).</sub>",
        "",
    ]
    return "\n".join(lines)
