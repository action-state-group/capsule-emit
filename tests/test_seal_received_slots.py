# SPDX-License-Identifier: Apache-2.0
"""Tests for the seal() / received() / who() / can() / did() / audit() Layer-0
developer surface.

Surface of record: ``_work/dev-surface-v4-2026-08-24.md`` §1/§3 (frozen);
clean break removing the v3 ``compose()``/``carry()`` verbs from the public
surface: ``_work/v4-complete-050-and-single-witness-task-2026-08-27.md`` (A/B).
"""
from __future__ import annotations

import hashlib
import importlib
import subprocess
import sys
import uuid
from unittest import mock

import pytest

import capsule_emit
import capsule_emit.surface as surface_module
from capsule_emit import Capsule, audit, can, did, emit, received, seal, who
from capsule_emit.core import _emit_capsule
from capsule_emit.verification import verify_capsule as verify

# agent_action_capsule/__init__.py does `from .emit import ... emit`, which
# rebinds the package's `emit` ATTRIBUTE to the re-exported function — so both
# plain attribute access (`agent_action_capsule.emit`) and `import
# agent_action_capsule.emit as x` (which also resolves via that attribute)
# return the function, not the submodule, and a dotted-string mock.patch()
# target built on either is unreliable (confirmed to resolve differently
# between Python 3.9 and 3.13's unittest.mock). importlib.import_module()
# reads sys.modules directly, sidestepping the shadowed attribute.
_base_emit_module = importlib.import_module("agent_action_capsule.emit")


def test_seal_returns_capsule_and_verifies_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    capsule = seal({"vendor": "Frobozz Supply", "total": "1240.19"}, operator="acme-co", anchor=False)
    assert isinstance(capsule, Capsule)
    result = verify(capsule.capsule)
    assert result.ok, result.findings


def test_seal_mint_result_is_a_capsule_not_a_receipt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    capsule = seal({"a": 1}, anchor=False)
    assert isinstance(capsule, capsule_emit.Capsule)
    assert not hasattr(capsule_emit, "Receipt")
    # vocabulary discipline: no docstring on this surface teaches `receipt = seal(...)`
    assert "receipt = seal(" not in (surface_module.__doc__ or "")
    assert "receipt = seal(" not in (seal.__doc__ or "")


def test_received_standalone_dispatch_is_a_carry(tmp_path, monkeypatch):
    # received(bytes, type=...) called directly performs the carry now and
    # returns a Capsule — the foreign artifact is recorded under its own
    # declared registered type.
    monkeypatch.chdir(tmp_path)
    mandate_jws = b'{"iss": "acme-mandates", "sub": "po-agent@v1"}'
    effect = received(mandate_jws, type="machine-mandate", anchor=False)
    assert isinstance(effect, Capsule)

    carried_ref = effect.capsule["model_attestation"]["compute_attestation"]["carried_artifact"]
    assert carried_ref["type"] == "machine-mandate"
    assert carried_ref["digest"] == hashlib.sha256(mandate_jws).hexdigest()
    assert carried_ref["digest_alg"] == "SHA-256"

    result = verify(effect.capsule)
    assert result.ok, result.findings


