# SPDX-License-Identifier: Apache-2.0
"""A non-string dict key is refused with a typed error (capsule-emit#439).

JSON object keys are strings. ``seal()`` raised the digest layer's bare
``AttributeError`` for such a key, and ``verify_input_digest`` broke its
"never raises" contract with it. Now seal raises ``NonStringKeyError`` and
verify returns NON_CONFORMING; payloads with string keys keep their digests.
"""
from __future__ import annotations

import pytest

from capsule_emit.core import _digest
from capsule_emit.pydantic_payload import NonStringKeyError, non_string_key_error
from capsule_emit.verify import VerifyReason, verify_input_digest

CAPSULE = {"model_attestation": {"compute_attestation": {"agent_input_digest": "0" * 64}}}

SHAPES = {
    "int": {1: "x"},
    "float": {1.5: "x"},
    "complex": {1 + 2j: "x"},
    "none": {None: "x"},
    "tuple": {(1, 2): "x"},
    "nested": {"a": {1: 2}},
    "in list": [{"a": 1}, {2: 3}],
    "in tuple": {"a": ({2: 3},)},
    "beside non-JSON value": {1: object()},
}


@pytest.mark.parametrize("value", SHAPES.values(), ids=SHAPES.keys())
def test_seal_refuses(value):
    with pytest.raises(NonStringKeyError):
        _digest(value)
    with pytest.raises(NonStringKeyError):
        _digest(value, salt="s")


@pytest.mark.parametrize("value", SHAPES.values(), ids=SHAPES.keys())
def test_verify_is_non_conforming_and_never_raises(value):
    result = verify_input_digest(CAPSULE, value)
    assert not result.ok
    assert result.reason is VerifyReason.NON_CONFORMING


def test_message_names_the_key_path():
    with pytest.raises(NonStringKeyError, match=r"int dict key at 'a\.<key>'"):
        _digest({"a": {1: 2}})


def test_cyclic_payload_without_bad_key_does_not_hang():
    a: list = []
    a.append(a)
    assert non_string_key_error(a) is None


def test_string_keys_unchanged():
    value = {"b": 1, "a": {"c": [1, 2]}}
    digest = _digest(value)
    capsule = {"model_attestation": {"compute_attestation": {"agent_input_digest": digest}}}
    assert verify_input_digest(capsule, value).reason is VerifyReason.VERIFIED


def test_key_type_with_hostile_metaclass_runs_no_user_code():
    class Meta(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                raise RuntimeError("metaclass ran")
            return super().__getattribute__(name)

    class Key(metaclass=Meta):
        pass

    with pytest.raises(NonStringKeyError):
        _digest({Key(): 1})
    assert verify_input_digest(CAPSULE, {Key(): 1}).reason is VerifyReason.NON_CONFORMING


def test_str_subclass_key_with_raising_encode_does_not_escape_verify():
    class K(str):
        def encode(self, *a, **k):
            raise AttributeError("encode")

    assert verify_input_digest(CAPSULE, {K("a"): 1}).reason is VerifyReason.NON_CONFORMING


def test_disclosure_envelope_reports_a_typed_error():
    from capsule_emit.disclosure import DisclosureError, build_disclosure_envelope

    capsule = {"model_attestation": {"compute_attestation": {"agent_input_digest": "0" * 64}}}
    with pytest.raises(DisclosureError, match="dict key"):
        build_disclosure_envelope(capsule, agent_input={1: "x"})
