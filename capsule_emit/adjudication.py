# SPDX-License-Identifier: Apache-2.0
"""Adjudication record pattern — seal a twin-comparison verdict capsule.

One new ``chain.relation`` value, ``"adjudicates"``, and one public helper:

``seal_adjudication(half_a_capsule_id, half_b_capsule_id, verdict, ...)``
    Seals a capsule carrying:

    - ``chain.parent_capsule_id`` — *half_a_capsule_id*
    - ``chain.relation`` — ``"adjudicates"``
    - ``disposition.verdict_class`` — ``"assessed"`` (a detection
      disposition, never ``"executed"``/``"confirmed"`` — same discipline as
      the ``"assesses"`` relation; see ``capsule_emit.core``'s ``relation``
      parameter docs)
    - ``compute_attestation.adjudication`` — ``source: "twin_comparison"``,
      ``capture_method: "deterministic_replay"``, the *verdict*, and the
      properties that back it (``divergence_index``, ``margin``,
      ``margin_tau``, ``twin_owner_distinct``, ``weights_digest``, and both
      halves' capsule ids)

This module mints the record; it does not compute the verdict. Finding the
first divergent token between two transcripts and deciding
corroborated/inconclusive/contradicted from it is a caller concern (see
``capsule-emit-mesh``'s ``twin_adjudicator.compare_transcripts`` /
``adjudicate``, which reuses ``replay_spot_check.SpotCheckResult``'s digest
domain). No coordinator, no scorer, no new record type — this is the same
``chain``/``compute_attestation`` shape every other capsule already has,
with one new registry-governed (open, never rejecting — see
``agent_action_capsule.registries``) relation value.

**No verdict is not an error.** A twin comparison that can't be adjudicated
(mismatched ``weights_digest``, or a same-owner "twin") has *nothing to
adjudicate* — the caller simply never calls :func:`seal_adjudication` for
that case; there is no "refused" verdict value to construct. See
``VERDICT_CORROBORATED``/``VERDICT_INCONCLUSIVE``/:func:`contradicted` for
the only three shapes an actual verdict takes.

"Disagreement is a trigger, not a verdict; inconclusive is first-class."
"""
from __future__ import annotations

import os
from typing import Any

from .core import EmitResult, _emit_capsule
from .numbers import float_to_str

__all__ = [
    "RELATION_ADJUDICATES",
    "SOURCE_TWIN_COMPARISON",
    "CAPTURE_METHOD_DETERMINISTIC_REPLAY",
    "VERDICT_CORROBORATED",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_CONTRADICTED_PREFIX",
    "contradicted",
    "seal_adjudication",
]

#: The new chain.relation value this module adds. Registry-governed but
#: open (agent_action_capsule.registries: an unregistered chain.relation is
#: informational, never a rejection) — no spec change required to use it.
RELATION_ADJUDICATES = "adjudicates"

#: Honesty labels carried on every adjudication capsule (module docstring).
SOURCE_TWIN_COMPARISON = "twin_comparison"
CAPTURE_METHOD_DETERMINISTIC_REPLAY = "deterministic_replay"

#: Schema tag for the adjudication artifact (co-carried like other
#: capsule-emit-mesh honesty-labelled blocks, e.g. output_cross_check's
#: CROSS_CHECK_SCHEMA).
ADJUDICATION_SCHEMA = "capsule-emit/adjudication/v1"

#: The closed set of verdict SHAPES (not values — "contradicted" takes an
#: owner). Deliberately mirrors the referee doc's
#: ``corroborated | contradicted:<owner_id> | inconclusive``. Never a score.
VERDICT_CORROBORATED = "corroborated"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_CONTRADICTED_PREFIX = "contradicted:"


def contradicted(owner_id: str) -> str:
    """Build a ``"contradicted:<owner_id>"`` verdict string.

    *owner_id* names which twin's half diverged from the referee tiebreak —
    a detection label, never an accusation the capsule's own signature
    would need to prove more than "this is what the referee's forward pass
    returned." Raises if *owner_id* is empty: a contradiction with no named
    owner is not a valid verdict shape.
    """
    if not owner_id:
        raise ValueError("contradicted(owner_id) requires a non-empty owner_id")
    return f"{VERDICT_CONTRADICTED_PREFIX}{owner_id}"


