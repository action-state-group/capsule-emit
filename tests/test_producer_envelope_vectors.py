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
"""
from __future__ import annotations

import json
from pathlib import Path

VECTOR_DIR = Path(__file__).resolve().parents[1] / "test-vectors" / "producer-envelope" / "valid"


def _load():
    capsule_id = (VECTOR_DIR / "capsule_id.txt").read_text(encoding="ascii").strip()
    envelope = (VECTOR_DIR / "envelope.cose").read_bytes()
    expected = json.loads((VECTOR_DIR / "expected.json").read_text(encoding="utf-8"))
    capsule = json.loads((VECTOR_DIR / "capsule.json").read_text(encoding="utf-8"))
    return capsule_id, envelope, expected, capsule


def test_vector_verifies_under_the_neutral_python_reference_verifier():
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    capsule_id, envelope, expected, _capsule = _load()
    result = verify_producer_envelope(capsule_id, envelope)
    assert result.ok is expected["ok"]
    assert result.public_key.hex() == expected["public_key_hex"]
    assert [f.code for f in result.findings] == expected["finding_codes"]


def test_vector_round_trips_through_capsule_emit_own_verifier():
    from capsule_emit.signing import verify_capsule_signature

    capsule_id, envelope, _expected, capsule = _load()
    record = dict(capsule, signature=envelope.hex(), key_id=capsule["key_id"])
    assert record["capsule_id"] == capsule_id
    assert verify_capsule_signature(record)


def test_vector_tampered_payload_fails():
    """Same envelope, presented for a different capsule_id -- must fail
    (payload-mismatch), matching agent-action-capsule's own negative case
    of the same name in ``producer-envelope-vectors/``."""
    from agent_action_capsule.producer_envelope import verify_producer_envelope

    _capsule_id, envelope, _expected, _capsule = _load()
    wrong_id = "ff" * 32
    result = verify_producer_envelope(wrong_id, envelope)
    assert not result.ok
    assert "envelope_payload_mismatch" in [f.code for f in result.findings]
