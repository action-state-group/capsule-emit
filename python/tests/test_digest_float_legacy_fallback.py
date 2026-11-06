# SPDX-License-Identifier: Apache-2.0
"""A float is refused wherever it sits, even beside a non-JSON value (capsule-emit#431).

The legacy ``default=str`` fallback used to seal such a float via ``repr()``,
which §5.1 forbids and ``verify_input_digest`` cannot reproduce.
"""
from __future__ import annotations

import datetime as dt
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


@pytest.mark.parametrize("salt", [None, "abcd"])
def test_float_free_legacy_digests_unchanged(salt):
    import hashlib
    import json

    value = {"a": (1, 2), "b": dt.date(2020, 1, 1), "s": "x", "o": Plain(3)}
    value["o"] = "plain"  # keep the str() of the object stable across runs
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if salt is not None:
        raw += "|" + salt
    assert _digest(value, salt=salt) == hashlib.sha256(raw.encode()).hexdigest()
