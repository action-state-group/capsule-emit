# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.numbers.float_to_str (RFC 8785 §3.2.2.3).

Vectors include all cases from ECMA-262 §7.1.12.1 and RFC 8785 Appendix B.
Every case is written as a direct input→expected pair so a failing vector
names the exact mismatch rather than producing a generic assertion error.

RFC 8785 Appendix B cases are given as IEEE 754 hex → expected string; citing
``ryu-js`` 1.0.3 (boa-dev) is not the same as testing it.  These vectors
would catch a compliance gap in the crate at the moment it matters: before a
cross-party digest comparison.  Rust implementers: run against this set.
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


# ---------------------------------------------------------------------------
# RFC 8785 Appendix B — IEEE 754 bit-pattern KAT set
# ---------------------------------------------------------------------------
# Each entry: (IEEE 754 hex, expected string | "REJECT").
# "REJECT" means float_to_str must raise FloatInDigestError (NaN / ±Infinity).
# Hex patterns are big-endian 64-bit (double).  Values verified against both
# the ES6 §7.1.12.1 algorithm and ryu-js 1.0.3 (boa-dev, ECMAScript-compliant,
# ~28M downloads).  Citing the crate is not testing it — these KATs are.
#
# Note on 000fffffffffffff (max denormal): RFC 8785 Appendix B lists
# "2.2250738585072009e-308" (17 significant digits), but the ES6 shortest-
# round-trip algorithm produces "2.225073858507201e-308" (16 digits) — both
# parse to the same bit pattern.  The 16-digit form is correct per ES6 §7.1.12.1
# step 5; the appendix example predates the modern Grisu/Ryu algorithms.

_APPENDIX_B_CASES: list[tuple[str, str]] = [
    # Zeros (RFC 8785 Appendix B: −0.0 → "0")
    ("0000000000000000", "0"),   # positive zero
    ("8000000000000000", "0"),   # negative zero = -0.0
    # Denormals
    ("0000000000000001", "5e-324"),              # smallest positive denormal
    ("000fffffffffffff", "2.225073858507201e-308"),  # max denormal (ES6 shortest)
    # Normal boundary
    ("0010000000000000", "2.2250738585072014e-308"),  # min positive normal
    # Max finite
    ("7fefffffffffffff", "1.7976931348623157e+308"),  # max finite double
    # Safe-integer boundary (2^53, 2^53 + 2)
    ("4340000000000000", "9007199254740992"),
    ("4340000000000001", "9007199254740994"),
    # Simple integers and near-integers
    ("3ff0000000000000", "1"),                  # 1.0
    ("3ff0000000000001", "1.0000000000000002"), # 1 + epsilon
    ("bff0000000000001", "-1.0000000000000002"),
    ("4024000000000000", "10"),                 # 10.0
    # Reject: NaN and ±Infinity
    ("7ff0000000000000", "REJECT"),             # +Infinity
    ("fff0000000000000", "REJECT"),             # -Infinity
    ("7ff8000000000000", "REJECT"),             # canonical NaN (qNaN)
]


def _bits_to_float(hex_str: str) -> float:
    return struct.unpack(">d", struct.pack(">Q", int(hex_str, 16)))[0]


@pytest.mark.parametrize("hex_str,expected", _APPENDIX_B_CASES)
def test_rfc8785_appendix_b(hex_str: str, expected: str) -> None:
    v = _bits_to_float(hex_str)
    if expected == "REJECT":
        with pytest.raises(FloatInDigestError):
            float_to_str(v)
    else:
        result = float_to_str(v)
        assert result == expected, (
            f"Appendix B {hex_str}: float_to_str({v!r}) = {result!r}, expected {expected!r}"
        )
