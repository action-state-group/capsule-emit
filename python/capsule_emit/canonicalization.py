# SPDX-License-Identifier: Apache-2.0
"""Canonicalization-aware Capsule ID computation.

**draft-04 reversal (2026-08-24):** ``capsule_id`` is a
pure, signer-independent content address again. It excludes only
``capsule_id`` itself plus capsule-emit's own ledger-line bookkeeping fields
(``signature``, ``key_id`` — the COSE_Sign1 producer envelope and the raw
public key that produced it; see ``capsule_emit.signing``), which are never
members of the neutral Capsule object and are only ever added to the dict
*after* ``capsule_id`` is computed. Everything else — including ``chain`` and
``canonicalization_id`` — is committed into the preimage under the declared
``"jcs"`` algorithm, closing the pre-reversal gap where a chain link
(``parent_capsule_id``/``relation``) was unauthenticated. ``"jcs"`` is the only
algorithm: the withdrawn ``"jcs-n"`` construction is refused.
"""

from __future__ import annotations

import hashlib
from typing import Any

from agent_action_capsule.canonical import jcs

CANONICALIZATION_JCS = "jcs"

#: capsule-emit's own ledger-line bookkeeping — the COSE_Sign1 producer
#: envelope and its signing key_id. Never members of the neutral Capsule
#: object, so never part of any capsule_id preimage, under either algorithm.
_LOCAL_ONLY_FIELDS = ("signature", "key_id")


class UnsupportedCanonicalizationError(ValueError):
    """The declared algorithm cannot be recomputed from a capsule object."""


def canonical_capsule_bytes(record: dict[str, Any]) -> bytes:
    """Return the canonical Capsule ID preimage of ``record``.

    Plain JCS over every member but ``capsule_id`` and the ledger-line
    bookkeeping fields; ``chain`` IS committed, matching
    ``agent_action_capsule.canonical.compute_capsule_id`` and Rust's
    ``compute_capsule_id``. The record must declare ``canonicalization_id``
    ``jcs``: an absent, null or other value (``jcs-n`` included) raises
    :class:`UnsupportedCanonicalizationError`.
    """
    declared = record.get("canonicalization_id")
    if declared != CANONICALIZATION_JCS:
        raise UnsupportedCanonicalizationError(
            f"cannot compute capsule_id with canonicalization_id {declared!r}"
        )
    return jcs({
        key: value
        for key, value in record.items()
        if key != "capsule_id" and key not in _LOCAL_ONLY_FIELDS
    })


def compute_capsule_id(capsule: dict[str, Any]) -> str:
    """Return the SHA-256 Capsule ID of a record."""
    return hashlib.sha256(canonical_capsule_bytes(capsule)).hexdigest()
