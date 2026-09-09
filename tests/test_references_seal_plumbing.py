# SPDX-License-Identifier: Apache-2.0
"""Tests for the references= plumbing through seal() → _emit_capsule() → AAC emit.

Spec: draft-04 §5.5.5 references[] — cross-record citations outside a Capsule's
own chain scope. Tests confirm:

1. seal(payload, references=...) threads references to AAC BEFORE id/sign/persist.
2. Payload-level content does NOT produce top-level Capsule references[].
3. Slot-composition: references= on seal(who(...), ...) lands on the COMPOSITION
   capsule only, NOT on individually-minted member capsules.
4. references=None (default) omits the key; references=() emits [].
5. The resulting capsule verifies offline.
"""
from __future__ import annotations

from agent_action_capsule.contracts import ReferenceEntry

from capsule_emit import ReferenceEntry as CapsuleEmitReferenceEntry
from capsule_emit import can, did, received, seal, who
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# 1. Basic plumbing: references= threads to AAC before id/sign/persist
# ---------------------------------------------------------------------------


def test_seal_references_present_in_capsule_dict(tmp_path, monkeypatch):
    """references= on seal() appears at the top level of the capsule dict,
    committed to capsule_id before signing and persistence."""
    monkeypatch.chdir(tmp_path)
    ref = ReferenceEntry(
        type="agent-action-capsule",
        digest_alg="SHA-256",
        digest="a" * 64,
    )
    capsule = seal({"vendor": "Acme", "total": "100.00"}, references=(ref,), anchor=False)
    assert "references" in capsule.capsule
    refs = capsule.capsule["references"]
    assert isinstance(refs, list)
    assert len(refs) == 1
    assert refs[0]["type"] == "agent-action-capsule"
    assert refs[0]["digest_alg"] == "SHA-256"
    assert refs[0]["digest"] == "a" * 64


def test_seal_references_committed_before_capsule_id(tmp_path, monkeypatch):
    """The capsule_id must differ when references differ — proving references
    are committed to the id BEFORE it is computed (not injected post-hoc)."""
    monkeypatch.chdir(tmp_path)
    ref_a = ReferenceEntry(
        type="agent-action-capsule", digest_alg="SHA-256", digest="a" * 64
    )
    ref_b = ReferenceEntry(
        type="agent-action-capsule", digest_alg="SHA-256", digest="b" * 64
    )
    capsule_with_a = seal(
        {"x": 1}, references=(ref_a,), anchor=False, ledger=tmp_path / "a.jsonl"
    )
    capsule_with_b = seal(
        {"x": 1}, references=(ref_b,), anchor=False, ledger=tmp_path / "b.jsonl"
    )
    capsule_no_ref = seal({"x": 1}, anchor=False, ledger=tmp_path / "c.jsonl")

    # All three capsule_ids must differ — references are committed to the id.
    assert capsule_with_a.capsule_id != capsule_with_b.capsule_id
    assert capsule_with_a.capsule_id != capsule_no_ref.capsule_id
    assert capsule_with_b.capsule_id != capsule_no_ref.capsule_id


def test_seal_with_references_verifies_offline(tmp_path, monkeypatch):
    """A capsule with references= must verify correctly — signature covers
    the capsule_id which already commits references[]."""
    monkeypatch.chdir(tmp_path)
    ref = ReferenceEntry(
        type="agent-action-capsule", digest_alg="SHA-256", digest="c" * 64
    )
    capsule = seal({"item": "invoice"}, references=(ref,), anchor=False)
    result = verify(capsule.capsule)
    assert result.ok, result.findings


def test_seal_references_none_omits_key(tmp_path, monkeypatch):
    """The default (references=None) must NOT add a references key to the capsule."""
    monkeypatch.chdir(tmp_path)
    capsule = seal({"a": 1}, anchor=False)
    assert "references" not in capsule.capsule


def test_seal_references_empty_tuple_emits_empty_list(tmp_path, monkeypatch):
    """references=() emits references: [] in the capsule dict — distinct from None."""
    monkeypatch.chdir(tmp_path)
    capsule = seal({"a": 1}, references=(), anchor=False)
    assert "references" in capsule.capsule
    assert capsule.capsule["references"] == []