def test_received_has_two_distinct_addresses(tmp_path, monkeypatch):
    # dev-surface v4 §1: "A carry receipt has its own capsule_id (over the
    # carried bytes as payload) while preserving the foreign record's own
    # digest inside — two addresses, two facts: theirs identifies their
    # record, yours identifies your act of holding it."
    monkeypatch.chdir(tmp_path)
    mandate_jws = b'{"iss": "acme-mandates", "sub": "po-agent@v1"}'
    effect = received(mandate_jws, type="machine-mandate", anchor=False)

    compute_att = effect.capsule["model_attestation"]["compute_attestation"]
    carried_digest = hashlib.sha256(mandate_jws).hexdigest()

    # "theirs" — the foreign record's own address, untouched.
    assert compute_att["carried_artifact"]["digest"] == carried_digest
    # "yours" — this capsule's own payload commitment, in the same
    # payload-commitment slot agent_input_digest/agent_output_digest occupy
    # for seal(), but under its own name (a carried artifact is opaque bytes,
    # never JCS-reinterpreted, so it can't share agent_input_digest's
    # SHA-256(JCS(...)) contract).
    assert compute_att["carried_input_digest"] == carried_digest
    assert "agent_input_digest" not in compute_att

    # Two addresses, two facts: capsule_id (the whole-envelope commitment to
    # this act of holding) is never equal to the bare carried-bytes digest.
    assert effect.capsule_id != carried_digest

    # capsule_id is sensitive to the carried bytes as payload: carrying
    # different foreign bytes under an otherwise-identical call changes it.
    other = received(b'{"iss": "acme-mandates-2"}', type="machine-mandate", anchor=False)
    assert other.capsule_id != effect.capsule_id

    result = verify(effect.capsule)
    assert result.ok, result.findings


def test_slot_form_composes_the_frozen_surface_canonical_example(tmp_path, monkeypatch):
    # dev-surface v4 §3's own example, verbatim shape:
    #   seal(who(delegation_record), can(received(mandate_jws, type=...)), did(payment_action))
    monkeypatch.chdir(tmp_path)
    delegation_record = {"delegate": "po-agent@v1", "scope": "write_order"}
    mandate_jws = b'{"iss": "acme-mandates", "sub": "po-agent@v1"}'
    payment_action = {"vendor": "Frobozz Supply", "total": "1240.19"}

    mandate = received(mandate_jws, type="machine-mandate", anchor=False)
    action = seal(who(delegation_record), can(mandate), did(payment_action), anchor=False)

    assert isinstance(action, Capsule)
    members = action.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    by_slot = {m["slot"]: m for m in members}
    assert set(by_slot) == {"who", "can", "did"}
    # Layer 0: members referenced by CPB typed digest ref, slot-annotated — no log coordinates.
    assert all(set(m) == {"type", "digest_alg", "digest", "slot"} for m in members)
    # can() references the mandate capsule already produced by received() —
    # not a re-mint (O8: slot-form and carry-form are byte-identical).
    assert by_slot["can"]["digest"] == mandate.capsule_id

    result = verify(action.capsule)
    assert result.ok, result.findings


def test_slot_form_mints_a_fresh_capsule_for_a_raw_payload(tmp_path, monkeypatch):
    # who()/can()/did()/audit() wrapping a raw payload (not a Capsule) mints
    # it as its own member capsule, under the slot name as the action — this
    # is what makes seal(who(dict), did(dict)) work without pre-sealing
    # every member by hand.
    monkeypatch.chdir(tmp_path)
    action = seal(who({"delegate": "po-agent@v1"}), did({"vendor": "Frobozz"}), anchor=False)
    members = action.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    assert len(members) == 2

    ledger_records = capsule_emit.read_ledger("ledger.jsonl")
    # Three ledger entries: the two freshly-minted members, plus the composition.
    assert len(ledger_records) == 3
    who_member_id = next(m["digest"] for m in members if m["slot"] == "who")
    who_record = next(r for r in ledger_records if r["capsule_id"] == who_member_id)
    assert who_record["action_id"].startswith("who/")


def test_slot_form_can_wrapping_a_received_capsule_is_byte_identical_to_standalone(tmp_path, monkeypatch):
    # O8 acceptance: slot-form and carry-form produce byte-identical records.
    # can(received(...)) must reference the EXACT same capsule bytes a
    # standalone received() call for the identical Capsule object would —
    # never a re-mint (which would also double-append to the ledger).
    monkeypatch.chdir(tmp_path)
    mandate_jws = b'{"iss": "acme-mandates", "sub": "po-agent@v1"}'
    mandate = received(mandate_jws, type="machine-mandate", anchor=False)

    ledger_path = tmp_path / "ledger.jsonl"
    lines_after_receive = ledger_path.read_text().splitlines()

    action = seal(can(mandate), did({"vendor": "Frobozz"}), anchor=False)

    lines_after_compose = ledger_path.read_text().splitlines()
    # Exactly one new entry (the composition) — can(mandate) did not re-append it.
    assert len(lines_after_compose) == len(lines_after_receive) + 2  # did() member + composition

    members = action.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    can_ref = next(m for m in members if m["slot"] == "can")
    assert can_ref["digest"] == mandate.capsule_id
    assert can_ref["type"] == "capsule"


