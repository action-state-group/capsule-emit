# SPDX-License-Identifier: Apache-2.0
"""A float is refused wherever it sits in the standard containers, even beside a
non-JSON value (capsule-emit#431).

The legacy ``default=str`` fallback used to seal such a float via ``repr()``,
which §5.1 forbids and ``verify_input_digest`` cannot reproduce. Only dict, list,
tuple, set, frozenset, dataclasses and Pydantic models are walked; any other
object keeps its legacy behaviour untouched.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass

import pytest
from agent_action_capsule.canonical import FloatInDigestError

from capsule_emit.core import _digest


@dataclass(frozen=True)
class Box:
    v: float


@dataclass
class Holder:
    inner: object


SHAPES = {
    "tuple_before": {"a": (1,), "x": 1.5},
    "datetime_before": {"a": dt.datetime(2020, 1, 1), "x": 1.5},
    "tuple_after": {"x": 1.5, "z": (1,)},
    "float_in_tuple": {"a": (1.5,)},
    "float_in_set": {"s": {1.5}},
    "float_in_frozenset": {"s": frozenset({1.5})},
    "dataclass": {"d": Box(1.5)},
    "dataclass_in_set": {Box(1.5)},
    "nested_dataclass": {"h": Holder(Holder(Box(1.5)))},
    "float_in_list_in_tuple": {"a": ([{"k": 1.5}],), "b": dt.date(2020, 1, 1)},
    "float_dict_key_in_tuple": {"t": ({1.5: "a"},)},
}


@pytest.mark.parametrize("salt", [None, "abcd"])
@pytest.mark.parametrize("name", SHAPES)
def test_float_refused(name, salt):
    with pytest.raises(FloatInDigestError):
        _digest(SHAPES[name], salt=salt)


@pytest.mark.parametrize("salt", [None, "abcd"])
def test_model_shapes_refused(salt):
    pydantic = pytest.importorskip("pydantic")

    class Frozen(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(frozen=True)
        x: float

    class Wrap(pydantic.BaseModel):
        x: float

    class Hidden(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(frozen=True)
        x: float = pydantic.Field(exclude=True)

    class Ser(pydantic.BaseModel):
        x: float

        @pydantic.field_serializer("x")
        def _s(self, v):
            return "s"

    class Free(pydantic.BaseModel):
        n: int = 1

    for payload in (
        {Frozen(x=1.5)},
        Holder(Wrap(x=1.5)),
        {"d": Holder(Wrap(x=1.5))},
        {Hidden(x=1.5)},
        Holder(Ser(x=1.5)),
    ):
        with pytest.raises(FloatInDigestError):
            _digest(payload, salt=salt)
    # a recycled id() of a temporary dump must not hide the second model
    for _ in range(20):
        with pytest.raises(FloatInDigestError):
            _digest({"d": Holder([Free(), Ser(x=1.5)])}, salt=salt)


class _Lazy:
    def __getattr__(self, name):
        raise RuntimeError("no context")

    def __str__(self):
        return "lazy"


class _Floaty:
    def __str__(self):
        return "floaty"

    v = 1.5


@pytest.mark.parametrize("salt", [None, "abcd"])
def test_foreign_objects_keep_legacy_behaviour(salt):
    """Objects outside the standard containers are never inspected: a lazy proxy,
    a huge range and an object holding a float all seal exactly as before
    (a float held only inside such an object is a known limit)."""
    value = {"l": _Lazy(), "r": range(10**10), "o": _Floaty(), "t": (1,)}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if salt is not None:
        raw += "|" + salt
    assert _digest(value, salt=salt) == hashlib.sha256(raw.encode()).hexdigest()


def test_numpy_array_with_floats_is_a_known_limit():
    np = pytest.importorskip("numpy")
    value = {"a": np.array([1.5]), "t": (1,)}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    assert _digest(value) == hashlib.sha256(raw.encode()).hexdigest()


# Digests computed on main (a91ed79), before #431: float-free legacy-fallback payloads must not move.
_PINNED = {
    "tuple": ({"a": (1, 2), "s": "x"},
        "3170b93bd88ec63c7009e268cd09100dac442254c0ef05818b30fffb61894b6c",
        "a54d0463412e7d099764ab7a51d5246871e6b1f900353a153a7ea93a6756720d"),
    "date": ({"b": dt.date(2020, 1, 1), "n": None},
        "5615c1c85b5307c07f7a94aefaca791f4a4acec912639ec23e26f843cb1684fe",
        "bbe0003ab11bd98b152d2d47a268bc2ee9f901c3565f444a8db709dc158fbeab"),
    "frozenset": ({"f": frozenset({1}), "t": (1,)},
        "590bfdbcc41ae44af60b149f492ae2b83bf66dafaf3e22956c21d6bfb1a4c6ab",
        "98e6ca515da8f56021476fa7fb7a98ac20dc906734867db44e37c981062c7b73"),
}


@pytest.mark.parametrize("name", _PINNED)
def test_float_free_legacy_digests_unchanged(name):
    value, unsalted, salted = _PINNED[name]
    assert _digest(value) == unsalted
    assert _digest(value, salt="abcd") == salted
