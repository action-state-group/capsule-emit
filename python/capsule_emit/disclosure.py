# SPDX-License-Identifier: Apache-2.0
"""Disclosure Envelope builder (draft-mih-scitt-agent-action-capsule-disclosure-envelope-00).

Wraps an already-sealed capsule alongside an out-of-band ``disclosures``
object, without touching the capsule's own bytes — the wire format a
verifier (e.g. the neutral web viewer) reads to recompute and check a
digest-only field's committed content, per the companion profile in
``agent-action-capsule``. The digest check is
agent-action-capsule's (``agent_action_capsule.disclosure_envelope``); this
module keeps capsule-emit's keyword signature and error type.

This module is the single-capsule payload primitive. The ledger-level
``disclose`` CLI verb (
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

    ``capsule`` is included byte-for-byte unmodified. Pass the raw value for
    each artifact to disclose; one left as ``None`` stays WITHHELD.

    With ``strict`` (the default) the envelope is built by
    agent-action-capsule's ``disclosure_envelope.build_disclosure_envelope``,
    which checks each value against the digest committed in
    ``model_attestation.compute_attestation``; a mismatch or a missing
    committed digest raises :class:`DisclosureError`. Pass ``strict=False``
    to build an envelope without the check (for example, a deliberately
    mismatching test fixture).
    """
    from agent_action_capsule.disclosure_envelope import (
        NO_COMMITTED_DIGEST,
    )
    from agent_action_capsule.disclosure_envelope import (
        build_disclosure_envelope as _build,
    )

    disclosures = {
        member: value
        for member, value in (("agent_input", agent_input), ("agent_output", agent_output))
        if value is not None
    }
    if not strict:
        return {"capsule": capsule, "disclosures": disclosures}
    try:
        return _build(capsule, disclosures)
    except ValueError as exc:
        code, _, member = str(exc).partition(": ")
        digest_field = DISCLOSURE_ELIGIBLE_FIELDS.get(member, member)
        if code == NO_COMMITTED_DIGEST:
            raise DisclosureError(
                f"capsule has no {digest_field} committed — cannot disclose {member!r}"
            ) from exc
        try:
            computed = json_digest(disclosures[member])
        except (FloatInDigestError, TypeError, ValueError) as err:
            raise DisclosureError(f"{member} value cannot be canonicalized: {err}") from exc
        stored = (capsule.get("model_attestation") or {}).get("compute_attestation", {}).get(digest_field)
        raise DisclosureError(
            f"{member} does not match the committed digest "
            f"(computed {computed}, capsule has {stored}) — "
            "pass strict=False to build the envelope anyway"
        ) from exc
