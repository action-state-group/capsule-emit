# SPDX-License-Identifier: Apache-2.0
"""A float is refused wherever it sits in the standard containers, even beside a
non-JSON value (capsule-emit#431).

The legacy ``default=str`` fallback used to seal such a float via ``repr()``,
which §5.1 forbids and ``verify_input_digest`` cannot reproduce. Only dict, list,
tuple, set, frozenset, dataclasses and Pydantic models are walked; any other
object keeps its legacy behaviour untouched.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from dataclasses import InitVar, dataclass
from typing import ClassVar

import pytest
from agent_action_capsule.canonical import FloatInDigestError

from capsule_emit.core import _digest


@dataclass(frozen=True)
class Box:
    v: float


@dataclass
class Bag(set):
    score: float = 0.0


class _BadKey:
    def __str__(self):
        raise RuntimeError("boom")

    def __repr__(self):
        return "badkey"


@dataclass
class Holder:
    inner: object


def _bag(score):
    b = Bag([1])
    b.score = score
    return b


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
    "dataclass_set_subclass": {"b": _bag(1.5)},
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


_ARMED = [False]


def _hostile(base, *args):
    """A subclass of ``base`` whose Python-level hooks all raise (the C JSON
    encoder serialises these natively and never calls them)."""

    def boom(self, *a, **k):
        raise RuntimeError("user code ran")

    ns = {
        "__str__": boom,
        "__repr__": boom,
        "__format__": boom,
        "__getattr__": boom,
        "__eq__": boom,
        "__ne__": boom,
        "__bool__": boom,
        "__getitem__": boom,
        # hashable while the payload is built, raising once armed
        "__hash__": lambda self: boom(self) if _ARMED[0] else base.__hash__(self),
    }
    return type("Hostile" + base.__name__, (base,), ns)(*args)


class _FakeClass:
    """An object whose __class__ lookup (what isinstance consults) runs user code."""

    @property
    def __class__(self):
        raise RuntimeError("user code ran")


@pytest.mark.parametrize("salt", [None, "abcd"])
def test_walk_runs_no_user_code(salt):
    """The rule: the walk calls no user code (str/repr/hash/eq/getattr/iteration
    overrides) on any path. Hostile objects in float-free payloads must digest
    exactly as the legacy encoder (main) does."""
    value = {
        "t": (1,),  # forces the legacy fallback
        "d": _hostile(dict, {"a": 1}),
        "l": _hostile(list, [1, 2]),
        "s": _hostile(str, "x"),
        "tu": _hostile(tuple, (1, 2)),
        "nested": [_hostile(dict, {"k": [1]})],
    }
    sortable = dict(value)
    # JCS str()s an int subclass itself (main too), so these join the walker-only part
    value["n"] = _hostile(int, 7)
    value["nested2"] = [_hostile(dict, {"k": [_hostile(int, 3)]})]
    # the legacy encoder str()s a frozenset (running its elements' repr) and cannot
    # sort mixed keys, so those two shapes are checked on the walker alone
    value[_hostile(int, 5)] = "int-subclass key"
    value["in-set"] = frozenset({_hostile(int, 9)})
    expected_raw = json.dumps(sortable, sort_keys=True, separators=(",", ":"), default=str)
    if salt is not None:
        expected_raw += "|" + salt
    assert _digest(sortable, salt=salt) == hashlib.sha256(expected_raw.encode()).hexdigest()
    from capsule_emit.pydantic_payload import deep_float_path

    _ARMED[0] = True
    try:
        assert deep_float_path(value) is None  # the whole payload, int-subclass key included
    finally:
        _ARMED[0] = False


def test_walk_labels_found_float_without_formatting_hostile_keys():
    from capsule_emit.pydantic_payload import deep_float_path

    key = _hostile(int, 1)
    assert deep_float_path({key: [1.5]}) == "<key>[0]"
    assert deep_float_path({"a": {"b": (0, 1.5)}}) == "a.b[1]"
    assert deep_float_path({4: 1.5}) == "4"
    assert deep_float_path({"a": _hostile(dict, {"x": 1.5})}) == "a.x"
    assert deep_float_path({"o": _FakeClass(), "f": [1.5]}) == "f[0]"


class _RaisingMeta(type):
    """A metaclass whose ``__getattr__`` raises once armed (the ``@dataclass``
    decorator itself probes the class, so it is armed after class creation): the
    walk must decide dataclass-ness without reaching it (capsule-emit#435)."""

    armed = False

    def __getattr__(cls, name):
        if _RaisingMeta.armed:
            raise RuntimeError(f"metaclass __getattr__ ran for {name}")
        raise AttributeError(name)


class _PlainWithMeta(metaclass=_RaisingMeta):
    pass


@dataclass
class _DcWithMeta(metaclass=_RaisingMeta):
    x: object = None


@dataclass
class _Parent:
    p: object = None


class _Child(_Parent):  # not itself decorated; inherits the fields
    pass


@dataclass
class _Pseudo:
    a: int = 1
    cv: ClassVar[float] = 1.5
    iv: InitVar[float] = 2.5


def test_metaclass_getattr_never_runs():
    from capsule_emit.pydantic_payload import deep_float_path

    _RaisingMeta.armed = True
    try:
        assert deep_float_path({"a": _PlainWithMeta(), "b": [_PlainWithMeta]}) is None
        assert deep_float_path({"d": _DcWithMeta(x=1.5)}) == "d.x"
        assert deep_float_path({"c": _Child(p=1.5)}) == "c.p"
        assert deep_float_path({"a": _PlainWithMeta(), "f": [1.5]}) == "f[0]"
    finally:
        _RaisingMeta.armed = False


def test_dataclass_classvar_and_initvar_are_not_walked():
    from capsule_emit.pydantic_payload import deep_float_path

    assert deep_float_path({"d": _Pseudo()}) is None


class _HostileGetattribute(type):
    """Any attribute read on the class (``__mro__``, ``__dict__``) raises once armed."""

    armed = False

    def __getattribute__(cls, name):
        if _HostileGetattribute.armed:
            raise RuntimeError(f"metaclass __getattribute__ ran for {name}")
        return super().__getattribute__(name)


@dataclass
class _DcHostileAttr(metaclass=_HostileGetattribute):
    x: object = None


def test_metaclass_getattribute_never_runs():
    from capsule_emit.pydantic_payload import deep_float_path

    plain = _HostileGetattribute("P", (), {})
    _HostileGetattribute.armed = True
    try:
        assert deep_float_path({"a": plain(), "f": [1.5]}) == "f[0]"
        assert deep_float_path({"d": _DcHostileAttr(x=1.5)}) == "d.x"
    finally:
        _HostileGetattribute.armed = False


class _Desc:
    ran = False

    def __get__(self, obj, owner=None):
        _Desc.ran = True
        return 1.5

    def __set__(self, obj, value):  # a data descriptor: it wins over the instance dict
        pass


@dataclass
class _DescField:
    x: object = _Desc()
    y: object = None


@dataclass
class _SlotsDc:
    __slots__ = ("x",)  # not slots=True, which needs Python 3.10
    x: object


def _plain_default(): ...


@dataclass
class _FuncDefault:
    cb: object = _plain_default  # a function default: a non-data descriptor


class _MyField(dataclasses.Field):
    __slots__ = ()


@dataclass
class _SubField:
    x: object = None


_SubField.__dataclass_fields__["x"].__class__ = _MyField


class _EvilEntry:
    ran = False

    @property
    def _field_type(self):
        _EvilEntry.ran = True
        raise RuntimeError("user code ran")


class _NotAField:
    __dataclass_fields__ = {"a": 1, "b": _EvilEntry()}  # a dict, but not of dataclasses.Field


def test_walk_skips_descriptor_fields_and_odd_dataclass_fields():
    from capsule_emit.pydantic_payload import deep_float_path

    d = _DescField(y=1.5)
    _Desc.ran = False  # the dataclass __init__ itself reads the default once
    assert deep_float_path({"d": d}) == "d.y"
    assert not _Desc.ran
    assert deep_float_path({"d": _SlotsDc(x=1.5)}) == "d.x"
    assert deep_float_path({"o": _NotAField()}) is None
    assert not _EvilEntry.ran
    # a function default is a non-data descriptor: the instance value still counts
    assert deep_float_path({"d": _FuncDefault(cb=1.5)}) == "d.cb"
    assert deep_float_path({"d": _FuncDefault(cb=[1.5])}) == "d.cb[0]"
    assert deep_float_path({"d": _FuncDefault()}) is None

    assert deep_float_path({"w": _SubField(x=1.5)}) == "w.x"  # a Field subclass entry


@dataclass
class _InitFalse:
    rate: object = dataclasses.field(default=1.5, init=False)


@dataclass
class _InitFalseTuple:
    rate: object = dataclasses.field(default=(1.5,), init=False)


class _InitFalseChild(_InitFalse):
    pass


def test_init_false_class_level_default_is_walked():
    from capsule_emit.pydantic_payload import deep_float_path

    assert deep_float_path({"a": _InitFalse()}) == "a.rate"
    assert deep_float_path({"a": _InitFalseTuple()}) == "a.rate[0]"
    assert deep_float_path({"a": _InitFalseChild()}) == "a.rate"
