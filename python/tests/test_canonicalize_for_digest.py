# SPDX-License-Identifier: Apache-2.0
"""Tests for capsule_emit.numbers.canonicalize_for_digest (capsule-emit#128).

``float_to_str`` is the rule for one number; this is the rule applied to a
whole tool payload, which is what an adapter actually holds. The vectors below
pin the three decisions that make the walk safe to put in front of every
adapter's digest:

- floats become JCS decimal *strings*, at any nesting depth
- everything a float-free payload contains comes back untouched (that is what
  keeps existing digests from moving — see test_adapter_digest_stability.py)
- NaN/±Infinity raise, naming the field path, because neither has a JCS
  representation to commit to
"""
from __future__ import annotations

import pytest
from agent_action_capsule.canonical import FloatInDigestError, jcs

from capsule_emit.numbers import canonicalize_for_digest as canon

# ---------------------------------------------------------------------------
# Floats become canonical decimal strings, at any depth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        # the capsule-emit#128 repro argument
        ({"name": "risk", "value": 0.75}, {"name": "risk", "value": "0.75"}),
        # bare float
        (0.75, "0.75"),
        # list of floats
        ({"rates": [1.5, 2.5]}, {"rates": ["1.5", "2.5"]}),
        # nested dict inside a list inside a dict
        (
            {"cfg": {"bands": [{"lo": 0.1}, {"hi": 0.9}]}},
            {"cfg": {"bands": [{"lo": "0.1"}, {"hi": "0.9"}]}},
        ),
        # integer-valued float renders without the trailing ".0" (ES6, not repr)
        ({"v": 42.0}, {"v": "42"}),
        # RFC 8785 Appendix B: negative zero
        ({"v": -0.0}, {"v": "0"}),
        # ES6 formatting, not Python repr
        ({"v": 1e-5}, {"v": "0.00001"}),
        ({"v": 1.5e21}, {"v": "1.5e+21"}),
    ],
)
def test_floats_become_jcs_decimal_strings(value, expected):
    assert canon(value) == expected


def test_deeply_nested_floats_are_all_converted():
    payload = {"a": [{"b": [{"c": [0.5, {"d": 0.25}]}]}]}
    assert canon(payload) == {"a": [{"b": [{"c": ["0.5", {"d": "0.25"}]}]}]}


# ---------------------------------------------------------------------------
# Float-free payloads are returned unchanged — the no-digest-shift property
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        {"name": "risk", "mode": "strict"},
        {"count": 3, "limit": -7, "zero": 0},
        {"on": True, "off": False, "missing": None},
        {"cfg": {"a": ["x", "y"], "n": 12}, "tag": "t"},
        {"n": 9007199254740991},
        ["a", "b", "c"],
        "risk",
        {},
        [],
        None,
        0,
        True,
    ],
)
def test_float_free_payloads_round_trip_unchanged(value):
    assert canon(value) == value


def test_booleans_are_not_treated_as_numbers():
    """``bool`` subclasses ``int`` in Python; a True must not become "1"."""
    out = canon({"flag": True, "other": False})
    assert out == {"flag": True, "other": False}
    assert out["flag"] is True and out["other"] is False


def test_integers_are_left_as_integer_tokens():
    """§5.1 permits integer tokens, so ints stay ints — see the 1 vs 1.0 test."""
    out = canon({"n": 5})
    assert out["n"] == 5 and isinstance(out["n"], int)


# ---------------------------------------------------------------------------
# int vs float: digests differ, deterministically, and this documents why
# ---------------------------------------------------------------------------


def test_int_one_and_float_one_canonicalize_differently():
    """``1`` and ``1.0`` are distinct source types with distinct carriers.

    An integer token is legal in a digest-bearing field, so ``1`` stays a JSON
    number; a float's only canonical carrier is a decimal string, so ``1.0``
    becomes ``"1"``. The two therefore digest differently. That asymmetry is
    deliberate: coercing ints to strings as well would give ``1`` and ``1.0``
    the same digest at the cost of moving every integer digest ever sealed.
    """
    assert canon({"v": 1}) == {"v": 1}
    assert canon({"v": 1.0}) == {"v": "1"}
    assert jcs(canon({"v": 1})) == b'{"v":1}'
    assert jcs(canon({"v": 1.0})) == b'{"v":"1"}'
    assert jcs(canon({"v": 1})) != jcs(canon({"v": 1.0}))


# ---------------------------------------------------------------------------
# Sequences: JSON has one array type
# ---------------------------------------------------------------------------


def test_tuples_become_lists_and_match_their_list_form():
    assert canon(("a", "b")) == ["a", "b"]
    assert jcs(canon(("a", "b"))) == jcs(canon(["a", "b"]))


def test_floats_inside_tuples_are_converted_too():
    """Without this, a float in a tuple slipped past the rule entirely.

    A tuple makes ``jcs`` raise TypeError, so ``core._digest`` fell back to
    ``json.dumps(default=str)`` — which serializes floats as Python ``repr``
    without complaint. The float was digested, just not reproducibly.
    """
    assert canon(("x", 120.5)) == ["x", "120.5"]


# ---------------------------------------------------------------------------
# NaN / Infinity — no JCS representation, so nothing to commit to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_raise(bad):
    with pytest.raises(FloatInDigestError):
        canon({"value": bad})


@pytest.mark.parametrize(
    "payload, field_path",
    [
        ({"value": float("nan")}, "value"),
        ({"cfg": {"rates": [1.0, float("inf")]}}, "cfg.rates[1]"),
        ([{"a": float("nan")}], "[0].a"),
    ],
)
def test_nan_and_infinity_name_the_field_path(payload, field_path):
    """The message must say *which* field, or an operator cannot act on it."""
    with pytest.raises(FloatInDigestError) as exc:
        canon(payload)
    assert field_path in str(exc.value)


def test_field_prefix_is_carried_into_the_path():
    with pytest.raises(FloatInDigestError) as exc:
        canon({"value": float("nan")}, field="agent_input")
    assert "agent_input.value" in str(exc.value)


# ---------------------------------------------------------------------------
# Non-string dict keys stay a (pre-existing, type-agnostic) hard failure
# ---------------------------------------------------------------------------


def test_non_string_dict_keys_are_not_rescued():
    """Keys are not converted — JCS rejects every non-string key type alike.

    Converting float keys only would make ``{1.5: "x"}`` work while
    ``{1: "x"}`` still failed. The canonicalizer leaves both to fail at the
    digest layer, where the adapters' warn-and-skip path handles them.
    """
    assert canon({1.5: "x"}) == {1.5: "x"}
    with pytest.raises(AttributeError):
        jcs(canon({1.5: "x"}))
    with pytest.raises(AttributeError):
        jcs(canon({1: "x"}))


def test_input_is_not_mutated():
    payload = {"rates": [1.5], "name": "risk"}
    canon(payload)
    assert payload == {"rates": [1.5], "name": "risk"}
    assert isinstance(payload["rates"][0], float)
