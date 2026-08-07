# SPDX-License-Identifier: Apache-2.0
"""Demo permalink builder — withheld/bundle half.

The verify-surface viewer at ``<base-url>/v/<capsule_id>#<fragment>`` renders a
single-capsule detail view when the fragment decodes to a JSON object, and the
chain-navigation table when it decodes to a JSON array. A single-capsule link
where the presenter meant to paste the whole chain silently degrades to the
first case — that is the failure this module exists to make impossible: bundle
mode is the default whenever more than one capsule is supplied, not opt-in.

Disclosure (``--reveal``) is out of scope here — it depends on the not-yet-built
disclosure-envelope format ([aac-disclosure-envelope]). Every capsule produced
by this module is withheld-only.
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


def build_url(capsules: list[dict], *, base_url: str = DEFAULT_BASE_URL, bundle: bool) -> str:
    """Build the withheld verify-surface permalink.

    ``bundle=True`` encodes the JSON-array fragment (chain-navigation table);
    otherwise the single capsule object is encoded directly.
    """
    base_url = base_url.rstrip("/")
    anchor_id = _capsule_id_of(capsules[0])
    payload: Any = capsules if bundle else capsules[0]
    frag = base64.b64encode(json.dumps(payload).encode()).decode()
    return f"{base_url}/v/{anchor_id}#{frag}"
