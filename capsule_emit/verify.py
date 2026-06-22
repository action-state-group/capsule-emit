# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any


def verify_input_digest(capsule: dict, candidate_input: Any) -> bool:
    """Return True if candidate_input matches the agent_input_digest sealed in capsule.

    Extracts the stored digest from capsule["model_attestation"]["compute_attestation"]
    ["agent_input_digest"] and compares it to the JCS-SHA256 digest of candidate_input.
    Returns False if the digest field is absent (capsule was emitted without agent_input).
    """
    from agent_action_capsule.canonical import json_digest

    stored = (
        capsule.get("model_attestation", {})
               .get("compute_attestation", {})
               .get("agent_input_digest")
    )
    if stored is None:
        return False
    return stored == json_digest(candidate_input)
