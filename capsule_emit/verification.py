# SPDX-License-Identifier: Apache-2.0
"""Canonicalization-aware adapters for the neutral Capsule verifier.

``agent_action_capsule`` remains the source of truth for Class-1 structure,
chain, assurance, and registry checks. Its current Capsule ID check implements
the vintage ``jcs-n`` construction only, so this module replaces that single
verdict with recomputation selected by the record's declared algorithm.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from agent_action_capsule import Finding, VerificationResult
from agent_action_capsule import verify as _verify
from agent_action_capsule import verify_store as _verify_store
from agent_action_capsule.canonical import FloatInDigestError, UnsafeIntegerError

from .canonicalization import compute_capsule_id

_CAPSULE_ID_FINDINGS = frozenset({"capsule_id_mismatch", "capsule_id_uncomputable"})


def _apply_declared_capsule_id_check(capsule: Any, result: VerificationResult) -> None:
    """Replace the upstream fixed-``jcs-n`` identity verdict in ``result``."""
    result.findings[:] = [finding for finding in result.findings if finding.code not in _CAPSULE_ID_FINDINGS]
    result.capsule_id = None

    if isinstance(capsule, Mapping) and capsule.get("capsule_id") is not None:
        carried = capsule.get("capsule_id")
        try:
            recomputed = compute_capsule_id(dict(capsule))
        except (FloatInDigestError, UnsafeIntegerError):
            # The upstream structural check already reports these digest hazards.
            pass
        except Exception as exc:  # The public verifier is total and never raises.
            result.findings.append(Finding("capsule_id_uncomputable", repr(exc), check=2))
        else:
            result.capsule_id = recomputed
            if recomputed != carried:
                result.findings.append(
                    Finding(
                        "capsule_id_mismatch",
                        f"recomputed {recomputed} != carried {carried}",
                        check=2,
                    )
                )

    result.ok = not any(finding.severity == "error" for finding in result.findings)


def verify_capsule(
    capsule: Any,
    *,
    store: Iterable[Any] | None = None,
    registries: Mapping[str, frozenset] | None = None,
) -> VerificationResult:
    """Run neutral Class-1 verification with declared Capsule ID dispatch."""
    result = _verify(capsule, store=store, registries=registries)
    _apply_declared_capsule_id_check(capsule, result)
    return result


def verify_store(
    capsules: list[Any],
    *,
    registries: Mapping[str, frozenset] | None = None,
) -> list[VerificationResult]:
    """Run store-level verification with declared Capsule ID dispatch."""
    results = _verify_store(capsules, registries=registries)
    for capsule, result in zip(capsules, results):
        _apply_declared_capsule_id_check(capsule, result)
    return results
