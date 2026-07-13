# SPDX-License-Identifier: Apache-2.0
"""Regression: emit() must seal with the same canonicalization verify checks.

Before 0.3.2, ``emit()`` sealed ``agent_input_digest`` with
``json.dumps(sort_keys=True)`` while ``verify_input_digest`` recomputes with
RFC 8785 (JCS). They coincide only for "clean" values; for a receipt carrying a
``null`` / empty container / non-ASCII field they diverged, so a faithfully
sealed input failed its own verifier. These tests lock seal ≡ verify.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import capsule_emit


def _emit_and_read(receipt: dict) -> dict:
    with tempfile.TemporaryDirectory() as d:
        ledger = str(Path(d) / "ledger.jsonl")
        capsule_emit.emit(
            action="purchase",
            operator="did:key:zOperator",
            agent_input=receipt,
            anchor=False,
            ledger=ledger,
        )
        return capsule_emit.read_ledger(ledger)[0]


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param({"a": "x", "b": "y"}, id="ascii_clean"),
        pytest.param({"a": "x", "note": None}, id="null_field"),
        pytest.param({"a": "x", "tags": []}, id="empty_list"),
        pytest.param({"a": "x", "meta": {}}, id="empty_dict"),
        pytest.param({"a": "café", "b": "naïve"}, id="unicode"),
        pytest.param({"z": 1, "a": 2, "m": {"y": 1, "x": 2}}, id="key_order"),
    ],
)
def test_faithfully_sealed_input_reverifies(receipt: dict) -> None:
    """A receipt sealed by emit() must pass verify_input_digest — any shape.

    ``verify_input_digest`` takes ``(capsule, candidate_input)``.
    """
    capsule = _emit_and_read(receipt)
    assert capsule_emit.verify_input_digest(capsule, receipt) is True


def test_tamper_after_seal_is_caught() -> None:
    """The adversarial property still holds: a mutated receipt fails verify."""
    receipt = {"a": "x", "note": None}
    capsule = _emit_and_read(receipt)
    tampered = {"a": "x", "note": "SNEAKY"}
    assert capsule_emit.verify_input_digest(capsule, tampered) is False


def test_float_input_still_emits_backward_compatible() -> None:
    """Backward-compat: a raw float does NOT break emit (falls back to the
    legacy encoding). Per §5.1 it can't be JCS-digested, so it is a known
    non-verifiable case until encoded as an exact decimal string — but existing
    float-emitting callers must not start crashing at seal time on 0.3.2."""
    capsule = _emit_and_read({"amount": 19.99})
    ca = capsule.get("model_attestation", {}).get("compute_attestation", {})
    assert "agent_input_digest" in ca  # sealed without raising
    assert len(ca["agent_input_digest"]) == 64
