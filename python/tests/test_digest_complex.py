# SPDX-License-Identifier: Apache-2.0
"""A builtin complex is refused like a float (capsule-emit#437).

A complex is two floats, so §5.1's reason for refusing floats applies. It used to
seal as its ``repr()`` through ``default=str``. The refusal is write-side only:
``verify_input_digest`` refuses the same candidate, and complex-free payloads keep
their digests.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest
from agent_action_capsule.canonical import FloatInDigestError

from capsule_emit.core import _digest
from capsule_emit.verify import VerifyReason, verify_input_digest


@dataclasses.dataclass
class Box:
    v: complex


class Cx(complex):
    pass


SHAPES = {
    "top": (1 + 2j, "<payload>"),
    "dict": ({"a": 1 + 2j}, "a"),
    "nested": ({"a": {"b": [0, 1 + 2j]}}, "a.b[1]"),
    "list": ([1, 1 + 2j], "[1]"),
    "tuple": ({"a": (1 + 2j,)}, "a[0]"),
    "set": ({"s": {1 + 2j}}, "s[0]"),
    "frozenset": ({"s": frozenset({1 + 2j})}, "s[0]"),
    "dataclass": ({"d": Box(1 + 2j)}, "d.v"),
    "subclass": ({"a": Cx(1, 2)}, "a"),
    "zero_imag": ({"a": 1 + 0j}, "a"),
}


@pytest.mark.parametrize("salt", [None, "abcd"])
@pytest.mark.parametrize("name", SHAPES)
def test_complex_is_refused_with_the_float_path(name, salt):
    value, where = SHAPES[name]
    with pytest.raises(FloatInDigestError) as exc:
        _digest(value, salt)
    assert repr(where) in str(exc.value)


@pytest.mark.parametrize("salt", [None, "abcd"])
def test_complex_beside_a_pydantic_model_is_refused(salt):
    pydantic = pytest.importorskip("pydantic")

    class M(pydantic.BaseModel):
        n: int

    with pytest.raises(FloatInDigestError) as exc:
        _digest({"m": M(n=1), "c": 1 + 2j}, salt)
    assert "'c'" in str(exc.value)


def test_pydantic_complex_field_digests_as_its_json_dump():
    """Not refused, by design: ``model_dump(mode="json")`` turns a complex field
    into a string before the float walk runs, and a model digests as its dump
    (#426), the same as a ``Decimal`` field. A bare complex beside a model is
    refused (above)."""
    pydantic = pytest.importorskip("pydantic")

    class M(pydantic.BaseModel):
        c: complex

    assert _digest({"m": M(c=1 + 2j)}) == _digest({"m": {"c": "1+2j"}})


def test_complex_free_payloads_keep_their_digests():
    import datetime as dt

    value = {"a": (1, 2), "b": dt.date(2020, 1, 1), "c": "1+2j", "d": [1, None]}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    assert _digest(value) == hashlib.sha256(raw.encode()).hexdigest()
    assert _digest({"a": 1, "b": "x"}) == _digest({"b": "x", "a": 1})


@pytest.mark.parametrize("name", SHAPES)
def test_verify_input_digest_refuses_the_same_candidate(name):
    value, _ = SHAPES[name]
    capsule = {"model_attestation": {"compute_attestation": {"agent_input_digest": "0" * 64}}}
    result = verify_input_digest(capsule, value)
    assert not result.ok
    assert result.reason is VerifyReason.NON_CONFORMING


def test_pydantic_model_inside_a_dataclass_is_refused_for_a_complex_field():
    """A model nested in a dataclass or set is not dumped before the walk, so its
    complex field is refused (the changelog states the container split)."""
    pydantic = pytest.importorskip("pydantic")

    class M(pydantic.BaseModel):
        c: complex

    with pytest.raises(FloatInDigestError) as exc:
        _digest(Box(M(c=1 + 2j)))
    assert "'v.c'" in str(exc.value)


class _RaisingMeta(type):
    armed = False

    def __getattr__(cls, name):
        if _RaisingMeta.armed:
            raise RuntimeError(f"metaclass __getattr__ ran for {name}")
        raise AttributeError(name)


class _WithMeta(metaclass=_RaisingMeta):
    pass


def test_complex_walk_runs_no_metaclass_code():
    _RaisingMeta.armed = True
    try:
        with pytest.raises(FloatInDigestError):
            _digest({"a": _WithMeta(), "b": [_WithMeta], "c": 1 + 2j})
    finally:
        _RaisingMeta.armed = False


def test_exempt_pydantic_complex_field_verifies_against_its_seal():
    pydantic = pytest.importorskip("pydantic")

    class M(pydantic.BaseModel):
        c: complex

    model = M(c=1 + 2j)
    capsule = {"model_attestation": {"compute_attestation": {"agent_input_digest": _digest(model)}}}
    result = verify_input_digest(capsule, model)
    assert result.ok and result.reason is VerifyReason.VERIFIED
