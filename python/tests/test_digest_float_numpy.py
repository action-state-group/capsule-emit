# SPDX-License-Identifier: Apache-2.0
"""A float held in a numpy array or scalar is refused (capsule-emit#433).

The legacy ``default=str`` fallback used to seal it via ``repr()``, which §5.1
forbids and ``verify_input_digest`` cannot reproduce. The walk decides by dtype
alone and runs no user code; float-free arrays keep their legacy digests.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from agent_action_capsule.canonical import FloatInDigestError

from capsule_emit.core import _digest

np = pytest.importorskip("numpy")


@pytest.mark.parametrize("salt", [None, "abcd"])
@pytest.mark.parametrize(
    "arr",
    [
        lambda: np.array([1.5]),
        lambda: np.array(1.5),
        lambda: np.array([1.5], dtype=np.float32),
        lambda: np.array([1.5], dtype=np.float16),
        lambda: np.array([1.5], dtype=np.longdouble),
        lambda: np.array([1 + 2j]),
        lambda: np.array([1 + 2j], dtype=np.complex64),
        lambda: np.zeros((2, 3)),
        lambda: np.zeros(2, dtype=[("n", "i4"), ("x", "f8")]),
        lambda: np.zeros(2, dtype=[("n", "i4"), ("s", [("y", "f4")])]),
        lambda: np.zeros(2, dtype=[("n", "i4"), ("v", "f8", (3,))]),
        lambda: np.ma.array([1.5]),
        lambda: np.zeros(1, dtype=[("n", "i4"), ("x", "f8")])[0],
        lambda: np.float32(1.5),
        lambda: np.complex128(1 + 2j),
    ],
)
def test_float_in_numpy_is_refused(arr, salt):
    for value in (
        {"a": arr()},
        arr(),
        {"a": arr(), "t": (1,)},
        {"a": [arr()], "t": (1,)},
        [{"deep": {"x": (arr(),)}}],
    ):
        with pytest.raises(FloatInDigestError):
            _digest(value, salt=salt)


def test_object_dtype_float_is_a_known_limit():
    for arr in (
        np.array([1.5], dtype=object),
        np.array([(1.5,)], dtype=[("o", "O")]),
    ):
        value = {"a": arr, "t": (1,)}
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        assert _digest(value) == hashlib.sha256(raw.encode()).hexdigest()


def test_lazy_numpy_module_is_not_imported(monkeypatch):
    import sys
    import types

    reads = []

    class Lazy(types.ModuleType):
        def __getattr__(self, name):
            reads.append(name)  # a real lazy module would import numpy here
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "numpy", Lazy("numpy"))
    value = {"t": (1,), "a": 1}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    assert _digest(value) == hashlib.sha256(raw.encode()).hexdigest()
    assert reads == []


def test_error_names_the_path():
    with pytest.raises(FloatInDigestError, match=r"'a\[1\]'"):
        _digest({"a": [1, np.array([1.5])], "t": (1,)})


class _Sub(np.ndarray):
    @property
    def dtype(self):  # would hide the float dtype if the walker used attribute access
        raise RuntimeError("boom")

    def __array__(self, *a, **k):
        raise RuntimeError("boom")


def test_subclass_overrides_never_run():
    arr = np.array([1.5]).view(_Sub)
    with pytest.raises(FloatInDigestError):
        _digest({"a": arr, "t": (1,)})


def test_huge_array_is_not_read():
    big = np.broadcast_to(np.float64(1.5), (10**6, 10**4))  # 80 GB if materialized
    with pytest.raises(FloatInDigestError):
        _digest({"a": big, "t": (1,)})


def test_no_float_dtype_no_refusal():
    for arr in (
        np.zeros(1, dtype=[("n", "i4"), ("e", "f8", (0,))]),
        np.zeros(1, dtype=[("n", "i4"), ("e", "f8", (2, 0))]),
        np.array([1, 2]),
        np.array([True]),
        np.array(["x"]),
        np.array([], dtype=np.float64),
        np.zeros((0, 3)),
        np.array([1, "a"], dtype=object),
        np.int32(3),
        np.zeros(2, dtype=[("n", "i4"), ("s", "S3")]),
    ):
        value = {"a": arr, "t": (1,)}
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        assert _digest(value) == hashlib.sha256(raw.encode()).hexdigest()


# Digests computed on main (b0eba2b) before #433: float-free numpy payloads must not move.
_PINNED = {
    "int": (lambda: np.array([1, 2]),
        "6742fee9d69682894c4e6c7854928544a5242cb9e4beb99d6d6fadb6ab591ac8",
        "eab11eb95ab9f2118405349c42f894816a2c0bb70c697bb361eb6468def0fe54"),
    "bool": (lambda: np.array([True]),
        "5c93c9e0e0876faf73b0c819c61bfffd8d4d9e1620afba8ff7ce1ef6fe821407",
        "a9ae23e3efe51cf01f26cc83eb2d8c8162ff86917ea8881df9738d21154daae1"),
    "str": (lambda: np.array(["x"]),
        "89c71ff13155520ad3770f3954f7ef4602e45b7680fe96639cafbfd42004a0de",
        "846b3958f8c94ec6ebe7d6d852bf42820cf377c24e79a1151bdcc220ff8c30ff"),
    "empty_float": (lambda: np.array([]),
        "369754b7498ca184d7e5f4a4b8d752b1ed0aa32e428b271458cdd1a20a6485e9",
        "0a9291d99b6d1e9fa6d92dad2dd590187aa3a482302ffaea0cc52d1a33feb251"),
    "object": (lambda: np.array([1, "a"], dtype=object),
        "fc0fd35e397b24f5f71d824783e655438dc07604c1de8f3dde08651ea2514736",
        "958703d2486bf1379634fcb34da074a3578f55bf709adabe13e4fe402ed2538a"),
    "int32_scalar": (lambda: np.int32(3),
        "5073533f5261c658fa21ac810200e5a70affb75df313fc198568381457397b5f",
        "101be03b5ca5d01c8f47c81e15712b021625f18f4714e0b7ed15bc1674ffd65a"),
}


@pytest.mark.parametrize("name", _PINNED)
def test_float_free_numpy_digests_unchanged(name):
    make, unsalted, salted = _PINNED[name]
    value = {"a": make(), "t": (1,)}
    assert _digest(value) == unsalted
    assert _digest(value, salt="abcd") == salted
