# SPDX-License-Identifier: Apache-2.0
"""The Verification-stage comment, generated from a ledger.

Issues-first contribution workflows (goose's "Moving to issues as the new PRs",
2026-07-30, is the canonical statement) end at a **Verification** stage: a human
confirms the implementation works. Their PR rules ask the contributor to
"explain how the issue's verification plan was carried out" — but an
explanation is prose, and prose is exactly the thing the reviewer can no longer
afford to take on faith. This module generates the substrate for that stage: a
markdown comment built from the ledger the agent sealed while doing the work,
so the reviewer checks records, not claims.

Fail-closed by construction: every capsule is re-verified locally
(``capsule_emit.signing.verify_store_signed`` — ``agent_action_capsule.verify_store``'s
recompute + chain checks, plus the producer Ed25519 signature check that
verifier deliberately never performs, no network) at generation time, and no
comment is produced if any capsule fails. A contributor structurally cannot
hand a reviewer a verification comment whose records don't verify.

The viewer link carries an Evidence Bundle (``evidence-bundle/v2``): the one
the caller passes (``bundle=``, for example from ``capsule-emit bundle`` with
its log proof), which must not be INVALID under strict verification, or else
the capsules as agent-action-capsule's unproven bundle, and the comment says
that link carries no log proof.

Honesty rules (spec §3.2 posture): the comment reports ``attestation_mode``
exactly as each capsule carries it and makes **no anchoring claim of its own**
— if the capsules were anchored, that evidence lives in the capsules; if not,
the comment doesn't imply otherwise.
"""

from __future__ import annotations

from typing import Any

from .permalink import (
    PermalinkError,
    build_url,
    check_capsules,
    summarize,
)

__all__ = ["VerificationCommentError", "build_verification_comment"]


class VerificationCommentError(Exception):
    """Raised when a verification comment cannot honestly be produced."""


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


def _unproven(capsules: list[dict[str, Any]]) -> dict[str, Any]:
    """The capsules as an Evidence Bundle with no log proof, rooted at the
    last one, closure deep enough to reach every one."""
    from agent_action_capsule.bundle import BundleBuildError, build_unproven_bundle

    try:
        return build_unproven_bundle(
            capsules, root=capsules[-1].get("capsule_id"), closure_depth=len(capsules)
        )
    except BundleBuildError as exc:
        raise VerificationCommentError(str(exc)) from exc


def _proof_note(bundle: Any) -> str:
    """What a caller's bundle proves, for the link text; refuses an INVALID one."""
    from agent_action_capsule.bundle import verify_bundle

    verdict = verify_bundle(bundle, strict=True).verdict
    if verdict == "INVALID":
        raise VerificationCommentError(
            "the bundle is INVALID under strict verification — refusing to link it "
            "(run `capsule-emit verify --bundle` for the findings)"
        )
    if not isinstance(bundle.get("checkpoint"), dict):
        return f"this link carries no log proof ({verdict} under strict verification)"
    return f"this link carries a log proof under the log's signed checkpoint ({verdict} under strict verification)"


def build_verification_comment(
    capsules: list[dict[str, Any]],
    *,
    bundle: dict[str, Any] | None = None,
    issue_url: str | None = None,
    title: str = "Verification evidence",
    base_url: str | None = None,
    viewer_link: bool = True,
    ledger_name: str = "ledger.jsonl",
) -> str:
    """Build the Verification-stage markdown comment from verified capsules.

    Args:
        capsules: Ledger-ordered capsule dicts (e.g. from ``read_ledger``).
        bundle: The finished Evidence Bundle the viewer link carries, as it
            is (e.g. one ``capsule-emit bundle`` wrote, with its log proof).
            Without it the link carries ``capsules`` as agent-action-capsule's
            unproven bundle, and the comment says it has no log proof.
        issue_url: The Ready issue this work implements; rendered as the
            ``Implements:`` line so the evidence is issue-linked the way
            issues-first policies require.
        title: Comment heading.
        base_url: Verify-surface base URL for the viewer permalink (there
            is no default; required when ``viewer_link`` is true).
        viewer_link: Include the viewer permalink (set ``False`` for venues
            where an external viewer link is unwanted; the offline verify
            commands remain either way).
        ledger_name: Ledger filename to show in the offline verify commands.

    Raises:
        VerificationCommentError: if ``capsules`` is empty, any capsule fails
            local verification, or ``bundle`` is INVALID — a comment is never
            produced from records that don't verify.
    """
    if not capsules:
        raise VerificationCommentError("no capsules — an empty ledger has no evidence to show")

    results = check_capsules(capsules)
    failures = [(c, r) for c, r in zip(capsules, results) if not r.ok]
    if failures:
        details = "; ".join(
            f"{_short(c.get('capsule_id') or '')}: "
            + ("; ".join(f"{f.check}: {f.detail}" for f in r.errors) or "verification failed")
            for c, r in failures
        )
        raise VerificationCommentError(
            f"{len(failures)}/{len(capsules)} capsule(s) failed verify() — "
            f"refusing to build a verification comment ({details})"
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
        "this comment adds no claims of its own.",
        "",
        "Verify offline (no network, no trust in this comment):",
        "",
        "```bash",
        f"agent-action-capsule verify --store {ledger_name}",
        f"capsule-emit verify --store {ledger_name}",
        "```",
    ]
    if viewer_link:
        if not base_url:
            raise VerificationCommentError(
                "a viewer link needs the verify surface's base URL (--base-url, or base_url=); "
                "there is no default -- or pass viewer_link=False"
            )
        if bundle is None:
            linked, note = _unproven(capsules), "this link carries no log proof"
        else:
            linked, note = bundle, _proof_note(bundle)
        try:
            url = build_url(linked, base_url=base_url)
        except PermalinkError as exc:  # e.g. a ledger too large for an inline permalink
            raise VerificationCommentError(str(exc)) from exc
        lines += ["", f"[Open the Evidence Bundle in the verify viewer]({url}): {note}."]
    lines += [
        "",
        "<sub>Generated by `capsule-emit evidence`; every capsule re-verified "
        "locally at generation time (fail-closed).</sub>",
        "",
    ]
    return "\n".join(lines)