def test_slot_composition_order_is_canonicalized_regardless_of_caller_arg_order(tmp_path, monkeypatch):
    # dev-surface v4 §3's frozen composition order is who -> can -> did ->
    # audit. Two seal() calls citing the EXACT same three already-sealed
    # members, differing only in the order the caller wrote the positional
    # slot wrappers, must produce byte-identical composition capsules —
    # composed_members is never allowed to depend on call-argument order.
    # (Pre-fix: composed_members preserved call order verbatim, so this
    # diverged on capsule_id.)
    monkeypatch.chdir(tmp_path)
    delegation_record = {"delegate": "po-agent@v1", "scope": "write_order"}
    mandate_jws = b'{"iss": "acme-mandates", "sub": "po-agent@v1"}'
    payment_action = {"vendor": "Frobozz Supply", "total": "1240.19"}

    who_cap = seal(delegation_record, action="who", anchor=False)
    can_cap = received(mandate_jws, type="machine-mandate", anchor=False)
    did_cap = seal(payment_action, action="did", anchor=False)

    fixed_uuid = uuid.UUID(int=99)
    fixed_ts = "2026-08-27T00:00:00Z"

    with (
        mock.patch.object(_base_emit_module.uuid, "uuid4", return_value=fixed_uuid),
        mock.patch.object(_base_emit_module, "_utc_now", return_value=fixed_ts),
    ):
        canonical = seal(who(who_cap), can(can_cap), did(did_cap), anchor=False, ledger=tmp_path / "canonical.jsonl")

    with (
        mock.patch.object(_base_emit_module.uuid, "uuid4", return_value=fixed_uuid),
        mock.patch.object(_base_emit_module, "_utc_now", return_value=fixed_ts),
    ):
        non_canonical = seal(
            did(did_cap), can(can_cap), who(who_cap), anchor=False, ledger=tmp_path / "non_canonical.jsonl"
        )

    # capsule_id is signer-independent by construction (core.py: computed
    # before signing, draft-04 reversal) — the two calls use distinct default
    # signers (separate ledgers), so capsule_id equality, not full capsule
    # dict equality, is the right byte-identical check here.
    assert canonical.capsule_id == non_canonical.capsule_id

    canonical_members = canonical.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    non_canonical_members = non_canonical.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    assert [m["slot"] for m in canonical_members] == ["who", "can", "did"]
    assert [m["slot"] for m in non_canonical_members] == ["who", "can", "did"]


def test_seal_refuses_bare_bytes_naming_received(tmp_path, monkeypatch):
    # Dispatch-ambiguity refusal: raw bytes/bytearray handed straight to
    # seal() are never guessed at — the error names received() as the fix.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match=r"received\("):
        seal(b'{"provider_ack": "PO-9182"}', anchor=False)
    with pytest.raises(TypeError, match=r"received\("):
        seal(bytearray(b'{"provider_ack": "PO-9182"}'), anchor=False)


@pytest.mark.parametrize("wrapper", [who, can, did, audit])
def test_slot_wrappers_refuse_bare_bytes_naming_received(tmp_path, monkeypatch, wrapper):
    # Same dispatch-ambiguity refusal as seal() itself, for every slot
    # wrapper: bare foreign bytes are always refused, never guessed —
    # frozen surface §1's dispatch rule applies identically nested in a slot.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match=r"received\("):
        seal(wrapper(b'{"provider_ack": "PO-9182"}'), did({"x": 1}), anchor=False)


