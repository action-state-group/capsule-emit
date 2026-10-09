# SPDX-License-Identifier: Apache-2.0
"""Slot-composition conformance: capsule-emit's own committed slot-composition vector
(``test-vectors/slot-composition/``) — the carry-form and slot-form produce
byte-identical records, and each verifies independently under
``capsule_emit.verification``. Every case runs over both the released -04
corpus (``valid/``, frozen since v0.5.0) and its spec_version -05 twin
(``valid-v05/``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from capsule_emit.verification import verify_capsule as verify

VECTORS = Path(__file__).resolve().parents[1] / "test-vectors" / "slot-composition"
CASES = pytest.mark.parametrize("case", ["valid", "valid-v05"], ids=["spec-04", "spec-05"])


def _load(case):
    vector_dir = VECTORS / case
    carry_form = json.loads((vector_dir / "carry_form.json").read_text(encoding="utf-8"))
    slot_form = json.loads((vector_dir / "slot_form_composition.json").read_text(encoding="utf-8"))
    expected = json.loads((vector_dir / "expected.json").read_text(encoding="utf-8"))
    return carry_form, slot_form, expected


@CASES
def test_carry_form_and_slot_form_both_verify_independently(case):
    carry_form, slot_form, _expected = _load(case)
    assert verify(carry_form).ok
    assert verify(slot_form).ok


@CASES
def test_can_slot_member_is_byte_identical_to_the_standalone_carry_form(case):
    # The acceptance criterion: "the carry-form and slot-form produce byte-identical records" --
    # the can-slot member ref must digest-match the standalone received()
    # capsule's own capsule_id exactly (can() referenced it, never re-minted).
    carry_form, slot_form, expected = _load(case)
    members = slot_form["model_attestation"]["compute_attestation"]["composed_members"]
    can_ref = next(m for m in members if m["slot"] == "can")
    assert can_ref["digest"] == carry_form["capsule_id"]
    assert can_ref["digest"] == expected["carry_form_capsule_id"]


@CASES
def test_composed_members_carry_slot_annotations(case):
    _carry_form, slot_form, _expected = _load(case)
    members = slot_form["model_attestation"]["compute_attestation"]["composed_members"]
    assert {m["slot"] for m in members} == {"who", "can", "did"}
    assert all(set(m) == {"type", "digest_alg", "digest", "slot"} for m in members)
