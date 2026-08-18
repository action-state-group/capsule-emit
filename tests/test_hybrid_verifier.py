# SPDX-License-Identifier: Apache-2.0
"""Tests for the HYBRID verify_input_digest redesign (CPB §5 second pass).

Four arms, all tested:
  1. Lenient VERIFIED        — correct candidate, no float value
  2. Lenient DIGEST_MISMATCH — wrong value, conforming candidate
  3. Lenient NON_CONFORMING  — float value in candidate (value-rule violation)
  4. Strict  NON_CONFORMING  — float *token* in raw bytes where lenient PASSES

Arm 4 is the critical case that proves the strict tier is distinct from lenient:
the same integer value passes lenient (Python dict has int 42) but fails strict
when the raw JSON bytes used float token form (``42.0``).

Behavioral deltas named (vs. as-built bool return):
  - Return type: InputDigestResult, not bool. is True / is False identity breaks.
  - Float candidate: NON_CONFORMING, not False (reason is now distinguishable).
  - Wrong-value candidate: DIGEST_MISMATCH, not False (reason distinguishable).
  - Absent stored digest: DIGEST_MISMATCH (same verdict, richer info).
  - Strict tier: new — token-form check from raw_json_bytes.
"""
from __future__ import annotations

import os
import tempfile

from capsule_emit import InputDigestResult, VerifyReason, emit, verify_input_digest


def _tmp_ledger() -> str:
    return os.path.join(tempfile.mkdtemp(), "ledger.jsonl")


def _emit_with(agent_input: dict) -> dict:
    r = emit(
        "test_action",
        operator="op",
        developer="dev",
        agent_input=agent_input,
        anchor=False,
        ledger=_tmp_ledger(),
    )
    return r.capsule


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------


def test_returns_input_digest_result() -> None:
    capsule = _emit_with({"a": 1})
    result = verify_input_digest(capsule, {"a": 1})
    assert isinstance(result, InputDigestResult)


def test_truthy_on_verified() -> None:
    capsule = _emit_with({"a": 1})
    assert verify_input_digest(capsule, {"a": 1})


def test_falsy_on_mismatch() -> None:
    capsule = _emit_with({"a": 1})
    assert not verify_input_digest(capsule, {"a": 2})


# ---------------------------------------------------------------------------
# Arm 1: Lenient VERIFIED
# ---------------------------------------------------------------------------


def test_lenient_verified_integer_value() -> None:
    capsule = _emit_with({"amount": 42, "label": "ok"})
    result = verify_input_digest(capsule, {"amount": 42, "label": "ok"})
    assert result.ok
    assert result.reason == VerifyReason.VERIFIED


def test_lenient_verified_string_value() -> None:
    capsule = _emit_with({"price": "19.99"})
    result = verify_input_digest(capsule, {"price": "19.99"})
    assert result.ok
    assert result.reason == VerifyReason.VERIFIED


# ---------------------------------------------------------------------------
# Arm 2: Lenient DIGEST_MISMATCH
# ---------------------------------------------------------------------------


def test_lenient_digest_mismatch_wrong_value() -> None:
    capsule = _emit_with({"amount": 42})
    result = verify_input_digest(capsule, {"amount": 99})
    assert not result.ok
    assert result.reason == VerifyReason.DIGEST_MISMATCH


def test_lenient_digest_mismatch_absent_digest() -> None:
    """A capsule emitted without agent_input has no stored digest → DIGEST_MISMATCH."""
    r = emit("ping", operator="op", developer="dev", anchor=False, ledger=_tmp_ledger())
    capsule = r.capsule
    result = verify_input_digest(capsule, {"a": 1})
    assert not result.ok
    assert result.reason == VerifyReason.DIGEST_MISMATCH


# ---------------------------------------------------------------------------
# Arm 3: Lenient NON_CONFORMING (value-rule: float in candidate)
# ---------------------------------------------------------------------------


def test_lenient_non_conforming_float_candidate() -> None:
    """A raw float in the candidate violates the value rule → NON_CONFORMING, not DIGEST_MISMATCH."""
    capsule = _emit_with({"label": "ok"})
    result = verify_input_digest(capsule, {"amount": 19.99})
    assert not result.ok
    assert result.reason == VerifyReason.NON_CONFORMING


def test_lenient_non_conforming_is_distinct_from_mismatch() -> None:
    """NON_CONFORMING and DIGEST_MISMATCH are distinct reasons — a float candidate must not
    be silently collapsed to the same False as a wrong-value candidate."""
    capsule = _emit_with({"x": 1})
    float_result = verify_input_digest(capsule, {"x": 1.0})   # float value
    wrong_result = verify_input_digest(capsule, {"x": 2})      # wrong int
    assert float_result.reason == VerifyReason.NON_CONFORMING
    assert wrong_result.reason == VerifyReason.DIGEST_MISMATCH
    assert float_result.reason != wrong_result.reason


def test_lenient_never_raises_on_float_candidate() -> None:
    """Profile contract: a verifier MUST return a result, never raise."""
    capsule = _emit_with({"a": "x"})
    result = verify_input_digest(capsule, {"a": float("inf")})
    assert isinstance(result, InputDigestResult)
    assert not result.ok


# ---------------------------------------------------------------------------
# Arm 4: Strict NON_CONFORMING where Lenient VERIFIED (the critical case)
# ---------------------------------------------------------------------------