def test_seal_references_empty_and_none_produce_different_capsule_ids(tmp_path, monkeypatch):
    """references=None vs references=() are semantically equivalent ('no citation')
    but committed as distinct bytes — capsule_ids diverge."""
    monkeypatch.chdir(tmp_path)
    with_none = seal({"a": 1}, anchor=False, ledger=tmp_path / "none.jsonl")
    with_empty = seal({"a": 1}, references=(), anchor=False, ledger=tmp_path / "empty.jsonl")
    assert with_none.capsule_id != with_empty.capsule_id


# ---------------------------------------------------------------------------
# 2. Payload content does NOT create top-level references[]
# ---------------------------------------------------------------------------


def test_payload_with_references_key_does_not_leak_to_capsule_references(tmp_path, monkeypatch):
    """A payload dict that happens to contain a 'references' key MUST NOT
    produce a top-level Capsule references[] — only an explicit references=
    kwarg on seal() does that."""
    monkeypatch.chdir(tmp_path)
    payload_with_refs = {
        "action": "write_po",
        "references": [{"some": "data"}],
    }
    capsule = seal(payload_with_refs, anchor=False)
    # The payload is committed as agent_input_digest — its content never
    # promotes to top-level Capsule references[].
    assert "references" not in capsule.capsule


def test_payload_references_key_does_not_leak_with_slot_composition(tmp_path, monkeypatch):
    """Slot-member payloads that contain a 'references' key must not leak
    into top-level Capsule references[] on either the member or composition capsule."""
    monkeypatch.chdir(tmp_path)
    payload_a = {"action": "delegate", "references": ["some-id"]}
    payload_b = {"action": "execute"}
    composition = seal(who(payload_a), did(payload_b), anchor=False)

    # The composition capsule must not have references[].
    assert "references" not in composition.capsule

    # Member capsules also must not have references[].
    from capsule_emit import read_ledger
    records = read_ledger("ledger.jsonl")
    for record in records:
        if record.get("capsule_id") != composition.capsule_id:
            assert "references" not in record, (
                f"Member capsule {record.get('capsule_id')!r} leaked payload references "
                "into top-level Capsule references[]"
            )


# ---------------------------------------------------------------------------
# 3. Slot-composition: references= lands on the COMPOSITION capsule only
# ---------------------------------------------------------------------------


def test_slot_composition_references_land_on_composition_not_members(tmp_path, monkeypatch):
    """seal(who(...), can(...), did(...), references=...) must put references[]
    on the COMPOSITION capsule only — individually-minted member capsules get
    no references[]."""
    monkeypatch.chdir(tmp_path)
    ref = ReferenceEntry(
        type="agent-action-capsule", digest_alg="SHA-256", digest="d" * 64,
        citation_purpose="corroborates",
    )
    composition = seal(
        who({"delegate": "po-agent@v1"}),
        did({"vendor": "Frobozz", "total": "500.00"}),
        references=(ref,),
        anchor=False,
    )

    # Composition capsule carries references[].
    assert "references" in composition.capsule
    assert len(composition.capsule["references"]) == 1
    assert composition.capsule["references"][0]["digest"] == "d" * 64

    # Member capsules must NOT carry references[].
    from capsule_emit import read_ledger
    records = read_ledger("ledger.jsonl")
    member_ids = {
        m["digest"]
        for m in composition.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    }
    for record in records:
        if record.get("capsule_id") in member_ids:
            assert "references" not in record, (
                f"Member capsule {record.get('capsule_id')!r} incorrectly received "
                "references[] that should be on the composition capsule only"
            )


