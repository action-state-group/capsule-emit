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


def test_float_input_fails_closed_at_emit() -> None:
    """Per §5.1 a raw float can't be reproducibly digested. emit() must fail
    CLOSED — raise rather than silently seal an input its own verifier could
    never confirm. Encode monetary/quantity values as exact decimal strings."""
    from agent_action_capsule.canonical import FloatInDigestError

    with pytest.raises(FloatInDigestError):
        _emit_and_read({"amount": 19.99})


def test_verify_never_throws_on_float_candidate() -> None:
    """The profile requires a verifier to return a structured result, never
    throw. A float-bearing candidate must yield False, not FloatInDigestError —
    this is the crash/DoS surface the seal-side fix alone did NOT close."""
    capsule = _emit_and_read({"a": "x"})
    # must return a bool, not raise:
    assert capsule_emit.verify_input_digest(capsule, {"amount": 19.99}) is False


def test_non_json_native_types_still_emit() -> None:
    """Non-JSON-native types (e.g. tuples) the legacy encoder tolerated still
    seal via fallback — only floats fail closed."""
    capsule = _emit_and_read({"pair": (1, 2)})
    ca = capsule.get("model_attestation", {}).get("compute_attestation", {})
    assert len(ca["agent_input_digest"]) == 64
