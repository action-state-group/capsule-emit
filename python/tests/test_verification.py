# SPDX-License-Identifier: Apache-2.0
"""``capsule_emit.verification`` is agent-action-capsule's verifier, not a
second one: the same functions, so the same verdict for every capsule."""

from __future__ import annotations

import agent_action_capsule

from capsule_emit import seal
from capsule_emit.canonicalization import compute_capsule_id
from capsule_emit.verification import verify_capsule, verify_store


def _sealed(tmp_path):
    return seal(
        None,
        action="test_action",
        anchor=False,
        witness=False,
        ledger=tmp_path / "ledger",
        canonicalization_id="jcs",
    ).capsule


def test_the_functions_are_agent_action_capsules():
    assert verify_capsule is agent_action_capsule.verify
    assert verify_store is agent_action_capsule.verify_store


def test_a_sealed_capsule_verifies(tmp_path):
    capsule = _sealed(tmp_path)
    assert verify_capsule(capsule).ok
    assert verify_store([capsule])[0].ok


def test_a_vintage_jcs_n_capsule_fails_even_with_a_matching_id(tmp_path):
    # No canonicalization_id: the pre-slot jcs-n construction, whose id this
    # package can still compute. AAC defines format 4 with "jcs" only.
    vintage = dict(_sealed(tmp_path))
    del vintage["canonicalization_id"]
    vintage["capsule_id"] = compute_capsule_id(vintage)

    result = verify_capsule(vintage)
    assert not result.ok
    assert [f.code for f in result.errors] == ["canonicalization_id_missing"]


def test_an_undeclared_algorithm_fails(tmp_path):
    capsule = dict(_sealed(tmp_path), canonicalization_id="jcs-n")
    result = verify_capsule(capsule)
    assert not result.ok
    assert "canonicalization_profile_mismatch" in [f.code for f in result.errors]


def test_a_tampered_capsule_fails_check_2(tmp_path):
    capsule = dict(_sealed(tmp_path), operator="someone-else")
    result = verify_capsule(capsule)
    assert not result.ok
    assert [f.code for f in result.errors] == ["capsule_id_mismatch"]
