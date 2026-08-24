# SPDX-License-Identifier: Apache-2.0
"""Disclosure Envelope builder (draft-mih-scitt-agent-action-capsule-disclosure-envelope-00).

Wraps an already-sealed capsule alongside an out-of-band ``disclosures``
object, without touching the capsule's own bytes — the wire format a
verifier (e.g. the neutral web viewer) reads to recompute and check a
digest-only field's committed content, per the companion profile in
``agent-action-capsule``. Reuses the same JCS-SHA256 canonicalization
:func:`verify_input_digest` uses; this module defines no second hashing path.

This module is the single-capsule payload primitive. The ledger-level
``disclose`` CLI verb (O16 audit item 10, frozen dev-surface v4 §7b —
bundle + selected payload content + completeness statement + audience
suppression + its own sealed disclosure record, over an ``<id|range>``
selection) is ``capsule_emit.disclose``, which calls
:func:`build_disclosure_envelope` here once per selected record rather than
duplicating its digest-check logic.
"""
from __future__ import annotations

from typing import Any

from agent_action_capsule.canonical import FloatInDigestError, json_digest

# disclosures member name -> digest field within
# capsule["model_attestation"]["compute_attestation"]
DISCLOSURE_ELIGIBLE_FIELDS = {
    "agent_input": "agent_input_digest",
    "agent_output": "agent_output_digest",
}


class DisclosureError(ValueError):
    """Raised when a requested disclosure can't be built against this capsule."""


def build_disclosure_envelope(
    capsule: dict,
    *,
    agent_input: Any = None,
    agent_output: Any = None,
    strict: bool = True,
) -> dict:
    """Build a Disclosure Envelope: ``{"capsule": capsule, "disclosures": {...}}``.

    ``capsule`` is included byte-for-byte unmodified — ``capsule_id`` was
    already computed over it and is never recomputed here. Pass the raw
    value for each artifact to disclose; an artifact left as ``None`` stays
    WITHHELD (absent from ``disclosures``), the default posture.

    When ``strict`` (the default), each provided value is checked against
    the digest already committed in ``capsule.model_attestation
    .compute_attestation.<field>_digest`` — using the same JCS-SHA256
    primitive :func:`capsule_emit.verify.verify_input_digest` uses — and a
    :class:`DisclosureError` is raised on a mismatch or a missing committed
    digest, so a producer catches a copy-paste error at build time rather
    than shipping a link that renders REVEALED · MISMATCH. Pass
    ``strict=False`` to build an envelope without this check (for example,
    to deliberately construct a mismatching fixture for testing).
    """
    disclosures: dict[str, Any] = {}
    for member, value in (("agent_input", agent_input), ("agent_output", agent_output)):
        if value is None:
            continue
        disclosures[member] = value

    if strict:
        compute_attestation = (
            capsule.get("model_attestation", {}).get("compute_attestation", {})
            if isinstance(capsule, dict)
            else {}
        )
        for member, value in disclosures.items():
            digest_field = DISCLOSURE_ELIGIBLE_FIELDS[member]
            stored = compute_attestation.get(digest_field)
            if stored is None:
                raise DisclosureError(
                    f"capsule has no {digest_field} committed — cannot disclose {member!r}"
                )
            try:
                computed = json_digest(value)
            except (FloatInDigestError, TypeError, ValueError) as exc:
                raise DisclosureError(f"{member} value cannot be canonicalized: {exc}") from exc
            if computed != stored:
                raise DisclosureError(
                    f"{member} does not match the committed digest "
                    f"(computed {computed}, capsule has {stored}) — "
                    "pass strict=False to build the envelope anyway"
                )

    return {"capsule": capsule, "disclosures": disclosures}
