# SPDX-License-Identifier: Apache-2.0
"""Behavior and error paths of capsule_emit.produce. Byte identity with Go and
TypeScript is in test_produce_interop.py."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from agent_action_capsule.media_types import CAPSULE_ID_MEDIA_TYPE
from agent_action_capsule.versions import ACCEPTED_SPEC_VERSIONS, DEFAULT_SPEC_VERSION
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from capsule_emit import produce as p
from capsule_emit.produce.digest import MAX_JSON_DEPTH

SEED = bytes(range(32))
HEX_A = "a" * 64
COORDS = {"log_id": "l", "leaf_index": 3, "inclusion_proof": [HEX_A]}
INTEROP_V04 = Path(__file__).resolve().parents[2] / "go" / "testdata" / "capsule-emit" / "format4-interop" / "valid"


def _input(**changes) -> p.Input:
    base = p.Input(
        action_id="act/1",
        action_type=p.ActionType.FYI,
        operator="example-org",
        developer="example-agent@v1",
        timestamp="2026-10-01T08:00:00Z",
    )
    return dataclasses.replace(base, **changes)


def _identity() -> p.SigningIdentity:
    return p.new_ed25519_signing_identity(SEED)


# --- constants -------------------------------------------------------------


def test_constants_mirror_agent_action_capsule():
    assert p.SPEC_VERSION == DEFAULT_SPEC_VERSION
    assert p.ACCEPTED_SPEC_VERSIONS == tuple(ACCEPTED_SPEC_VERSIONS)
    assert p.CONTENT_TYPE == CAPSULE_ID_MEDIA_TYPE
    assert (p.FORMAT_VERSION, p.CANONICALIZATION_ID) == ("4", "jcs")


def test_produce_imports_no_cll():
    package = Path(p.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "import cll" not in text and "from cll" not in text, source.name


# --- digest_json and decode_payload ---------------------------------------


def test_digest_json_matches_jcs_sha256():
    assert p.digest_json({"b": 1, "a": "x"}) == p.digest_json({"a": "x", "b": 1})
    assert len(p.digest_json(None)) == 64


@pytest.mark.parametrize(
    "value, error",
    [
        ({"x": 1.5}, ValueError),
        ({"x": 2**53}, ValueError),
        ({"x": "\ud800"}, ValueError),
        ({"\udc00": 1}, ValueError),
        ({1: "x"}, TypeError),
        ((1, 2), TypeError),
        (b"{}", TypeError),
    ],
)
def test_digest_json_rejects(value, error):
    with pytest.raises(error):
        p.digest_json(value)


def test_digest_json_rejects_excessive_depth():
    deep: list = []
    for _ in range(1001):
        deep = [deep]
    with pytest.raises(ValueError, match="maximum depth"):
        p.digest_json(deep)


def test_decode_payload_accepts_bytes_and_str():
    assert p.decode_payload(b'{"a": 1}') == {"a": 1}
    assert p.decode_payload('{"a": [true, null]}') == {"a": [True, None]}


@pytest.mark.parametrize(
    "data, message",
    [
        (b'{"a": 1, "a": 2}', "duplicate object key"),
        (b'{"a": 1} {}', "invalid JSON"),
        (b'{"a": "\xff"}', "UTF-8"),
        (b'{"a": NaN}', "non-standard JSON constant"),
        (b"[1]", "not an object"),
        (b"[" * 5000 + b"]" * 5000, "maximum depth"),
        (b'{"a": "\\ud800"}', "unpaired surrogate"),
    ],
)
def test_decode_payload_rejects(data, message):
    with pytest.raises(ValueError, match=message):
        p.decode_payload(data)


def test_decode_payload_rejects_non_text():
    with pytest.raises(TypeError):
        p.decode_payload(12)


# --- timestamps ------------------------------------------------------------


def test_format_timestamp_datetime():
    tz = timezone(timedelta(hours=-5))
    assert p.format_timestamp(datetime(2026, 10, 1, 23, 30, 0, 1, tzinfo=tz)) == "2026-10-02T04:30:00.000001Z"
    assert p.format_timestamp(datetime(2026, 10, 1, 8, tzinfo=timezone.utc)) == "2026-10-01T08:00:00Z"


@pytest.mark.parametrize(
    "value, message",
    [
        (datetime(2026, 10, 1, 8), "timezone-aware"),
        ("2026-10-01T08:00:00", "RFC 3339"),
        ("2026-10-01 08:00:00Z", "RFC 3339"),
        ("2026-02-30T08:00:00Z", "invalid"),
        ("2026-10-01T08:00:00+25:00", "invalid"),
        ("２０２６-10-01T08:00:00Z", "RFC 3339"),
        (None, "required"),
        (1700000000, "required"),
    ],
)
def test_format_timestamp_rejects(value, message):
    with pytest.raises(ValueError, match=message):
        p.format_timestamp(value)


# --- build -----------------------------------------------------------------


def test_build_returns_verified_detached_capsule():
    built = p.build(_input())
    assert built.json == json.dumps(built.value, separators=(",", ":"), sort_keys=True).encode()
    assert built.value["spec_version"] == p.SPEC_VERSION
    assert built.value["assurance"] == {
        "effect_mode": "not_applicable",
        "attestation_mode": "self_attested",
        "ledger_mode": "standalone",
    }
    assert "signature" not in built.value and "key_id" not in built.value
    assert p.verify_capsule(built.json).capsule_id == built.capsule_id


def test_build_is_deterministic_and_detached_from_caller_values():
    coordinates = {"log_id": "l", "leaf_index": 1, "inclusion_proof": [HEX_A]}
    reference = p.Reference(type="capsule", digest_alg="SHA-256", digest=HEX_A, log_coordinates=coordinates)
    first = p.build(_input(references=[reference]))
    coordinates["leaf_index"] = 2
    assert first.value["references"][0]["log_coordinates"]["leaf_index"] == 1
    assert p.build(_input(references=[dataclasses.replace(reference, log_coordinates={"log_id": "l", "leaf_index": 1, "inclusion_proof": [HEX_A]})])) == first


def test_build_requires_input_type():
    with pytest.raises(TypeError):
        p.build({"action_id": "x"})


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"model": p.Model(provider="p", model_id=" ")}, "model id must be non-empty"),
        ({"model": p.Model(provider=" ", model_id="m")}, "model provider must be non-empty"),
        ({"compute": p.ComputeAttestation(runtime=" ")}, "runtime must be non-empty"),
        ({"compute": p.ComputeAttestation(agent_output_digest="x")}, "agent output digest"),
        ({"chain": p.Chain(parent_capsule_id=HEX_A, relation="")}, "chain relation is required"),
        ({"chain": p.Chain(parent_capsule_id=HEX_A + "\n", relation="confirms")}, "64 lowercase hex"),
        ({"effect": p.Effect(type=" ", status="planned", irreversibility_class="two_way")}, "effect type"),
        ({"effect": p.Effect(type="t", status="planned", irreversibility_class="")}, "irreversibility"),
        ({"effect": p.Effect(type="t", status="planned", irreversibility_class="two_way", effect_attestation="gate_executed")}, "planned effect must not carry effect attestation"),
        ({"effect": p.Effect(type="t", status="dispatched", irreversibility_class="two_way", effect_attestation="gate_executed", response_digest=HEX_A)}, "dispatched effect must not carry"),
        ({"effect": p.Effect(type="t", status="dispatched", irreversibility_class="two_way")}, "dispatched effect requires"),
        ({"effect": p.Effect(type="t", status="confirmed", irreversibility_class="two_way", response_digest=HEX_A)}, "confirmed effect requires effect attestation"),
        ({"effect": p.Effect(type="t", status="failed", irreversibility_class="two_way")}, "failed effect requires"),
        ({"effect": p.Effect(type="t", status="planned", irreversibility_class="two_way", request_digest="X")}, "request digest"),
        ({"action_type": "decide", "disposition": p.Disposition(decision="", approver="policy")}, "decision is required"),
        ({"action_type": "decide", "disposition": p.Disposition(decision="accept", approver="policy", reason_digest="x")}, "reason digest"),
        ({"action_type": "decide", "disposition": p.Disposition(decision="accept", approver="policy", human_disposed=1)}, "must be a bool"),
        ({"provenance_mode": p.ProvenanceMode(mode="imported")}, "must be"),
        ({"provenance_mode": p.ProvenanceMode(mode="backfilled", source_ref=p.Reference("t", "SHA-256", HEX_A, citation_purpose="acted_on"))}, "must not carry citation"),
        ({"provenance_mode": p.ProvenanceMode(mode="backfilled", source_ref=p.Reference(" ", "SHA-256", HEX_A))}, "requires type, digest alg"),
        ({"provenance_mode": p.ProvenanceMode(mode="backfilled", source_ref=p.Reference("t", "SHA-256", HEX_A), source_asserted_at="a", import_batch="b", imported_at="c", time_rung="sworn")}, "time rung"),
        ({"references": [p.Reference("capsule", "SHA-256", HEX_A, log_coordinates={"x": 0.5})]}, "log coordinates"),
        ({"extensions": [p.Extension("note", b"")]}, "has no value"),
        ({"extensions": [p.Extension("note", b'{"a":')]}, "invalid JSON"),
        ({"extensions": [p.Extension("note", b'{"a": 9007199254740992}')]}, "interoperable range"),
        ({"extensions": [p.Extension("note", "[1]"), p.Extension("note", "[2]")]}, "more than once"),
        ({"action_id": "act/\ud800"}, "unpaired surrogate"),
    ],
)
def test_build_rejects(changes, message):
    with pytest.raises(ValueError, match=message):
        p.build(_input(**changes))


def test_build_accepts_extension_as_str_and_mapping_coordinates():
    from types import MappingProxyType

    built = p.build(
        _input(
            extensions=[p.Extension("note", '{"b": [1, true], "a": "x"}')],
            references=[p.Reference("capsule", "SHA-256", HEX_A, log_coordinates=MappingProxyType(COORDS))],
        )
    )
    assert built.value["note"] == {"a": "x", "b": [1, True]}
    assert built.value["references"][0]["log_coordinates"] == COORDS


# --- carry, received, composition -----------------------------------------


def test_received_digests_exact_bytes_without_parsing():
    artifact = b'{"b":1,  "a":2}'
    built = p.received(_input(), artifact, "machine-mandate")
    import hashlib

    digest = hashlib.sha256(artifact).hexdigest()
    compute = built.value["model_attestation"]["compute_attestation"]
    assert compute == {
        "carried_artifact": {"type": "machine-mandate", "digest_alg": "SHA-256", "digest": digest},
        "carried_input_digest": digest,
    }
    carried = p.carry(_input(), bytearray(artifact))
    assert carried.value["model_attestation"]["compute_attestation"]["carried_artifact"]["type"] == "foreign-artifact"


def test_received_rejects_text_and_bad_type():
    with pytest.raises(TypeError):
        p.received(_input(), "text", "t")
    with pytest.raises(ValueError):
        p.received(_input(), b"x", 5)


def test_slot_helpers_reject_other_values():
    with pytest.raises(TypeError):
        p.who({"capsule_id": HEX_A})


def test_composition_orders_slots_and_accepts_built_and_sealed_members():
    first = p.build(_input(action_id="a/1"))
    second = p.seal(p.SealInput(capsule=_input(action_id="a/2"), identity=_identity()))
    built = p.build_composition(_input(action_id="c/1"), p.audit(second), p.who(first))
    members = built.value["model_attestation"]["compute_attestation"]["composed_members"]
    assert [m["slot"] for m in members] == ["who", "audit"]
    assert [m["digest"] for m in members] == [first.capsule_id, second.capsule_id]


def test_composition_rejects_bad_members():
    member = p.build(_input())
    good = p.who(member)
    with pytest.raises(ValueError, match="member 0 has invalid slot"):
        p.build_composition(_input(), [good])
    with pytest.raises(ValueError, match="member 0 has invalid slot"):
        p.build_composition(_input(), p.SlotMember(slot="why", capsule_id=member.capsule_id, json=member.json))
    with pytest.raises(ValueError, match="malformed Capsule ID"):
        p.build_composition(_input(), p.SlotMember(slot="who", capsule_id="x", json=member.json))
    with pytest.raises(ValueError, match="not a matching verified"):
        p.build_composition(_input(), p.SlotMember(slot="who", capsule_id=HEX_A, json=member.json))
    with pytest.raises(ValueError, match="not a matching verified"):
        p.build_composition(_input(), p.SlotMember(slot="who", capsule_id=member.capsule_id, json=b"{"))
    authored = _input(compute=p.ComputeAttestation(agent_output_digest=HEX_A))
    with pytest.raises(ValueError, match="must not include agent input or output digest"):
        p.build_composition(authored, good)


# --- signing identities, sign, seal ----------------------------------------


def test_signing_identity_forms_agree():
    key = Ed25519PrivateKey.from_private_bytes(SEED)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    for form in (SEED, bytearray(SEED), SEED + public, key):
        assert p.new_ed25519_signing_identity(form).public_key == public
    assert public.hex() in repr(_identity())
    assert p.generate_signing_identity().public_key != p.generate_signing_identity().public_key


def test_signing_identity_rejects():
    with pytest.raises(ValueError, match="wrong size"):
        p.new_ed25519_signing_identity(b"\x00" * 31)
    with pytest.raises(ValueError, match="public half"):
        p.new_ed25519_signing_identity(SEED + b"\x00" * 32)
    with pytest.raises(TypeError):
        p.new_ed25519_signing_identity("seed")
    with pytest.raises(TypeError):
        p.SigningIdentity(SEED)
    identity = _identity()
    with pytest.raises(AttributeError):
        identity._public_key = b"x"


def test_sign_is_independent_of_signer_and_verifies():
    built = p.build(_input())
    one = p.sign(built, _identity())
    two = p.sign(built, p.generate_signing_identity())
    assert one != two
    assert p.sign(built, _identity()) == one  # Ed25519 is deterministic
    assert p.verify_envelope(built.capsule_id, one).public_key == _identity().public_key
    assert p.verify_envelope(built.capsule_id, two).ok


def test_sign_rejects():
    built = p.build(_input())
    with pytest.raises(ValueError, match="signing identity"):
        p.sign(built, SEED)
    with pytest.raises(TypeError):
        p.sign(built.json, _identity())
    with pytest.raises(ValueError, match="64 lowercase"):
        p.sign(dataclasses.replace(built, capsule_id="x"), _identity())
    with pytest.raises(ValueError, match="does not match"):
        p.sign(dataclasses.replace(built, capsule_id=HEX_A), _identity())
    with pytest.raises(ValueError, match="does not match"):
        p.sign(dataclasses.replace(built, json=b"{}"), _identity())


def test_seal_commits_payload_output_and_explicit_null():
    identity = _identity()
    sealed = p.seal(p.SealInput(capsule=_input(), identity=identity, payload={"a": 1}, agent_output=p.JSON_NULL))
    compute = p.decode_payload(sealed.payload)["model_attestation"]["compute_attestation"]
    assert compute == {"agent_input_digest": p.digest_json({"a": 1}), "agent_output_digest": p.digest_json(None)}
    assert p.verify_envelope(sealed.capsule_id, sealed.envelope).ok
    bare = p.seal(p.SealInput(capsule=_input(), identity=identity))
    assert "model_attestation" not in p.decode_payload(bare.payload)
    assert bare.payload == p.build(_input()).json
    assert repr(p.JSON_NULL) == "JSON_NULL"


def test_seal_composition_with_runtime():
    member = p.build(_input())
    sealed = p.seal(p.SealInput(capsule=_input(action_id="c/1"), identity=_identity(), members=[p.did(member)], runtime="r@1"))
    compute = p.decode_payload(sealed.payload)["model_attestation"]["compute_attestation"]
    assert compute["runtime"] == "r@1"
    assert compute["composed_members"][0]["digest"] == member.capsule_id


def test_seal_rejects():
    identity = _identity()
    with pytest.raises(TypeError):
        p.seal(_input())
    with pytest.raises(ValueError, match="at least one slot member"):
        p.seal(p.SealInput(capsule=_input(), identity=identity, members=[]))
    with pytest.raises(ValueError, match="model and compute metadata"):
        p.seal(p.SealInput(capsule=_input(compute=p.ComputeAttestation(runtime="r")), identity=identity))
    with pytest.raises(ValueError, match="signing identity"):
        p.seal(p.SealInput(capsule=_input(), identity=None))
    with pytest.raises(ValueError, match="digest agent input"):
        p.seal(p.SealInput(capsule=_input(), identity=identity, payload=object()))


# --- verification ----------------------------------------------------------


def test_verify_capsule_accepts_released_v04_capsules():
    stored = (INTEROP_V04 / "authored" / "capsule.stored.json").read_bytes()
    assert p.decode_payload(stored)["spec_version"] == "draft-mih-scitt-agent-action-capsule-04"
    assert p.verify_capsule(stored).ok


def test_verify_capsule_rejects():
    value = dict(p.build(_input()).value)
    with pytest.raises(ValueError, match="unsupported Capsule profile"):
        p.verify_capsule(json.dumps({**value, "format_version": "2"}))
    with pytest.raises(ValueError, match="unsupported Capsule profile"):
        p.verify_capsule(json.dumps({**value, "canonicalization_id": "jcs-n"}))
    with pytest.raises(p.Class1Error) as caught:
        p.verify_capsule(json.dumps({**value, "operator": "someone-else"}))
    assert not caught.value.result.ok
    assert "capsule_id" in str(caught.value)


def test_verify_envelope_rejects():
    built = p.build(_input())
    envelope = p.sign(built, _identity())
    with pytest.raises(p.EnvelopeVerificationError) as caught:
        p.verify_envelope(HEX_A, envelope)
    assert caught.value.result.findings
    with pytest.raises(p.EnvelopeVerificationError):
        p.verify_envelope(built.capsule_id, envelope[:-1] + bytes([envelope[-1] ^ 1]))


def test_deep_json_within_the_limit_is_a_value_error_not_a_recursion_error():
    """agent-action-capsule's canonicalizer recurses per nesting level, so
    nesting that passes the depth check can still exhaust Python's recursion
    limit; that is reported as ValueError, like excessive depth."""
    value: list = []
    for _ in range(MAX_JSON_DEPTH - 2):
        value = [value]
    with pytest.raises(ValueError):
        p.digest_json(value)
    with pytest.raises(ValueError):
        p.build(_input(extensions=(p.Extension("deep", json.dumps(value)),)))


def test_whole_number_floats_digest_as_integers_like_go():
    assert p.digest_json({"x": 1.0, "y": [0.0, -0.0]}) == p.digest_json({"x": 1, "y": [0, 0]})
    with pytest.raises(ValueError):
        p.digest_json({"x": 1.5})
    with pytest.raises(ValueError):
        p.digest_json(float("nan"))
    reference = p.Reference(type="capsule", digest_alg="SHA-256", digest=HEX_A, log_coordinates={**COORDS, "leaf_index": 3.0})
    built = p.build(_input(references=(reference,)))
    assert built.value["references"][0]["log_coordinates"]["leaf_index"] == 3


def test_timestamp_boundaries_are_value_errors():
    for bad in (
        datetime(1, 1, 1, tzinfo=timezone.utc),
        "0001-01-01T00:00:00Z",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        datetime(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-1))),
    ):
        with pytest.raises(ValueError):
            p.format_timestamp(bad)
    assert p.format_timestamp("0001-01-01T00:00:01Z") == "0001-01-01T00:00:01Z"
    # Go tests IsZero before truncating to microseconds.
    assert p.format_timestamp("0001-01-01T00:00:00.000000001Z") == "0001-01-01T00:00:00Z"
