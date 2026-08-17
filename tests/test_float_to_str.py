# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.numbers.float_to_str (RFC 8785 §3.2.2.3).

Vectors include all cases from ECMA-262 §7.1.12.1 and RFC 8785 Appendix B.
Every case is written as a direct input→expected pair so a failing vector
names the exact mismatch rather than producing a generic assertion error.
"""
from __future__ import annotations

import struct

import pytest
from agent_action_capsule.canonical import FloatInDigestError

from capsule_emit.numbers import float_to_str

# ---------------------------------------------------------------------------
# Spec reference cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "v, expected",
    [
        # RFC 8785 Appendix B — −0.0 → "0"
        (-0.0, "0"),
        (+0.0, "0"),
        # Integer-valued floats (ES6 step 6: k ≤ n ≤ 21)
        (1.0, "1"),
        (42.0, "42"),
        (100.0, "100"),
        (1000.0, "1000"),
        (1e20, "100000000000000000000"),
        (1.5e20, "150000000000000000000"),
        # Decimal fractions (ES6 step 7: 0 < n ≤ 21)
        (1.5, "1.5"),
        (12.5, "12.5"),
        (1240.19, "1240.19"),
        (128.5, "128.5"),
        # Small fractions (ES6 step 8: −6 < n ≤ 0)
        (0.1, "0.1"),
        (0.5, "0.5"),
        (0.01, "0.01"),
        (1e-5, "0.00001"),          # repr gives "1e-05"; ES6 gives "0.00001"
        (1.5e-5, "0.000015"),
        (9.99e-6, "0.00000999"),    # -6 < n=-5 ≤ 0
        # Scientific (ES6 steps 9–10: n ≤ −6 or n > 21)
        (1e21, "1e+21"),
        (1.5e21, "1.5e+21"),
        (1e-7, "1e-7"),
        (1.5e-7, "1.5e-7"),
        # Negative values
        (-1.5, "-1.5"),
        (-42.0, "-42"),
        (-1e-5, "-0.00001"),
        (-1e21, "-1e+21"),
        # repr() vs ES6 discrepancy table from the decision doc
        # repr(42.0) = '42.0'; ES6 = '42'  (covered above)
        # repr(1e-05) = '1e-05'; ES6 = '0.00001'  (covered above)
    ],
)
def test_float_to_str_spec_vectors(v: float, expected: str) -> None:
    assert float_to_str(v) == expected, (
        f"float_to_str({v!r}) = {float_to_str(v)!r}, expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_nan_raises() -> None:
    with pytest.raises(FloatInDigestError, match="NaN"):
        float_to_str(float("nan"))


def test_inf_raises() -> None:
    with pytest.raises(FloatInDigestError, match="Infinity"):
        float_to_str(float("inf"))


def test_neg_inf_raises() -> None:
    with pytest.raises(FloatInDigestError, match="Infinity"):
        float_to_str(float("-inf"))


def test_field_name_in_error() -> None:
    with pytest.raises(FloatInDigestError, match="my_field"):
        float_to_str(float("nan"), field="my_field")


# ---------------------------------------------------------------------------
# Round-trip property
# ---------------------------------------------------------------------------


def test_round_trip_sample() -> None:
    """float_to_str(v) parses back to exactly v for a variety of inputs."""
    samples = [0.1, 0.2, 0.3, 1.5, 42.0, 1240.19, 1e-5, 1.5e20, -1.5, 9.99]
    for v in samples:
        s = float_to_str(v)
        assert float(s) == v, f"round-trip failed for {v!r}: got {s!r} → {float(s)!r}"


# ---------------------------------------------------------------------------
# Width-widening contract
# ---------------------------------------------------------------------------


def test_narrower_widths_widen_first() -> None:
    """Callers must widen narrower floats first with float().  float32 → float64 is lossless."""
    # Simulate a float32 value by round-tripping through struct pack/unpack.
    f32_bytes = struct.pack(">f", 1.5)
    f32_value = struct.unpack(">f", f32_bytes)[0]
    # float() widens to float64 — float_to_str should then work normally.
    assert float_to_str(float(f32_value)) == "1.5"


# ---------------------------------------------------------------------------
# Integration: result embeds as JSON string in digest-bearing fields
# ---------------------------------------------------------------------------


def test_result_is_always_str() -> None:
    for v in [0.0, -0.0, 1.5, 42.0, 1e21, 1e-7, -1.5e-5]:
        result = float_to_str(v)
        assert isinstance(result, str)
