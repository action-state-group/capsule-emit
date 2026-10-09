# SPDX-License-Identifier: Apache-2.0
"""Frozen digest vectors for the adapters' commitment path (capsule-emit#128).

The #128 fix inserts :func:`capsule_emit.numbers.canonicalize_for_digest` in
front of every adapter digest. The whole point is that it is a no-op for
payloads that contain no floats — otherwise the fix would silently invalidate
every ``agent_input_digest`` capsule-emit has ever sealed from an adapter, and
every stored capsule_id built over one.

The hex below was captured by running these exact payloads through
``CapsuleEmitterBase.emit_capsule`` on **pristine main f07f147**, before the
canonicalizer existed. It is a golden set, not a recomputation: if a future
change to the canonicalization path moves any of these, this test fails and
names the case. Do not regenerate the constants to make it pass.

``agent_input_digest`` is the field under test because ``capsule_id`` folds in
a timestamp and a fresh action_id and so is not stable across runs.

The tuple cases are here deliberately. Tuples never reached JCS — they raise
TypeError there and fall through to ``core._digest``'s legacy
``json.dumps(default=str)`` branch. The canonicalizer now turns them into
lists, so these vectors prove that redirection is digest-identical for
float-free values. (For float-*bearing* tuples it is not identical, and that
is the bug being fixed: see
``test_canonicalize_for_digest.py::test_floats_inside_tuples_are_converted_too``.)
"""
from __future__ import annotations

import json

import pytest

from capsule_emit.adapters._base import CapsuleEmitterBase

#: (label, payload, agent_input_digest as sealed on pristine main f07f147)
FROZEN_VECTORS = [
    (
        "plain_strings",
        {"name": "risk", "mode": "strict"},
        "50beac9bdbcb5b1e2101a3af31c1fcf37600edee49e36a212178748a6c8d90de",
    ),
    (
        "ints",
        {"count": 3, "limit": -7, "zero": 0},
        "660c8f42394dac1fed4433574992aae5947433e778eb201d4186e275d8559eb0",
    ),
    (
        "bools_and_null",
        {"on": True, "off": False, "missing": None},
        "80c119a16dc14c2f7ccfb5b142e3429ff4eee2100c1aa6aeabb8bf2562d25de6",
    ),
    (
        "nested_no_float",
        {"cfg": {"a": ["x", "y"], "n": 12}, "tag": "t"},
        "40e1c0df7d432531e74493407d51cdf14c34675d5ea41bee92d474e3019095c0",
    ),
    (
        "big_int",
        {"n": 9007199254740991},
        "e1da48c6a6089f06ecb4e0a2259e658e3786b2420f52baccdf929ec6460d7b41",
    ),
    (
        "list_of_strings",
        ["a", "b", "c"],
        "fa1844c2988ad15ab7b49e0ece09684500fad94df916859fb9a43ff85f5bb477",
    ),
    (
        "bare_string",
        "risk",
        "0d1b2b5d80429c0c47800dd161e760f594bcb347f68421831c5c7d415cb20781",
    ),
    (
        "empty_dict",
        {},
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    ),
    (
        "tuple_of_strings",
        ("a", "b", "c"),
        "fa1844c2988ad15ab7b49e0ece09684500fad94df916859fb9a43ff85f5bb477",
    ),
    (
        "tuple_nested",
        {"pair": ("x", "y"), "n": 4},
        "66340f0ade90065612606574fbcc8cff68c324a4bc49f20639efdb18c2d56e4f",
    ),
    (
        "tuple_with_null",
        ("a", {"x": None}),
        "55b83c17a20f97e3944d3273366dd05ce92332a25511b11eebeaae8c210c9102",
    ),
]


def _seal_and_read(tmp_path, payload):
    ledger = tmp_path / "ledger.jsonl"
    base = CapsuleEmitterBase(
        operator="o", developer="d", ledger=ledger, anchor=False
    )
    base.emit_capsule(
        action="set_threshold",
        tool_input=payload,
        tool_output=payload,
        effect={"type": "set_threshold", "status": "confirmed"},
        action_type="fyi",
        runtime="regression-guard",
    )
    record = json.loads(ledger.read_text().splitlines()[-1])
    return record


@pytest.mark.parametrize(
    "label, payload, expected", FROZEN_VECTORS, ids=[v[0] for v in FROZEN_VECTORS]
)
def test_float_free_adapter_digests_did_not_move(tmp_path, label, payload, expected):
    record = _seal_and_read(tmp_path, payload)
    actual = record["model_attestation"]["compute_attestation"]["agent_input_digest"]
    assert actual == expected, (
        f"{label}: adapter input digest moved — pre-#128 {expected}, now {actual}. "
        "Any change here invalidates previously sealed capsules."
    )


@pytest.mark.parametrize(
    "label, payload, expected", FROZEN_VECTORS, ids=[v[0] for v in FROZEN_VECTORS]
)
def test_output_and_response_digests_agree_with_the_input_digest(
    tmp_path, label, payload, expected
):
    """Same value in, same value out — all three digests are over one payload."""
    record = _seal_and_read(tmp_path, payload)
    compute = record["model_attestation"]["compute_attestation"]
    assert compute["agent_output_digest"] == expected
    assert record["effect"]["response_digest"] == expected


def test_an_identical_string_arg_call_digests_identically_twice(tmp_path):
    """The desk's stated guard: seal the same string-arg call twice, compare."""
    payload = {"po": "PO-1", "amount": "120.00"}
    first = _seal_and_read(tmp_path / "a", payload)
    second = _seal_and_read(tmp_path / "b", payload)
    a = first["model_attestation"]["compute_attestation"]["agent_input_digest"]
    b = second["model_attestation"]["compute_attestation"]["agent_input_digest"]
    assert a == b