def test_strict_fails_where_lenient_passes_float_token() -> None:
    """The declared gap in action: same integer value, different token form.

    Capsule sealed with {"amount": 42} (integer value).
    Caller's dict: {"amount": 42} — Python int, passes lenient.
    Caller's raw bytes: b'{"amount": 42.0}' — float token, fails strict.

    This is the scenario proving the strict tier is not just a duplicate of
    lenient: lenient has no way to see the token form; strict does.
    """
    capsule = _emit_with({"amount": 42})
    candidate_dict = {"amount": 42}
    float_token_bytes = b'{"amount": 42.0}'

    lenient_result = verify_input_digest(capsule, candidate_dict)
    strict_result = verify_input_digest(
        capsule, candidate_dict, strict=True, raw_json_bytes=float_token_bytes
    )

    assert lenient_result.ok, "Lenient must PASS: same value as sealed, Python int"
    assert lenient_result.reason == VerifyReason.VERIFIED

    assert not strict_result.ok, "Strict must FAIL: float token violates producer MUST"
    assert strict_result.reason == VerifyReason.NON_CONFORMING


def test_strict_passes_on_integer_token() -> None:
    """Strict tier must not reject a conforming producer."""
    capsule = _emit_with({"amount": 42})
    int_token_bytes = b'{"amount": 42}'
    result = verify_input_digest(
        capsule, {"amount": 42}, strict=True, raw_json_bytes=int_token_bytes
    )
    assert result.ok
    assert result.reason == VerifyReason.VERIFIED


def test_strict_non_conforming_nested_float_token() -> None:
    """Strict tier catches float tokens nested inside objects."""
    capsule = _emit_with({"nested": {"val": 1}})
    raw = b'{"nested": {"val": 1.0}}'
    strict_result = verify_input_digest(
        capsule, {"nested": {"val": 1}}, strict=True, raw_json_bytes=raw
    )
    lenient_result = verify_input_digest(capsule, {"nested": {"val": 1}})
    assert lenient_result.ok
    assert not strict_result.ok
    assert strict_result.reason == VerifyReason.NON_CONFORMING


def test_strict_non_conforming_list_float_token() -> None:
    """Strict tier catches float tokens inside lists."""
    capsule = _emit_with({"vals": [1, 2, 3]})
    raw = b'{"vals": [1, 2.0, 3]}'
    strict_result = verify_input_digest(
        capsule, {"vals": [1, 2, 3]}, strict=True, raw_json_bytes=raw
    )
    lenient_result = verify_input_digest(capsule, {"vals": [1, 2, 3]})
    assert lenient_result.ok
    assert not strict_result.ok
    assert strict_result.reason == VerifyReason.NON_CONFORMING


def test_strict_without_raw_bytes_behaves_as_lenient() -> None:
    """strict=True with no raw_json_bytes: strict tier is silently skipped, lenient applies."""
    capsule = _emit_with({"a": 1})
    result = verify_input_digest(capsule, {"a": 1}, strict=True, raw_json_bytes=None)
    assert result.ok
    assert result.reason == VerifyReason.VERIFIED


def test_strict_invalid_json_bytes_non_conforming() -> None:
    """Malformed raw_json_bytes in strict mode → NON_CONFORMING (fail-closed)."""
    capsule = _emit_with({"a": 1})
    result = verify_input_digest(
        capsule, {"a": 1}, strict=True, raw_json_bytes=b"not valid json {"
    )
    assert not result.ok
    assert result.reason == VerifyReason.NON_CONFORMING


# ---------------------------------------------------------------------------
# Mutant: tampering must still be caught in all modes
# ---------------------------------------------------------------------------


def test_mutant_tampered_value_fails_strict_too() -> None:
    """A tampered value fails in both modes (DIGEST_MISMATCH, not NON_CONFORMING)."""
    capsule = _emit_with({"amount": 42})
    wrong_bytes = b'{"amount": 99}'
    lenient = verify_input_digest(capsule, {"amount": 99})
    strict = verify_input_digest(
        capsule, {"amount": 99}, strict=True, raw_json_bytes=wrong_bytes
    )
    assert lenient.reason == VerifyReason.DIGEST_MISMATCH
    assert strict.reason == VerifyReason.DIGEST_MISMATCH


# ---------------------------------------------------------------------------
# canonicalization_id stays parameterized (no hardcoded id in verifier)
# ---------------------------------------------------------------------------


def test_canonicalization_id_is_parameterized() -> None:
    """The id value read from CANONICALIZATION_ID must be the same constant
    that was written into the capsule — no hardcoded 'jcs-n' in the verifier."""
    from capsule_emit.numbers import CANONICALIZATION_ID
    r = emit("ping", operator="op", developer="dev", anchor=False, ledger=_tmp_ledger())
    assert r.capsule["canonicalization_id"] == CANONICALIZATION_ID
    # The verifier does not inspect canonicalization_id (that is the sibling
    # task [capsule-emit-canonicalization-id-emitter]); we assert here only
    # that the id is NOT hardcoded at any verify call site.
    import inspect

    from capsule_emit import verify
    source = inspect.getsource(verify)
    assert "jcs-n" not in source, (
        "The verifier must not hardcode the canonicalization_id value 'jcs-n'; "
        "use CANONICALIZATION_ID from capsule_emit.numbers"
    )
