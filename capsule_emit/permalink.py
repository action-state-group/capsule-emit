# SPDX-License-Identifier: Apache-2.0
"""Demo permalink builder — withheld/bundle half.

Every permalink carries an AAC Evidence Bundle (``evidence-bundle/v2``,
draft-mih-zhang-agent-disclosure-bundle-00) in the URL fragment, encoded with
that draft's §9 Fragment Codec: unpadded RFC 4648 base64url over the UTF-8 JCS
bytes of the whole Bundle (``agent_action_capsule.bundle.encode_fragment``).
One capsule is a Bundle of one; a chain is a Bundle whose ``root`` is the last
capsule in ledger order. The link opens the viewer's ``/bundle`` route, which
decodes that codec. (Before this, the fragment was padded standard base64 over
``json.dumps`` of a bare capsule or array, which no conformant decoder reads.)

Disclosure (``--reveal``) goes in the Bundle-level ``disclosures`` overlay,
``{capsule_id: {member: preimage}}`` (the draft's §5). Enclosed capsules are
never wrapped or altered, so each one's ``capsule_id`` still recomputes.

A URL past ``MAX_INLINE_URL_BYTES`` (Chromium's 2 MiB URL cap) does not open,
so above it the fragment carries a pointer instead of the Bundle:
``{"bundle_ref": {"digest": <bundle digest>, "root": <capsule_id>,
"locations": [<URI>, ...]}}``, encoded with the same codec. ``locations`` are
routes the producer chooses and hosts; there is no default. With no location,
an oversize permalink is refused rather than emitted unopenable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_action_capsule.bundle import bundle_digest, encode_fragment

DEFAULT_BASE_URL = "https://verify.agentactioncapsule.org"

#: Chromium refuses URLs longer than 2 MiB; a longer permalink never opens.
MAX_INLINE_URL_BYTES = 2 * 1024 * 1024

BUNDLE_VERSION = "2"
BUNDLE_KIND = "evidence-bundle/v2"


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
    capsule, in ledger order, with store-level chain checks, plus the producer
    signature check ``verify_store`` itself never performs (see
    ``capsule_emit.signing.verify_store_signed``). No network. Returns the
    list of ``VerificationResult`` — callers decide what a failure means."""
    from .signing import verify_store_signed

    return verify_store_signed(capsules)


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


def _citation_targets(capsule: dict) -> list[str]:
    """The capsule digests a record cites: its chain parent and every
    ``agent-action-capsule`` SHA-256 reference (the Bundle draft's §4)."""
    targets: list[str] = []
    chain = capsule.get("chain")
    if isinstance(chain, dict) and isinstance(chain.get("parent_capsule_id"), str):
        targets.append(chain["parent_capsule_id"])
    for ref in capsule.get("references") or []:
        if (
            isinstance(ref, dict)
            and ref.get("type") == "agent-action-capsule"
            and ref.get("digest_alg") == "SHA-256"
            and isinstance(ref.get("digest"), str)
        ):
            targets.append(ref["digest"])
    return targets


def _completeness(root_id: str, capsules: list[dict]) -> dict:
    """Declare citation closure from ``root_id`` deep enough to reach every
    supplied record, listing each cited capsule that is not supplied."""
    by_id = {_capsule_id_of(c): c for c in capsules}
    depth = len(capsules)
    missing: list[str] = []
    frontier = [root_id]
    for _ in range(depth):
        next_frontier: list[str] = []
        for source_id in frontier:
            for target in _citation_targets(by_id[source_id]):
                if target in by_id:
                    next_frontier.append(target)
                elif target not in missing:
                    missing.append(target)
        frontier = next_frontier
    completeness: dict[str, Any] = {
        "closure_depth": depth,
        "records_mode": "declared_incomplete" if missing else "complete",
    }
    if missing:
        completeness["missing"] = missing
    return completeness


