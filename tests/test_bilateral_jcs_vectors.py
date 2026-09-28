# SPDX-License-Identifier: Apache-2.0
"""The bilateral signed payloads are RFC 8785 JCS, byte-for-byte.

``test-vectors/bilateral-payloads/vectors.json`` pins the bytes each payload
function must return. The expected bytes were produced by an independent
implementation (agent-action-capsule's Go ``canonical.JCS``), not by
capsule-emit, so a green run here is cross-implementation agreement rather
than a self-consistency check.

The non-ASCII cases are the ones ``json.dumps(sort_keys=True)`` got wrong:
it escapes every non-ASCII code point (``ensure_ascii``), where JCS emits the
UTF-8 bytes. ASCII-only payloads are identical under both, so previously
signed ASCII-only payloads still verify.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from capsule_emit import bilateral

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "test-vectors" / "bilateral-payloads" / "vectors.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.mark.parametrize("case", VECTORS, ids=[c["id"] for c in VECTORS])
def test_payload_bytes_match_the_independent_jcs_vector(case):
    payload = getattr(bilateral, case["fn"])(*case["args"])
    assert payload.hex() == case["jcs_hex"]
    assert hashlib.sha256(payload).hexdigest() == case["sha256"]


@pytest.mark.parametrize("case", VECTORS, ids=[c["id"] for c in VECTORS])
def test_payload_is_the_library_jcs_of_the_phase_object(case):
    from agent_action_capsule.canonical import jcs

    assert getattr(bilateral, case["fn"])(*case["args"]) == jcs(case["object"])


def test_non_ascii_payload_is_utf8_not_escaped():
    payload = bilateral.request_payload("Zürich Rück AG", "org-b", "0" * 64)
    assert "Zürich".encode() in payload
    assert b"\\u00fc" not in payload
