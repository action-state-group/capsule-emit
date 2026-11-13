# SPDX-License-Identifier: Apache-2.0
"""Canonicalization-id verifier for Agent Action Capsule records.

The ``verify_canonicalization_id`` function is the §5-normative check for the
self-describing binding slot (``canonicalization_id`` at the top level of the
capsule JSON body, committed to ``capsule_id`` in the signed payload).

**Verdict semantics:**

``VERIFIED``
    The declared algorithm is registered and matches the profile, the
    capsule_id recomputation passes, and the record is consistent.

``DIGEST_MISMATCH``
    The ``capsule_id`` does not recompute correctly.  This covers both (a) a
    tampered record where a known ``canonicalization_id`` was altered to another
    registered value without recomputing ``capsule_id`` and (b) a known algorithm
    that does not match the declared profile algorithm (profile cross-check
    failure).

``UNKNOWN_ID``
    The ``canonicalization_id`` field is present but names an algorithm not in
    the CPB registry.  The verifier fails closed; the record is unverifiable.

``NON_CONFORMING``
    Structural error, an absent or null ``canonicalization_id`` (a pre-format-4
    record, refused), an algorithm such as ``as-transmitted`` that cannot be
    recomputed from parsed JSON, or an unexpected verifier failure. The record
    cannot be evaluated.

**Fail-closed principle:**

An absent algorithm registry match is never treated as implicit approval.
An unknown algorithm, a mismatched profile, or a failing ``capsule_id``
always returns a non-``ok`` result.  Do not relax without extending the
registry.

**HYBRID verifier (U2/B3):**

This module covers the canonicalization binding slot only.  The full HYBRID
second-pass verifier (input/output digest re-verification against a candidate)
lives in ``capsule_emit.verify`` and is a separate task (U2/B3).  Do not fold
the two.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .canonicalization import UnsupportedCanonicalizationError, compute_capsule_id
from .numbers import CANONICALIZATION_ID

#: CPB Payload Canonicalization Algorithm Registry (normative entries).
#: Update here when the registry gains a new entry.
KNOWN_ALGORITHMS: frozenset[str] = frozenset({"jcs", "as-transmitted"})


class CanonicalizationVerdict(str, Enum):
    """Verdict reason returned by :func:`verify_canonicalization_id`."""

    VERIFIED = "verified"
    """Declared algorithm is registered, matches the profile, and capsule_id
    recomputes correctly.
    """

    DIGEST_MISMATCH = "digest_mismatch"
    """``capsule_id`` does not recompute correctly (tampered binding), or the
    declared algorithm does not match the profile algorithm (profile cross-check
    failure).
    """

    UNKNOWN_ID = "unknown_id"
    """``canonicalization_id`` is present but names an algorithm not in the
    CPB registry.  Verifier fails closed — the record is unverifiable.
    """

    NON_CONFORMING = "non_conforming"
    """Structural error (missing ``capsule_id`` field, absent or null
    ``canonicalization_id``, or unexpected exception).
    The record cannot be evaluated.
    """


@dataclass
class CanonicalizationResult:
    """Structured result from :func:`verify_canonicalization_id`.

    Truthy when ``ok`` is True; falsy otherwise.  Use ``if result:`` or
    ``result.ok`` — do NOT use ``is True`` / ``is False`` identity checks.

    Attributes:
        ok:       True only when the verdict is VERIFIED.
        verdict:  Reason code from :class:`CanonicalizationVerdict`.
        declared: The raw ``canonicalization_id`` field value from the capsule,
                  or ``None`` when the field was absent.
        resolved: The algorithm that was evaluated (the declared value).
    """

    ok: bool
    verdict: CanonicalizationVerdict
    declared: str | None = None
    resolved: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def verify_canonicalization_id(
    capsule: dict[str, Any],
    *,
    profile_algorithm: str = CANONICALIZATION_ID,
) -> CanonicalizationResult:
    """Return a structured verdict for the ``canonicalization_id`` binding slot.

    **Never raises.**  Per the profile's structured-result contract, this
    function catches all exceptions and returns NON_CONFORMING instead.

    **Algorithm** (in order):

    1. Extract ``capsule_id`` from *capsule*.  If absent → NON_CONFORMING.
    2. Resolve and validate ``canonicalization_id`` from *capsule*. An unknown
       identifier returns UNKNOWN_ID.
    3. Recompute ``capsule_id`` using the resolved algorithm. If the
       recomputed value does not match → DIGEST_MISMATCH.
    4. Profile match:

       a. **Absent or null** → NON_CONFORMING (step 3 cannot recompute it).
       b. **Present, known, mismatches** *profile_algorithm* → DIGEST_MISMATCH.
       c. **Present, known, matches** *profile_algorithm* → VERIFIED.

    Args:
        capsule:           The capsule dict as produced by :func:`capsule_emit.emit`.
        profile_algorithm: The registered algorithm identifier that this
                           capsule profile declares.  Default is
                           :data:`capsule_emit.numbers.CANONICALIZATION_ID`
                           (``"jcs"``) — a format-4 record REQUIRES ``"jcs"``
                           (§5.1). Pass another value only for deliberate
                           negative/mutant testing.

    Returns:
        :class:`CanonicalizationResult` with ``.ok``, ``.verdict``,
        ``.declared``, and ``.resolved``.
    """
    try:
        stored_cid = capsule.get("capsule_id")
        if stored_cid is None:
            return CanonicalizationResult(
                ok=False,
                verdict=CanonicalizationVerdict.NON_CONFORMING,
            )

        declared: str | None = capsule.get("canonicalization_id")
        resolved = declared
        # Present but not in the registry.
        if declared is not None and declared not in KNOWN_ALGORITHMS:
            return CanonicalizationResult(
                ok=False,
                verdict=CanonicalizationVerdict.UNKNOWN_ID,
                declared=declared,
                resolved=None,
            )

        try:
            recomputed = compute_capsule_id(dict(capsule))
        except UnsupportedCanonicalizationError:
            return CanonicalizationResult(
                ok=False,
                verdict=CanonicalizationVerdict.NON_CONFORMING,
                declared=declared,
                resolved=resolved,
            )
        if recomputed != stored_cid:
            return CanonicalizationResult(
                ok=False,
                verdict=CanonicalizationVerdict.DIGEST_MISMATCH,
                declared=declared,
                resolved=resolved,
            )

        # Present, registered, but wrong profile.
        if declared != profile_algorithm:
            return CanonicalizationResult(
                ok=False,
                verdict=CanonicalizationVerdict.DIGEST_MISMATCH,
                declared=declared,
                resolved=declared,
            )

        # Present, registered, matches profile.
        return CanonicalizationResult(
            ok=True,
            verdict=CanonicalizationVerdict.VERIFIED,
            declared=declared,
            resolved=declared,
        )

    except Exception:  # noqa: BLE001
        return CanonicalizationResult(
            ok=False,
            verdict=CanonicalizationVerdict.NON_CONFORMING,
        )
