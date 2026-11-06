# SPDX-License-Identifier: Apache-2.0
"""seal() accepts Pydantic v2 models without manual serialisation (capsule-emit#426).

The model is reduced to ``model_dump(mode="json")`` before digesting, so it
digests the same as that dump. §5.1 is unchanged: a float field is still
rejected, and a field that is not JSON raises instead of becoming ``str(x)``.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from agent_action_capsule.canonical import FloatInDigestError

from capsule_emit import seal
from capsule_emit.adapters._base import CapsuleEmitterBase
from capsule_emit.core import PydanticPayloadError
from capsule_emit.verify import VerifyReason, verify_input_digest

pydantic = pytest.importorskip("pydantic")


class Inner(pydantic.BaseModel):
    note: str
    at: dt.datetime
    amount: Decimal


class Order(pydantic.BaseModel):
    id: int
    tags: list[str]
    inner: Inner


def _digest_of(tmp_path, name, payload, **kw):
    r = seal(payload, action="t", anchor=False, witness=False, ledger=tmp_path / name, **kw)
    return r.capsule["model_attestation"]["compute_attestation"]["agent_input_digest"]


def _order():
    return Order(
        id=7,
        tags=["a", "b"],
        inner=Inner(note="x", at=dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc), amount=Decimal("1.50")),
    )


def test_model_digests_like_its_json_dump(tmp_path):
    m = _order()
    assert _digest_of(tmp_path, "a.jsonl", m) == _digest_of(tmp_path, "b.jsonl", m.model_dump(mode="json"))


def test_model_inside_dict_and_list(tmp_path):
    m = _order()
    nested = {"order": m, "all": [m]}
    plain = {"order": m.model_dump(mode="json"), "all": [m.model_dump(mode="json")]}
    assert _digest_of(tmp_path, "a.jsonl", nested) == _digest_of(tmp_path, "b.jsonl", plain)


def test_salted_digest_also_accepts_a_model(tmp_path):
    r = seal(_order(), action="t", anchor=False, witness=False, ledger=tmp_path / "s.jsonl", salt_digests=True)
    assert "digest_salt" in r.capsule["model_attestation"]["compute_attestation"]


def test_float_field_still_rejected_and_names_the_fix(tmp_path):
    class F(pydantic.BaseModel):
        x: float

    with pytest.raises(FloatInDigestError, match=r"at 'x'.*Decimal"):
        _digest_of(tmp_path, "a.jsonl", F(x=1.5))


def test_plain_float_error_has_no_pydantic_hint(tmp_path):
    with pytest.raises(FloatInDigestError) as ei:
        _digest_of(tmp_path, "a.jsonl", {"x": 1.5})
    assert "Pydantic" not in str(ei.value)


def test_alias_and_serializers_follow_model_dump(tmp_path):
    class S(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(populate_by_name=True)
        user_id: int = pydantic.Field(alias="userId")
        price: Decimal

        @pydantic.field_serializer("price")
        def _price(self, v: Decimal) -> str:
            return f"USD {v:.2f}"

    m = S(userId=3, price=Decimal("2.5"))
    dump = m.model_dump(mode="json")
    assert dump == {"user_id": 3, "price": "USD 2.50"}  # field names, not aliases
    assert _digest_of(tmp_path, "a.jsonl", m) == _digest_of(tmp_path, "b.jsonl", dump)


def test_non_json_field_is_a_clear_error_not_a_guess(tmp_path):
    class Opaque:
        pass

    class Bad(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)
        thing: Opaque

    with pytest.raises(PydanticPayloadError, match="Bad cannot be sealed"):
        _digest_of(tmp_path, "a.jsonl", Bad(thing=Opaque()))
    assert not (tmp_path / "a.jsonl").exists()


def test_verify_input_digest_accepts_the_same_model(tmp_path):
    m = _order()
    r = seal(m, action="t", anchor=False, witness=False, ledger=tmp_path / "v.jsonl")
    assert verify_input_digest(r.capsule, m).reason == VerifyReason.VERIFIED
    assert verify_input_digest(r.capsule, m.model_dump(mode="json")).reason == VerifyReason.VERIFIED


def test_float_hint_is_conditional_for_plain_sibling_floats(tmp_path):
    class M(pydantic.BaseModel):
        n: int

    with pytest.raises(FloatInDigestError, match="at 'x'.*Pydantic"):
        _digest_of(tmp_path, "a.jsonl", {"m": M(n=1), "x": 1.5})


def test_very_deep_dict_keeps_the_depth_error(tmp_path):
    d: dict = {}
    d["self"] = d
    with pytest.raises(Exception) as ei:
        _digest_of(tmp_path, "a.jsonl", d)
    assert not isinstance(ei.value, RecursionError)


def test_adapter_seals_a_model_with_a_float_field(tmp_path):
    class F(pydantic.BaseModel):
        x: float

    emitter = CapsuleEmitterBase(operator="o", developer="d", ledger=tmp_path / "ad")
    res = emitter.emit_capsule("a", tool_input=F(x=1.5), tool_output={})
    dump_res = emitter.emit_capsule("a", tool_input={"x": "1.5"}, tool_output={})
    assert (
        res.capsule["model_attestation"]["compute_attestation"]["agent_input_digest"]
        == dump_res.capsule["model_attestation"]["compute_attestation"]["agent_input_digest"]
    )


def test_adapter_seals_a_model_inside_a_tuple(tmp_path):
    class F(pydantic.BaseModel):
        x: float

    emitter = CapsuleEmitterBase(operator="o", developer="d", ledger=tmp_path / "ad")
    res = emitter.emit_capsule("a", tool_input=(F(x=1.5),), tool_output={})
    ref = emitter.emit_capsule("a", tool_input=[{"x": "1.5"}], tool_output={})
    key = ("model_attestation", "compute_attestation", "agent_input_digest")

    def get(r):
        return r.capsule[key[0]][key[1]][key[2]]

    assert get(res) == get(ref)


def test_seal_refuses_a_float_model_inside_a_tuple(tmp_path):
    class F(pydantic.BaseModel):
        x: float

    with pytest.raises(FloatInDigestError):
        _digest_of(tmp_path, "a.jsonl", (F(x=1.5),))
    with pytest.raises(FloatInDigestError):
        _digest_of(tmp_path, "b.jsonl", {"a": (F(x=1.5),)})
    assert _digest_of(tmp_path, "c.jsonl", (_order(),)) == _digest_of(
        tmp_path, "d.jsonl", [_order().model_dump(mode="json")]
    )


def test_float_error_names_the_nested_field_path(tmp_path):
    class Line(pydantic.BaseModel):
        score: float

    class Doc(pydantic.BaseModel):
        order: Line

    with pytest.raises(FloatInDigestError, match=r"at 'order\.score'"):
        _digest_of(tmp_path, "a.jsonl", Doc(order=Line(score=0.5)))


@pytest.mark.parametrize("salted", [False, True])
def test_float_refused_even_beside_non_json_siblings(tmp_path, salted):
    class F(pydantic.BaseModel):
        x: float

    kw = {"salt_digests": True} if salted else {}
    for i, payload in enumerate(
        [
            {"a": (1,), "m": F(x=1.5)},
            [(1,), F(x=1.5)],
            {"d": dt.datetime(2020, 1, 1), "m": F(x=1.5)},
        ]
    ):
        with pytest.raises(FloatInDigestError):
            _digest_of(tmp_path, f"{i}.jsonl", payload, **kw)
