# SPDX-License-Identifier: Apache-2.0
"""Canonicalization-aware Capsule ID computation.

The upstream ``agent_action_capsule`` helper implements the legacy ``jcs-n``
construction only. Capsules emitted here carry a ``canonicalization_id``
binding slot, so every producer and verifier path must dispatch through that
declared algorithm instead of treating the field as a label.
"""

from __future__ import annotations

import hashlib
from typing import Any

from agent_action_capsule.canonical import CHAIN_LINKAGE_FIELDS, jcs, normalize

VINTAGE_CANONICALIZATION_ID = "jcs-n"

#: Fields excluded from the ``jcs`` (format-4) Capsule-ID preimage: only
#: ``capsule_id`` itself. Unlike the vintage ``jcs-n`` profile, ``chain`` IS
#: committed under ``jcs`` — matches ``agent_action_capsule.canonical.compute_capsule_id``'s
#: own algorithm-conditional exclusion (present ``canonicalization_id`` removes
#: only ``capsule_id``; absent/vintage removes ``capsule_id`` and ``chain``).
_JCS_EXCLUDED_FIELDS = ("capsule_id",)


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

    if algorithm == "jcs-n":
        canonical = {key: value for key, value in capsule.items() if key not in CHAIN_LINKAGE_FIELDS}
        return jcs(normalize(canonical))
    if algorithm == "jcs":
        canonical = {key: value for key, value in capsule.items() if key not in _JCS_EXCLUDED_FIELDS}
        return jcs(canonical)
    raise UnsupportedCanonicalizationError(
        f"cannot compute capsule_id with canonicalization_id {algorithm!r}"
    )


def compute_capsule_id(capsule: dict[str, Any]) -> str:
    """Return the SHA-256 Capsule ID for the capsule's declared algorithm."""
    return hashlib.sha256(canonical_capsule_bytes(capsule)).hexdigest()
