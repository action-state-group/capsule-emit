# SPDX-License-Identifier: Apache-2.0
"""A float is refused wherever it sits, even beside a non-JSON value (capsule-emit#431).

The legacy ``default=str`` fallback used to seal such a float via ``repr()``,
which §5.1 forbids and ``verify_input_digest`` cannot reproduce.
"""
from __future__ import annotations

import array
import collections.abc
import datetime as dt
import enum
import functools
import math
import types
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


class Plain:
    def __init__(self, v):
        self.v = v


class Slotted:
    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v


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
    "plain_object": {"o": Plain(1.5)},
    "slots_object": {"o": Slotted(1.5)},
    "float_in_list_in_tuple": {"a": ([{"k": 1.5}],), "b": dt.date(2020, 1, 1)},
}


@pytest.mark.parametrize("salt", [None, "abcd"])
@pytest.mark.parametrize("name", SHAPES)
def test_float_refused(name, salt):
    with pytest.raises(FloatInDigestError):
        _digest(SHAPES[name], salt=salt)


@pytest.mark.parametrize("salt", [None, "abcd"])
def test_model_shapes_refused(salt):
    pydantic = pytest.importorskip("pydantic")

    class M(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(frozen=True)
        x: float

    class Wrap(pydantic.BaseModel):
        x: float

    with pytest.raises(FloatInDigestError):
        _digest({M(x=1.5)}, salt=salt)
    with pytest.raises(FloatInDigestError):
        _digest(Holder(Wrap(x=1.5)), salt=salt)
    with pytest.raises(FloatInDigestError):
        _digest({"d": Holder(Wrap(x=1.5))}, salt=salt)


def test_cycle_does_not_hang():
    a: dict = {"t": (1,)}
    a["self"] = a  # json.dumps also fails here; we only need the walker to end
    with pytest.raises((ValueError, RecursionError)):
        _digest(a)


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


class _Mangled:
    __slots__ = ("__x",)

    def __init__(self, v):
        self.__x = v


class _Rate(enum.Enum):
    LOW = 0.5


class _WithClassFloat:
    rate = 0.5


@pytest.mark.parametrize("salt", [None, "abcd"])
@pytest.mark.parametrize(
    "payload",
    [
        {"d": collections.deque([1.5])},
        {"a": array.array("d", [1.5])},
        {"m": types.MappingProxyType({"x": 1.5})},
        {"v": {"x": 1.5}.values()},
        {"c": 1.5 + 2j},
        {"o": _Mangled(1.5)},
    ],
)
def test_more_containers_refused(payload, salt):
    with pytest.raises(FloatInDigestError):
        _digest(payload, salt=salt)


@pytest.mark.parametrize("salt", [None, "abcd"])
def test_models_refused_even_when_dump_hides_float(salt):
    pydantic = pytest.importorskip("pydantic")

    class Hidden(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(frozen=True)
        x: float = pydantic.Field(exclude=True)

    class Free(pydantic.BaseModel):
        n: int = 1

    class Ser(pydantic.BaseModel):
        x: float

        @pydantic.field_serializer("x")
        def _s(self, v):
            return "s"

    with pytest.raises(FloatInDigestError):
        _digest({Hidden(x=1.5)}, salt=salt)
    with pytest.raises(FloatInDigestError):
        _digest(Holder(Ser(x=1.5)), salt=salt)
    # two models in a row: a recycled id() of a temporary dump must not hide the second
    for _ in range(20):
        with pytest.raises(FloatInDigestError):
            _digest({"d": Holder([Free(), Ser(x=1.5)])}, salt=salt)


def test_deep_chain_does_not_recurse():
    chain = Plain(None)
    for _ in range(5000):
        chain = Plain(chain)
    assert len(_digest({"a": (1,), "o": chain})) == 64


def test_classes_modules_enums_are_leaves():
    for payload in ({"c": _WithClassFloat}, {"m": math}, {"e": _Rate.LOW}):
        payload["t"] = (1,)
        assert len(_digest(payload)) == 64
    with pytest.raises(FloatInDigestError):
        _digest({"o": Plain(_WithClassFloat.rate), "t": (1,)})


@dataclass
class Batch:
    items: list
    rate: float

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __contains__(self, x):
        return x in self.items


class _BadIter(collections.abc.Sequence):
    def __len__(self):
        return 1

    def __getitem__(self, i):
        raise RuntimeError("closed")


@pytest.mark.parametrize("salt", [None, "abcd"])
@pytest.mark.parametrize(
    "payload",
    [
        {"b": Batch([1], 1.5)},
        {"e": ValueError(1.5)},
        {"p": functools.partial(print, 1.5)},
        {"s": slice(1.5)},
    ],
)
def test_collection_attrs_exceptions_partials_refused(payload, salt):
    with pytest.raises(FloatInDigestError):
        _digest(payload, salt=salt)


def test_numpy_scalar_refused():
    np = pytest.importorskip("numpy")
    with pytest.raises(FloatInDigestError):
        _digest({"x": np.float32(1.5), "t": (1,)})


def test_numpy_zero_d_float_refused_and_big_int_array_is_fast():
    np = pytest.importorskip("numpy")
    import time

    for arr in (np.array(1.5), np.array(1.5, dtype="float32"), np.array([1.5])):
        with pytest.raises(FloatInDigestError):
            _digest({"a": arr, "t": (1,)})
    start = time.monotonic()
    assert len(_digest({"a": np.arange(10**7), "t": (1,)})) == 64
    assert time.monotonic() - start < 2


def test_numpy_structured_dtypes():
    np = pytest.importorskip("numpy")
    for arr in (
        np.array([(1, 1.5)], dtype=[("i", "i4"), ("f", "f8")]),
        np.array([(1, 1.5)], dtype=[("i", "i4"), ("f", "f8")])[0],
        np.zeros(1, dtype=[("a", "f8", (2,))]),
    ):
        with pytest.raises(FloatInDigestError):
            _digest({"a": arr, "t": (1,)})
    ints = np.array([(1, 2)], dtype=[("i", "i4"), ("j", "i4")])
    assert len(_digest({"a": ints, "t": (1,)})) == 64


class _Lazy:
    def __getattr__(self, name):
        raise RuntimeError("no context")

    def __str__(self):
        return "lazy"


def test_raising_getattr_does_not_break_float_free_payload():
    assert len(_digest({"o": _Lazy(), "t": (1,)})) == 64


def test_iteration_failure_and_big_range_do_not_break_float_free_payloads():
    assert len(_digest({"b": _BadIter(), "t": (1,)})) == 64
    assert len(_digest({"r": range(10**10), "t": (1,)})) == 64
