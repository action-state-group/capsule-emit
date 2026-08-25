# SPDX-License-Identifier: Apache-2.0
"""Canonicalization-aware Capsule ID computation.

**draft-04 reversal ([capsule-cose-sign1], 2026-08-24):** ``capsule_id`` is a
pure, signer-independent content address again. It excludes only
``capsule_id`` itself plus capsule-emit's own ledger-line bookkeeping fields
(``signature``, ``key_id`` — the COSE_Sign1 producer envelope and the raw
public key that produced it; see ``capsule_emit.signing``), which are never
members of the neutral Capsule object and are only ever added to the dict
*after* ``capsule_id`` is computed. Everything else — including ``chain`` and
``canonicalization_id`` — is committed into the preimage under the declared
``"jcs"`` algorithm, closing the pre-reversal gap where a chain link
(``parent_capsule_id``/``relation``) was unauthenticated. ``"jcs-n"`` (the
vintage absent-field-normalized construction, which also excluded ``chain``)
remains verification-only, for records minted before this reversal.
"""

from __future__ import annotations

import hashlib
from typing import Any

from agent_action_capsule.canonical import jcs, normalize

VINTAGE_CANONICALIZATION_ID = "jcs-n"
CANONICALIZATION_JCS = "jcs"

#: capsule-emit's own ledger-line bookkeeping — the COSE_Sign1 producer
#: envelope and its signing key_id. Never members of the neutral Capsule
#: object, so never part of any capsule_id preimage, under either algorithm.
_LOCAL_ONLY_FIELDS = ("signature", "key_id")

#: Excluded ONLY under the vintage ``jcs-n`` absent-field construction.
#: Declared-``jcs`` capsules commit ``chain`` — see the module docstring.
_VINTAGE_ONLY_EXCLUDED_FIELDS = ("chain",)


class UnsupportedCanonicalizationError(ValueError):
    """The declared algorithm cannot be recomputed from a capsule object."""


def resolve_canonicalization_id(capsule: dict[str, Any]) -> Any:
    """Resolve an absent binding slot to the pre-slot ``jcs-n`` algorithm.

    Legacy normalization removes null-valued members, so an explicit JSON
    ``null`` has the same digest meaning as an omitted slot. Other false-y
    values, especially the empty string, remain declared values and fail
    closed during dispatch.
    """
    declared = capsule.get("canonicalization_id")
    return VINTAGE_CANONICALIZATION_ID if declared is None else declared


def canonical_capsule_bytes(capsule: dict[str, Any]) -> bytes:
    """Return the canonical Capsule ID preimage selected by the capsule.

    Records without a ``canonicalization_id`` predate the binding slot and
    retain the legacy ``jcs-n`` interpretation, which excludes both
    ``capsule_id`` and ``chain`` (§5.1 vintage rule). The ``jcs`` (format-4)
    profile excludes only ``capsule_id`` — ``chain`` IS committed, matching
    ``agent_action_capsule.canonical.compute_capsule_id``. ``as-transmitted``
    is not available here because parsing a wire record discards its
    original bytes.
    """
    algorithm = resolve_canonicalization_id(capsule)
    canonical = {
        key: value
        for key, value in capsule.items()
        if key != "capsule_id" and key not in _LOCAL_ONLY_FIELDS
    }

    if algorithm == "jcs-n":
        canonical = {k: v for k, v in canonical.items() if k not in _VINTAGE_ONLY_EXCLUDED_FIELDS}
        return jcs(normalize(canonical))
    if algorithm == "jcs":
        return jcs(canonical)
    raise UnsupportedCanonicalizationError(
        f"cannot compute capsule_id with canonicalization_id {algorithm!r}"
    )


def compute_capsule_id(capsule: dict[str, Any]) -> str:
    """Return the SHA-256 Capsule ID for the capsule's declared algorithm."""
    return hashlib.sha256(canonical_capsule_bytes(capsule)).hexdigest()
