# SPDX-License-Identifier: Apache-2.0
"""[capsule-cose-sign1] conformance: capsule-emit's own committed producer-
envelope vector (``test-vectors/producer-envelope/``) verifies under the
Python reference verifier (``agent_action_capsule.producer_envelope``) and
round-trips through capsule-emit's own signing/verification wrapper.

The corpus was additionally cross-verified, manually, against the Go
reference verifier (``agent-action-capsule``'s ``go/envelope`` package) —
see ``test-vectors/producer-envelope/README.md`` and
``scripts/verify_with_go.go`` — before being committed; that check is not
repeated here (capsule-emit has no other Go dependency / CI toolchain), so
this test is the permanent, Python-only regression guard that the checked-in
bytes keep verifying.

Every case runs over both the released -04 corpus (``valid/``, frozen since
v0.5.0) and its spec_version -05 twin (``valid-v05/``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

VECTORS = Path(__file__).resolve().parents[1] / "test-vectors" / "producer-envelope"
CASES = pytest.mark.parametrize("case", ["valid", "valid-v05"], ids=["spec-04", "spec-05"])


def _load(case):
    vector_dir = VECTORS / case
    capsule_id = (vector_dir / "capsule_id.txt").read_text(encoding="ascii").strip()
    envelope = (vector_dir / "envelope.cose").read_bytes()
    expected = json.loads((vector_dir / "expected.json").read_text(encoding="utf-8"))
    capsule = json.loads((vector_dir / "capsule.json").read_text(encoding="utf-8"))
    return capsule_id, envelope, expected, capsule


@CASES
def test_vector_verifies_under_the_neutral_python_reference_verifier(case):
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    capsule_id, envelope, expected, _capsule = _load(case)
    result = verify_producer_envelope(capsule_id, envelope)
    assert result.ok is expected["ok"]
    assert result.public_key.hex() == expected["public_key_hex"]
    assert [f.code for f in result.findings] == expected["finding_codes"]


@CASES
def test_vector_round_trips_through_capsule_emit_own_verifier(case):
    from capsule_emit.signing import verify_capsule_signature

    capsule_id, envelope, _expected, capsule = _load(case)
    record = dict(capsule, signature=envelope.hex(), key_id=capsule["key_id"])
    assert record["capsule_id"] == capsule_id
    assert verify_capsule_signature(record)


@CASES
def test_vector_tampered_payload_fails(case):
    """Same envelope, presented for a different capsule_id -- must fail
    (payload-mismatch), matching agent-action-capsule's own negative case
    of the same name in ``producer-envelope-vectors/``."""
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    _capsule_id, envelope, _expected, _capsule = _load(case)
    wrong_id = "ff" * 32
    result = verify_producer_envelope(wrong_id, envelope)
    assert not result.ok
    assert "envelope_payload_mismatch" in [f.code for f in result.findings]