def test_seal_nested_received_is_byte_identical_and_does_not_double_append(tmp_path, monkeypatch):
    # Nested-in-wrapper dispatch: seal(received(bytes, type=...)) must
    # produce the identical capsule to calling received() standalone — not a
    # second, independently-computed one — and must not append twice.
    monkeypatch.chdir(tmp_path)
    mandate_jws = b'{"iss": "acme-mandates", "sub": "po-agent@v1"}'
    effect = received(mandate_jws, type="machine-mandate", anchor=False)

    ledger_path = tmp_path / "ledger.jsonl"
    lines_before = ledger_path.read_text().splitlines()

    wrapped = seal(effect)
    assert wrapped is effect  # byte-identical: literally the same capsule

    lines_after = ledger_path.read_text().splitlines()
    assert lines_after == lines_before  # seal() did not append a second entry

    result = verify(wrapped.capsule)
    assert result.ok, result.findings


def test_seal_still_refuses_bare_string_payload_that_is_a_capsule_look_alike(tmp_path, monkeypatch):
    # seal()'s pass-through is scoped to actual carried Capsules only — an
    # EmitResult without a carried_artifact (e.g. a plain seal()/composition
    # result) is not a recognized carry and is not silently special-cased.
    monkeypatch.chdir(tmp_path)
    plain = seal({"a": 1}, anchor=False)
    assert surface_module._carried_artifact_ref(plain) is None


def test_seal_with_no_arguments_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match="requires a payload"):
        seal(anchor=False)


def test_seal_refuses_mixing_a_plain_payload_with_slot_wrappers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match="cannot mix"):
        seal({"a": 1}, did({"b": 2}), anchor=False)


def test_seal_refuses_more_than_one_plain_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match="exactly one payload"):
        seal({"a": 1}, {"b": 2}, anchor=False)


def test_seal_slot_form_refuses_an_outer_action(tmp_path, monkeypatch):
    # action= is ambiguous for a multi-member call (which member's action?)
    # — refused, never guessed at.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match="action"):
        seal(did({"a": 1}), audit({"b": 2}), action="mint", anchor=False)


def test_private_compose_helper_requires_at_least_one_member(tmp_path, monkeypatch):
    # The v3 compose() verb's flat-bind body survives as the private helper
    # the slot-form calls (frozen surface §1/§9 clean break) — its own
    # invariants (at least one member; every member already a Capsule) still
    # hold, exercised directly since there is no longer a public zero-slot
    # entry point to reach them through.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        surface_module._compose([], anchor=False)
    with pytest.raises(TypeError):
        surface_module._compose([{"not": "a capsule"}], anchor=False)


def test_slot_membership_is_never_inferred(tmp_path, monkeypatch):
    # "The verb never infers membership" (frozen surface §3a) — a capsule
    # sealed earlier in the same process/ledger must never silently show up
    # as a composition member just because it exists; only what is
    # explicitly wrapped in a slot is ever a member. This is the guessing
    # mutant this test is built to catch: any implementation that scans the
    # ledger/log for "recent" or "related" capsules instead of taking only
    # the explicitly-passed slot wrappers would fail it.
    monkeypatch.chdir(tmp_path)
    bystander = seal({"unrelated": "capsule"}, anchor=False)
    action = seal(did({"vendor": "Frobozz"}), anchor=False)

    members = action.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    member_digests = {m["digest"] for m in members}
    assert bystander.capsule_id not in member_digests
    assert len(members) == 1


def test_import_discipline_noun_not_shadowed():
    # `import capsule_emit as capsule` would shadow the `capsule` variable the
    # canonical line (`capsule = seal(payload)`) assigns to. Guard: the
    # package exports no symbol literally named `capsule` for that mistake
    # to silently bind to.
    assert not hasattr(capsule_emit, "capsule")
    assert "capsule" not in capsule_emit.__all__
    assert "import capsule_emit as capsule" in (capsule_emit.__doc__ or "")


