# SPDX-License-Identifier: Apache-2.0
"""Shared Python/Go vectors for ``canonicalize_for_digest`` (capsule-emit#428).

``test-vectors/number-canonicalization/vectors.json`` is the contract both
this helper and Go's ``emit.CanonicalizeForDigest`` must meet: the same JSON
text gives the same AAC JSON-DIGEST, or an error in both.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_action_capsule.canonical import (
    FloatInDigestError,
    UnsafeIntegerError,
    json_digest,
)

from capsule_emit.numbers import canonicalize_for_digest

VECTORS = (
    Path(__file__).resolve().parents[2]
    / "test-vectors"
    / "number-canonicalization"
    / "vectors.json"
)
CASES = json.loads(VECTORS.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_vector(case):
    value = json.loads(case["input_json"])
    if case.get("error"):
        with pytest.raises((FloatInDigestError, UnsafeIntegerError)):
            json_digest(canonicalize_for_digest(value))
    else:
        assert json_digest(canonicalize_for_digest(value)) == case["digest"]
