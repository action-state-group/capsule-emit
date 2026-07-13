# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any


def verify_input_digest(capsule: dict, candidate_input: Any) -> bool:
    """Return True if candidate_input matches the agent_input_digest sealed in capsule.

    Extracts the stored digest from capsule["model_attestation"]["compute_attestation"]
    ["agent_input_digest"] and compares it to the JCS-SHA256 digest of candidate_input.
    Returns False if the digest field is absent (capsule was emitted without agent_input).

    **Never raises.** Per the profile's structured-result contract ("a verifier MUST
    return a structured result, never throw"), a candidate that cannot be
    JCS-canonicalized — e.g. one carrying a raw float, which §5.1 forbids in a
    digest-bearing field — does not match the sealed digest and returns ``False``
    rather than propagating ``FloatInDigestError``. This closes the crash/DoS surface
    where a single float-bearing receipt could abort a caller's scoring loop.
    """
    from agent_action_capsule.canonical import FloatInDigestError, json_digest

    stored = (
        capsule.get("model_attestation", {})
               .get("compute_attestation", {})
               .get("agent_input_digest")
    )
    if stored is None:
        return False
    try:
        return stored == json_digest(candidate_input)
    except (FloatInDigestError, TypeError, ValueError):
        return False