def _validate_verdict(verdict: str) -> None:
    if verdict in (VERDICT_CORROBORATED, VERDICT_INCONCLUSIVE):
        return
    if verdict.startswith(VERDICT_CONTRADICTED_PREFIX) and len(verdict) > len(VERDICT_CONTRADICTED_PREFIX):
        return
    raise ValueError(
        f"verdict={verdict!r} is not one of "
        f"{VERDICT_CORROBORATED!r}, {VERDICT_INCONCLUSIVE!r}, or "
        f"{VERDICT_CONTRADICTED_PREFIX}<owner_id> (see contradicted())"
    )


def seal_adjudication(
    half_a_capsule_id: str,
    half_b_capsule_id: str,
    verdict: str,
    *,
    margin: float,
    margin_tau: float,
    ledger: str | os.PathLike,
    divergence_index: int | None = None,
    twin_owner_distinct: bool | None = None,
    weights_digest: str | None = None,
    anchor: bool = False,
    action: str = "adjudicate",
    operator: str = "",
    developer: str = "",
) -> EmitResult:
    """Seal an adjudication capsule over two already-sealed twin halves.

    Args:
        half_a_capsule_id: ``capsule_id`` of the first compared half. Becomes
            ``chain.parent_capsule_id`` (``_emit_capsule`` requires exactly
            one chain target; the second half is cited inside
            ``compute_attestation.adjudication.half_b_capsule_id`` instead
            of a second chain link — no new record/chain shape).
        half_b_capsule_id: ``capsule_id`` of the second compared half.
        verdict: ``VERDICT_CORROBORATED``, ``VERDICT_INCONCLUSIVE``, or
            ``contradicted(owner_id)``. Raises :class:`ValueError` for any
            other shape — there is no silent fourth verdict.
        margin: The computed agreement margin (e.g. the fraction of the
            compared transcript that matched before any divergence; ``1.0``
            for a byte/token-identical pair). Never a confidence score —
            purely the number the verdict rule compared against *margin_tau*.
            Stored as an exact decimal string (RFC 8785 §3.2.2.3 via
            :func:`capsule_emit.numbers.float_to_str`) — a raw JSON float in
            a digest-bearing field raises ``FloatInDigestError`` (§5.1).
        margin_tau: The threshold *margin* was compared against, published
            here so a stranger can recompute the same verdict from the same
            numbers (see the referee build spec's "τ ... a published
            constant" requirement). Also stored as an exact decimal string.
        ledger: Path to the JSONL ledger file.
        divergence_index: First token index at which the two halves diverged
            (``None`` when the transcripts fully matched).
        twin_owner_distinct: Whether the two halves' owners were confirmed
            distinct. Carried as a property; a caller checks this BEFORE
            deciding whether to call this function at all (a same-owner
            twin has no verdict to seal — see the module docstring).
        weights_digest: The shared ``weights_digest`` the two halves claimed
            (E5; may be ``None`` — stubbed until E5 lands. Present here only
            when both halves already agreed on it — a caller with
            mismatched halves has nothing to adjudicate and never reaches
            this function).
        anchor: Passed through to ``_emit_capsule`` (default ``False``).
        action: Stable action name (default ``"adjudicate"``).
        operator: Tenant / org identifier.
        developer: Agent name + version (the adjudicating party).

    Returns:
        :class:`~capsule_emit.core.EmitResult` with the sealed capsule.

    Raises:
        ValueError: *verdict* is not one of the three valid shapes.
    """
    _validate_verdict(verdict)

    extra_compute: dict[str, Any] = {
        "adjudication": {
            "schema": ADJUDICATION_SCHEMA,
            "source": SOURCE_TWIN_COMPARISON,
            "capture_method": CAPTURE_METHOD_DETERMINISTIC_REPLAY,
            "verdict": verdict,
            "divergence_index": divergence_index,
            # §5.1: a JSON float in a digest-bearing field raises
            # FloatInDigestError -- margin/margin_tau travel as exact
            # decimal strings (RFC 8785 §3.2.2.3), same rule
            # output_cross_check's compute_meter fields already follow.
            "margin": float_to_str(margin, field="adjudication.margin"),
            "margin_tau": float_to_str(margin_tau, field="adjudication.margin_tau"),
            "twin_owner_distinct": twin_owner_distinct,
            "weights_digest": weights_digest,
            "half_a_capsule_id": half_a_capsule_id,
            "half_b_capsule_id": half_b_capsule_id,
        }
    }

    return _emit_capsule(
        action=action,
        operator=operator,
        developer=developer,
        verdict="assessed",
        confirms=half_a_capsule_id,
        relation=RELATION_ADJUDICATES,
        extra_compute=extra_compute,
        ledger=ledger,
        anchor=anchor,
    )