def test_compose_and_carry_are_not_public(tmp_path, monkeypatch):
    # Clean break, no deprecation period (frozen surface §1/§9, task B): the
    # v3 flat-bind verbs are gone from __all__ and from the module namespace
    # entirely — compose()'s body survives only as the private _compose()
    # helper the slot-form calls; carry()'s body was already received()'s.
    assert "compose" not in capsule_emit.__all__
    assert "carry" not in capsule_emit.__all__
    assert not hasattr(capsule_emit, "compose")
    assert not hasattr(capsule_emit, "carry")
    assert not hasattr(surface_module, "compose")
    assert not hasattr(surface_module, "carry")


def test_layer_0_imports_no_checkpoint_module():
    # A fresh interpreter import of capsule_emit must not pull in the opt-in
    # CLL/checkpoint layer as a side effect — Layer 0 works with nothing
    # configured.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, capsule_emit\n"
            "assert 'capsule_emit.checkpoint' not in sys.modules, sorted(sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_seal_is_a_thin_alias_of_the_internal_primitive(tmp_path, monkeypatch):
    # seal(payload) must be provably the same underlying _emit_capsule() call,
    # not a divergent reimplementation. Freeze the two non-deterministic
    # inputs _emit_capsule() doesn't let callers override (action_id's uuid4
    # suffix, timestamp) and compare the full output.
    monkeypatch.chdir(tmp_path)
    fixed_uuid = uuid.UUID(int=0)
    fixed_ts = "2026-01-01T00:00:00Z"
    # Same signing key for both calls -- the default key is scoped per ledger
    # path, and this test intentionally writes to two different ledger files,
    # which would otherwise sign with two different (and thus divergent)
    # auto-generated keys and break the "identical output" comparison below.
    kwargs = {
        "operator": "acme-co",
        "developer": "po-agent@v1",
        "anchor": False,
        "signing_key_path": tmp_path / "shared.signing_key.pem",
    }

    with (
        mock.patch.object(_base_emit_module.uuid, "uuid4", return_value=fixed_uuid),
        mock.patch.object(_base_emit_module, "_utc_now", return_value=fixed_ts),
    ):
        via_primitive = _emit_capsule("mint", agent_input={"x": 1}, ledger="via_primitive.jsonl", **kwargs)
    with (
        mock.patch.object(_base_emit_module.uuid, "uuid4", return_value=fixed_uuid),
        mock.patch.object(_base_emit_module, "_utc_now", return_value=fixed_ts),
    ):
        via_seal = seal({"x": 1}, action="mint", ledger="via_seal.jsonl", **kwargs)

    assert via_primitive.capsule_id == via_seal.capsule_id
    assert via_primitive.capsule == via_seal.capsule


def test_emit_is_a_removed_raising_stub():
    # Clean break (2026-08-22): emit() was renamed. It stays importable for
    # one release so callers get a clear error instead of an ImportError.
    with pytest.raises(RuntimeError, match="emit\\(\\) was renamed"):
        emit("mint", agent_input={"x": 1})


# --- O16-07 follow-up: Ethan's #97 review (PR #97 review 5012424851) ---


def test_seal_on_received_rejects_outer_ledger_and_signer_instead_of_dropping_them(tmp_path, monkeypatch):
    # HIGH (surface.py:137): seal(received(...), ledger=other, signing_key_path=key)
    # must not silently use the wrong ledger/signer -- it must raise and name
    # the fix (pass the option to received() instead).
    monkeypatch.chdir(tmp_path)
    effect = received(b'{"iss": "acme-mandates"}', type="machine-mandate", anchor=False)
    with pytest.raises(TypeError, match="ledger"):
        seal(effect, ledger=tmp_path / "other.jsonl")
    with pytest.raises(TypeError, match="signing_key_path"):
        seal(effect, signing_key_path=tmp_path / "other.signing_key.pem")


