# SPDX-License-Identifier: Apache-2.0
"""The one-word outcome label the ledger views show for a capsule.

``disposition.verdict_class`` when the capsule carries one. Adjudications and
log entries carry none (the registry has no value for either), and their
``disposition.decision`` is always ``"accept"``, which would misstate them, so
they are labelled by record type instead:

- an adjudication shows the verdict it records
  (``compute_attestation.adjudication.verdict``: ``corroborated``,
  ``inconclusive`` or ``contradicted:<owner_id>``), or ``"adjudication"``
  if that is missing;
- a log entry (``compute_attestation.log_digest``, see ``capsule_emit.log``)
  shows ``"logged"``. That is a display label only; it is never written to a
  capsule.

Any other capsule without a verdict_class falls back to its decision.
"""
from __future__ import annotations

from typing import Any

from .adjudication import is_adjudication

__all__ = ["verdict_label"]

ADJUDICATION_LABEL = "adjudication"
LOG_ENTRY_LABEL = "logged"


def _compute(capsule: dict[str, Any]) -> dict[str, Any]:
    compute = (capsule.get("model_attestation") or {}).get("compute_attestation")
    return compute if isinstance(compute, dict) else {}


def verdict_label(capsule: dict[str, Any], default: str = "") -> str:
    disposition = capsule.get("disposition") or {}
    if disposition.get("verdict_class"):
        return disposition["verdict_class"]
    compute = _compute(capsule)
    if is_adjudication(capsule):
        verdict = (compute.get("adjudication") or {}).get("verdict")
        return verdict if isinstance(verdict, str) and verdict else ADJUDICATION_LABEL
    if "log_digest" in compute:
        return LOG_ENTRY_LABEL
    return disposition.get("decision") or default
