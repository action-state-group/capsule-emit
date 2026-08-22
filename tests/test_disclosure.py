# SPDX-License-Identifier: Apache-2.0
"""Disclosure Envelope builder — reuses the same JCS-SHA256 canonicalization
verify_input_digest uses; no second hashing path."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import capsule_emit
from capsule_emit import DisclosureError, build_disclosure_envelope
from capsule_emit.verify import verify_input_digest


def _emit_sealed(agent_input=None, agent_output=None) -> dict:
    with tempfile.TemporaryDirectory() as d:
        ledger = str(Path(d) / "ledger.jsonl")
        capsule_emit.seal(
            agent_input,
            action="purchase",
            operator="did:key:zOperator",
            developer="agent@v1",
            agent_output=agent_output,
            anchor=False,
            ledger=ledger,
        )
        return capsule_emit.read_ledger(ledger)[0]


def test_build_envelope_with_matching_disclosure():
    payload = {"vendor": "Frobozz Supply", "total": "1240.19"}
    capsule = _emit_sealed(agent_input=payload)

    envelope = build_disclosure_envelope(capsule, agent_input=payload)

    assert envelope["capsule"] == capsule  # anchored bytes untouched
    assert envelope["disclosures"] == {"agent_input": payload}


def test_capsule_is_byte_identical_not_copied_or_mutated():
    payload = {"a": 1}
    capsule = _emit_sealed(agent_input=payload)
    original = dict(capsule)

    build_disclosure_envelope(capsule, agent_input=payload)

    assert capsule == original  # builder never mutates its input


def test_both_artifacts_disclosable_independently():
    agent_input = {"a": 1}
    agent_output = {"b": 2}
    capsule = _emit_sealed(agent_input=agent_input, agent_output=agent_output)

    envelope = build_disclosure_envelope(capsule, agent_output=agent_output)

    assert envelope["disclosures"] == {"agent_output": agent_output}  # agent_input stays WITHHELD


def test_no_disclosures_by_default():
    capsule = _emit_sealed(agent_input={"a": 1})
    envelope = build_disclosure_envelope(capsule)
    assert envelope["disclosures"] == {}


def test_strict_mode_rejects_mismatched_value():
    capsule = _emit_sealed(agent_input={"a": 1})

    with pytest.raises(DisclosureError, match="does not match"):
        build_disclosure_envelope(capsule, agent_input={"a": 999})


def test_strict_mode_rejects_missing_committed_digest():
    capsule = _emit_sealed()  # no agent_input at all

    with pytest.raises(DisclosureError, match="no agent_input_digest committed"):
        build_disclosure_envelope(capsule, agent_input={"a": 1})


def test_non_strict_allows_deliberate_mismatch_for_fixtures():
    capsule = _emit_sealed(agent_input={"a": 1})

    envelope = build_disclosure_envelope(capsule, agent_input={"a": 999}, strict=False)

    assert envelope["disclosures"] == {"agent_input": {"a": 999}}


def test_envelope_disclosure_matches_verify_input_digest_result():
    """The envelope's disclosed value is exactly what verify_input_digest would accept."""
    payload = {"vendor": "Acme", "qty": 3}
    capsule = _emit_sealed(agent_input=payload)

    envelope = build_disclosure_envelope(capsule, agent_input=payload)

    assert verify_input_digest(envelope["capsule"], envelope["disclosures"]["agent_input"])