def test_seal_on_received_rejects_witness_false_rather_than_silently_dropping_it(tmp_path, monkeypatch):
    # HIGH (surface.py:137) -- the safety-critical case: an outer
    # witness=False on seal(received(...)) must never be silently dropped,
    # because a drop here means witnessing could arm despite an explicit
    # opt-out. The call must raise regardless of the value.
    monkeypatch.chdir(tmp_path)
    effect = received(b'{"iss": "acme-mandates"}', type="machine-mandate", anchor=False)
    with pytest.raises(TypeError, match="witness"):
        seal(effect, witness=False)


def test_seal_on_received_rejects_an_explicit_outer_action(tmp_path, monkeypatch):
    # The acceptance check calls out "incl. an explicit action" -- even
    # passing the same default value explicitly must raise, since seal()
    # cannot tell "explicit action='seal'" from "no action passed" any other
    # way once a sentinel default is in place.
    monkeypatch.chdir(tmp_path)
    effect = received(b'{"iss": "acme-mandates"}', type="machine-mandate", anchor=False)
    with pytest.raises(TypeError, match="action"):
        seal(effect, action="seal")


def test_seal_on_received_with_no_outer_options_still_passes_through_unchanged(tmp_path, monkeypatch):
    # Regression guard for the fix itself: the nested-dispatch pass-through
    # (test_seal_nested_received_is_byte_identical_and_does_not_double_append)
    # must keep working when genuinely no outer options are passed.
    monkeypatch.chdir(tmp_path)
    effect = received(b'{"iss": "acme-mandates"}', type="machine-mandate", anchor=False)
    assert seal(effect) is effect


def test_received_rejects_null_empty_and_non_string_type(tmp_path, monkeypatch):
    # MED (surface.py:209): type=None/""/123 must never mint a signed capsule
    # with a null/absent/wrong committed type.
    # [adv-run-2-fix-batch] A2: whitespace-only type ("   ") passed bool(type)
    # despite the docstring's stated intent (non-empty string) — included here
    # alongside the other invalid-type cases it belongs with.
    monkeypatch.chdir(tmp_path)
    for bad_type in (None, "", "   ", 123):
        with pytest.raises(TypeError, match="type"):
            received(b'{"iss": "acme-mandates"}', type=bad_type, anchor=False)


def test_received_rejects_int_and_int_list_instead_of_silently_recoercing_them(tmp_path, monkeypatch):
    # MED (surface.py:157): bytes(value) silently turns an int into a
    # NUL-padded buffer and a list of ints into a byte sequence -- neither is
    # the bytes the caller actually transmitted, so both must raise instead
    # of minting a capsule over reinterpreted content.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match="artifact_bytes"):
        received(7, type="machine-mandate", anchor=False)
    with pytest.raises(TypeError, match="artifact_bytes"):
        received([1, 2, 3], type="machine-mandate", anchor=False)
    with pytest.raises(TypeError, match="artifact_bytes"):
        received(None, type="machine-mandate", anchor=False)


def test_received_accepts_memoryview_as_a_valid_explicit_buffer(tmp_path, monkeypatch):
    # surface.py:157 -- unlike the bare-byte guard at surface.py:128,
    # received() legitimately accepts memoryview as one of the explicit
    # buffer types (str/bytes/bytearray/memoryview); it must round-trip to
    # the same digest as the underlying bytes.
    monkeypatch.chdir(tmp_path)
    raw = b'{"iss": "acme-mandates"}'
    effect = received(memoryview(raw), type="machine-mandate", anchor=False)
    carried_ref = effect.capsule["model_attestation"]["compute_attestation"]["carried_artifact"]
    assert carried_ref["digest"] == hashlib.sha256(raw).hexdigest()


def test_seal_refuses_bare_memoryview_naming_received(tmp_path, monkeypatch):
    # MED (surface.py:128): the bare-byte guard omitted memoryview, so a bare
    # memoryview handed to seal() fell through to the generic digest fallback
    # and produced a process-dependent, unreproducible digest instead of
    # being refused like bare bytes/bytearray.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError, match=r"received\("):
        seal(memoryview(b'{"provider_ack": "PO-9182"}'), anchor=False)
