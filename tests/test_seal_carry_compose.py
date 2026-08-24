# SPDX-License-Identifier: Apache-2.0
"""Tests for the seal() / carry() / compose() Layer-0 developer surface.

Surface of record: ``_work/api-verb-naming-design-2026-08-21.md`` §7.
"""
from __future__ import annotations

import hashlib
import importlib
import subprocess
import sys
import uuid
from unittest import mock

import pytest
from agent_action_capsule.verify import verify

import capsule_emit
import capsule_emit.surface as surface_module
from capsule_emit import Capsule, carry, compose, emit, seal
from capsule_emit.core import _emit_capsule

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


def test_carry_then_compose_round_trips_a_foreign_receipt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    auth = seal({"scope": "write_order"}, action="authorize", anchor=False)
    guard = seal({"checks": ["budget", "vendor_allowlist"]}, action="guard", anchor=False)
    act = seal({"vendor": "Frobozz Supply"}, action="write_order", anchor=False)

    # A foreign, already-signed artifact — as-transmitted bytes, not ours to re-canonicalize.
    foreign_receipt = b'{"provider_ack": "PO-9182", "status": "accepted"}'
    effect = carry(foreign_receipt, anchor=False)
    assert isinstance(effect, Capsule)
    carried_ref = effect.capsule["model_attestation"]["compute_attestation"]["carried_artifact"]
    assert carried_ref["digest"] == hashlib.sha256(foreign_receipt).hexdigest()
    assert carried_ref["digest_alg"] == "SHA-256"

    action = compose([auth, guard, act, effect], anchor=False)
    assert isinstance(action, Capsule)

    members = action.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    member_digests = {m["digest"] for m in members}
    assert member_digests == {auth.capsule_id, guard.capsule_id, act.capsule_id, effect.capsule_id}
    # Layer 0: members referenced by CPB typed digest ref alone — no log coordinates.
    assert all(set(m) == {"type", "digest_alg", "digest"} for m in members)

    result = verify(action.capsule)
    assert result.ok, result.findings


def test_carry_has_two_distinct_addresses(tmp_path, monkeypatch):
    # dev-surface v4 §1: "A carry receipt has its own capsule_id (over the
    # carried bytes as payload) while preserving the foreign record's own
    # digest inside — two addresses, two facts: theirs identifies their
    # record, yours identifies your act of holding it."
    monkeypatch.chdir(tmp_path)
    foreign_receipt = b'{"provider_ack": "PO-9182", "status": "accepted"}'
    effect = carry(foreign_receipt, anchor=False)

    compute_att = effect.capsule["model_attestation"]["compute_attestation"]
    carried_digest = hashlib.sha256(foreign_receipt).hexdigest()

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
    other = carry(b'{"provider_ack": "PO-9183", "status": "accepted"}', anchor=False)
    assert other.capsule_id != effect.capsule_id

    result = verify(effect.capsule)
    assert result.ok, result.findings


def test_compose_rejects_members_that_are_not_already_appended_capsules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError):
        compose([{"not": "a capsule"}], anchor=False)
    with pytest.raises(ValueError):
        compose([], anchor=False)


def test_import_discipline_noun_not_shadowed():
    # `import capsule_emit as capsule` would shadow the `capsule` variable the
    # canonical line (`capsule = seal(payload)`) assigns to. Guard: the
    # package exports no symbol literally named `capsule` for that mistake
    # to silently bind to.
    assert not hasattr(capsule_emit, "capsule")
    assert "capsule" not in capsule_emit.__all__
    assert "import capsule_emit as capsule" in (capsule_emit.__doc__ or "")


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