def build_bundle(
    capsules: list[dict],
    *,
    bundle: bool,
    disclosures: dict[str, Any] | None = None,
) -> dict:
    """Build the ``evidence-bundle/v2`` object a permalink carries.

    ``bundle=False`` makes a Bundle of one from ``capsules[0]``; otherwise
    every capsule is a record and the last one (in ledger order) is ``root``.

    ``disclosures`` shape depends on ``bundle``:

    - ``bundle=False``: a flat ``{field: payload}`` dict (e.g.
      ``{"agent_input": {...}, "agent_output": {...}}``) for the single
      capsule. Requires exactly one capsule.
    - ``bundle=True``: a ``{capsule_id: {field: payload}}`` dict. Every key
      must match a capsule_id present in ``capsules``.

    Either way it lands in the Bundle-level ``disclosures`` overlay; items
    with no entry are WITHHELD.
    """
    if disclosures is not None and not bundle and len(capsules) != 1:
        raise PermalinkError("disclosures require exactly one capsule (or bundle=True)")
    records = capsules if bundle else capsules[:1]
    ids = [_capsule_id_of(c) for c in records]
    if len(set(ids)) != len(ids):
        raise PermalinkError("duplicate capsule_id in the bundle")
    root_id = ids[-1]
    overlay: dict[str, Any] = {}
    if disclosures:
        if bundle:
            unknown = sorted(set(disclosures) - set(ids))
            if unknown:
                raise PermalinkError(
                    f"disclosures given for capsule_id(s) not in the bundle: {unknown}"
                )
            overlay = dict(disclosures)
        else:
            overlay = {root_id: disclosures}
    out: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "bundle_kind": BUNDLE_KIND,
        "root": root_id,
        "records": records,
        "completeness": _completeness(root_id, records),
    }
    if overlay:
        out["disclosures"] = overlay
    return out


def pointer_fragment(bundle_obj: dict, locations: list[str]) -> dict:
    """The pointer form a fragment carries in place of an oversize Bundle."""
    if not locations:
        raise PermalinkError("a bundle pointer needs at least one location")
    return {
        "bundle_ref": {
            "digest": bundle_digest(bundle_obj),
            "root": bundle_obj["root"],
            "locations": list(locations),
        }
    }


def resolve_pointer(pointer: dict, fetched: Any) -> dict:
    """Accept a Bundle fetched from a pointer's location only if its bundle
    digest and ``root`` equal the pointer's. Locations are routes, never
    identity, so the fetched bytes prove nothing until this check passes."""
    ref = pointer.get("bundle_ref") if isinstance(pointer, dict) else None
    if not isinstance(ref, dict):
        raise PermalinkError("not a bundle pointer")
    if not isinstance(fetched, dict):
        raise PermalinkError("fetched bundle is not a JSON object")
    if bundle_digest(fetched) != ref.get("digest"):
        raise PermalinkError("fetched bundle does not match the pointer's digest")
    if fetched.get("root") != ref.get("root"):
        raise PermalinkError("fetched bundle's root does not match the pointer's root")
    return fetched


def build_url(
    capsules: list[dict],
    *,
    base_url: str = DEFAULT_BASE_URL,
    bundle: bool,
    disclosures: dict[str, Any] | None = None,
    bundle_locations: list[str] | None = None,
    max_url_bytes: int = MAX_INLINE_URL_BYTES,
) -> str:
    """Build the verify-surface permalink: ``<base_url>/bundle#<fragment>``.

    The fragment is the §9 encoding of :func:`build_bundle`'s Bundle (see it
    for ``bundle`` and ``disclosures``). When that URL would exceed
    ``max_url_bytes`` the fragment is the pointer form instead, naming
    ``bundle_locations``; with no locations it raises :class:`PermalinkError`.
    """
    base_url = base_url.rstrip("/")
    bundle_obj = build_bundle(capsules, bundle=bundle, disclosures=disclosures)
    url = f"{base_url}/bundle#{encode_fragment(bundle_obj)}"
    if len(url) <= max_url_bytes:
        return url
    if not bundle_locations:
        raise PermalinkError(
            f"the permalink would be {len(url):,} bytes, over the {max_url_bytes:,}-byte "
            "limit browsers open; host the bundle (write it with --bundle-out) and pass "
            "its URL with --bundle-location to emit a pointer permalink instead"
        )
    url = f"{base_url}/bundle#{encode_fragment(pointer_fragment(bundle_obj, bundle_locations))}"
    if len(url) > max_url_bytes:
        raise PermalinkError("even the pointer permalink exceeds the URL limit; pass fewer locations")
    return url
