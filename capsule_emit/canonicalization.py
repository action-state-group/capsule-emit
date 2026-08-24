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

_VINTAGE_ALGORITHM = "jcs-n"


class UnsupportedCanonicalizationError(ValueError):
    """The declared algorithm cannot be recomputed from a capsule object."""


def canonical_capsule_bytes(capsule: dict[str, Any]) -> bytes:
    """Return the canonical Capsule ID preimage selected by the capsule.

    Records without a ``canonicalization_id`` predate the binding slot and
    retain the legacy ``jcs-n`` interpretation. ``as-transmitted`` is not
    available here because parsing a wire record discards its original bytes.
    """
    canonical = {key: value for key, value in capsule.items() if key not in CHAIN_LINKAGE_FIELDS}
    algorithm = capsule.get("canonicalization_id", _VINTAGE_ALGORITHM)

    if algorithm == "jcs-n":
        return jcs(normalize(canonical))
    if algorithm == "jcs":
        return jcs(canonical)
    raise UnsupportedCanonicalizationError(
        f"cannot compute capsule_id with canonicalization_id {algorithm!r}"
    )


def compute_capsule_id(capsule: dict[str, Any]) -> str:
    """Return the SHA-256 Capsule ID for the capsule's declared algorithm."""
    return hashlib.sha256(canonical_capsule_bytes(capsule)).hexdigest()
