# SPDX-License-Identifier: Apache-2.0
"""Demo permalink builder: a link that carries a finished Evidence Bundle.

A permalink carries an AAC Evidence Bundle (``evidence-bundle/v2``,
draft-mih-zhang-agent-disclosure-bundle) in the URL fragment, encoded with
that draft's §9 Fragment Codec: unpadded RFC 4648 base64url over the UTF-8 JCS
bytes of the whole Bundle (``agent_action_capsule.bundle.encode_fragment``).
The link opens the viewer's ``/bundle`` route, which decodes that codec.

:func:`build_url` takes the Bundle as it is and builds nothing: a proven one
from ``capsule-emit bundle`` (agent-action-capsule's producer over a ledger),
or, for loose Capsules that belong to no log, the unproven form
``agent_action_capsule.bundle.build_unproven_bundle`` makes, which carries no
log proof. ``capsule-emit permalink`` links a bundle file as it is
(``--bundle FILE``), or builds the unproven form from capsule files, with the
last one as ``root`` and any ``--reveal`` payload in the Bundle-level
``disclosures`` overlay.

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

#: There is no default verify surface: callers name the one they use. A
#: public one is, for example, ``https://verify.agentactioncapsule.org``.

#: Chromium refuses URLs longer than 2 MiB; a longer permalink never opens.
MAX_INLINE_URL_BYTES = 2 * 1024 * 1024


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
    bundle: dict,
    *,
    base_url: str,
    bundle_locations: list[str] | None = None,
    max_url_bytes: int = MAX_INLINE_URL_BYTES,
) -> str:
    """Build the verify-surface permalink for a finished Evidence Bundle:
    ``<base_url>/bundle#<fragment>``. ``base_url`` is the verify surface the
    caller chose; there is no default.

    The fragment is the §9 encoding of ``bundle``, unchanged. When that URL
    would exceed ``max_url_bytes`` the fragment is the pointer form instead,
    naming ``bundle_locations``; with no locations it raises
    :class:`PermalinkError`.
    """
    base_url = base_url.rstrip("/")
    url = f"{base_url}/bundle#{encode_fragment(bundle)}"
    if len(url) <= max_url_bytes:
        return url
    if not bundle_locations:
        raise PermalinkError(
            f"the permalink would be {len(url):,} bytes, over the {max_url_bytes:,}-byte "
            "limit browsers open; host the bundle and pass its URL with --bundle-location to emit "
            "a pointer permalink instead (write it with --bundle-out, or build a scoped, proven one "
            "with `capsule-emit bundle`)"
        )
    url = f"{base_url}/bundle#{encode_fragment(pointer_fragment(bundle, bundle_locations))}"
    if len(url) > max_url_bytes:
        raise PermalinkError("even the pointer permalink exceeds the URL limit; pass fewer locations")
    return url
