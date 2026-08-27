# SPDX-License-Identifier: Apache-2.0
"""O8 conformance: capsule-emit's own committed slot-composition vector
(``test-vectors/slot-composition/``) — the carry-form and slot-form produce
byte-identical records, and each verifies independently under
``capsule_emit.verification``.
"""
from __future__ import annotations

import json
from pathlib import Path

from capsule_emit.verification import verify_capsule as verify

VECTOR_DIR = Path(__file__).resolve().parents[1] / "test-vectors" / "slot-composition" / "valid"


def _load():
    carry_form = json.loads((VECTOR_DIR / "carry_form.json").read_text(encoding="utf-8"))
    slot_form = json.loads((VECTOR_DIR / "slot_form_composition.json").read_text(encoding="utf-8"))
    expected = json.loads((VECTOR_DIR / "expected.json").read_text(encoding="utf-8"))
    return carry_form, slot_form, expected


def test_carry_form_and_slot_form_both_verify_independently():
    carry_form, slot_form, _expected = _load()
    assert verify(carry_form).ok
    assert verify(slot_form).ok


def test_can_slot_member_is_byte_identical_to_the_standalone_carry_form():
    # O8: "the carry-form and slot-form produce byte-identical records" --
    # the can-slot member ref must digest-match the standalone received()
    # capsule's own capsule_id exactly (can() referenced it, never re-minted).
    carry_form, slot_form, expected = _load()
    members = slot_form["model_attestation"]["compute_attestation"]["composed_members"]
    can_ref = next(m for m in members if m["slot"] == "can")
    assert can_ref["digest"] == carry_form["capsule_id"]
    assert can_ref["digest"] == expected["carry_form_capsule_id"]


def test_composed_members_carry_slot_annotations():
    _carry_form, slot_form, _expected = _load()
    members = slot_form["model_attestation"]["compute_attestation"]["composed_members"]
    assert {m["slot"] for m in members} == {"who", "can", "did"}
    assert all(set(m) == {"type", "digest_alg", "digest", "slot"} for m in members)