def test_slot_composition_references_committed_to_composition_capsule_id(tmp_path, monkeypatch):
    """references= on the slot-form changes the COMPOSITION capsule_id, proving
    they are committed before id computation on the right capsule."""
    monkeypatch.chdir(tmp_path)
    ref = ReferenceEntry(
        type="agent-action-capsule", digest_alg="SHA-256", digest="e" * 64
    )

    # First: composition without references.
    import importlib
    import uuid
    from unittest import mock
    _base_emit_module = importlib.import_module("agent_action_capsule.emit")
    fixed_uuid = uuid.UUID(int=42)
    fixed_ts = "2026-09-08T00:00:00Z"
    kwargs = {"anchor": False, "signing_key_path": tmp_path / "shared.pem"}

    with (
        mock.patch.object(_base_emit_module.uuid, "uuid4", return_value=fixed_uuid),
        mock.patch.object(_base_emit_module, "_utc_now", return_value=fixed_ts),
    ):
        no_ref = seal(
            who({"d": "po-agent@v1"}), did({"v": "Frobozz"}),
            ledger=tmp_path / "no_ref.jsonl", **kwargs,
        )

    with (
        mock.patch.object(_base_emit_module.uuid, "uuid4", return_value=fixed_uuid),
        mock.patch.object(_base_emit_module, "_utc_now", return_value=fixed_ts),
    ):
        with_ref = seal(
            who({"d": "po-agent@v1"}), did({"v": "Frobozz"}),
            references=(ref,), ledger=tmp_path / "with_ref.jsonl", **kwargs,
        )

    # The composition capsule_ids must differ — references are in the preimage.
    assert no_ref.capsule_id != with_ref.capsule_id


def test_slot_composition_already_carried_member_not_affected_by_references(tmp_path, monkeypatch):
    """An already-carried Capsule passed as a slot member must be referenced
    unchanged — adding references= to the composition must not mutate or
    re-seal the already-produced member capsule."""
    monkeypatch.chdir(tmp_path)
    ref = ReferenceEntry(
        type="agent-action-capsule", digest_alg="SHA-256", digest="f" * 64
    )
    mandate = received(b'{"iss": "acme-mandates"}', type="machine-mandate", anchor=False)
    original_id = mandate.capsule_id

    composition = seal(
        can(mandate), did({"vendor": "Acme"}),
        references=(ref,), anchor=False,
    )

    # The can() member capsule_id must be unchanged.
    members = composition.capsule["model_attestation"]["compute_attestation"]["composed_members"]
    can_ref = next(m for m in members if m["slot"] == "can")
    assert can_ref["digest"] == original_id

    # The composition capsule has references[].
    assert "references" in composition.capsule

    # The mandate capsule itself (already in the ledger) has no references[].
    assert "references" not in mandate.capsule


# ---------------------------------------------------------------------------
# 4. ReferenceEntry is importable from the top-level package
# ---------------------------------------------------------------------------


def test_reference_entry_importable_from_capsule_emit():
    """ReferenceEntry must be importable from capsule_emit directly — callers
    should not need to import from agent_action_capsule.contracts."""
    assert CapsuleEmitReferenceEntry is ReferenceEntry


def test_reference_entry_in_capsule_emit_all():
    import capsule_emit
    assert "ReferenceEntry" in capsule_emit.__all__


# ---------------------------------------------------------------------------
# 5. citation_purpose and multiple references
# ---------------------------------------------------------------------------


def test_seal_multiple_references_with_citation_purpose(tmp_path, monkeypatch):
    """Multiple ReferenceEntry values with citation_purpose all appear in
    the capsule dict and the capsule verifies."""
    monkeypatch.chdir(tmp_path)
    refs = (
        ReferenceEntry(
            type="agent-action-capsule", digest_alg="SHA-256", digest="1" * 64,
            citation_purpose="corroborates",
        ),
        ReferenceEntry(
            type="agent-action-capsule", digest_alg="SHA-256", digest="2" * 64,
            citation_purpose="extends",
        ),
    )
    capsule = seal({"action": "verify"}, references=refs, anchor=False)

    assert len(capsule.capsule["references"]) == 2
    purposes = {r.get("citation_purpose") for r in capsule.capsule["references"]}
    assert "corroborates" in purposes
    assert "extends" in purposes

    result = verify(capsule.capsule)
    assert result.ok, result.findings
