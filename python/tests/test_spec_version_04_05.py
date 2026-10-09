# SPDX-License-Identifier: Apache-2.0
"""The -05 wire: capsule-emit stamps -05, and verifies -04, -05 and
unrecognized spec_version values alike.

Draft -05 ("Identity and parties"): a producer conforming to -05 emits
``spec_version`` -05; a verifier accepts -04 and -05 and never rejects solely
for either; ``spec_version`` selects no algorithm, and an unrecognized value
is informational, never by itself a rejection. The released -04 vectors stay
byte-identical; their -05 twins sit beside them in ``valid-v05/``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import capsule_emit.core as core_module
from capsule_emit import ACCEPTED_SPEC_VERSIONS, SPEC_VERSION, log, received, seal
from capsule_emit.core import _emit_capsule
from capsule_emit.holds import Action
from capsule_emit.holds.capsules import build_hold_reserve_capsule
from capsule_emit.ledger import read_ledger
from capsule_emit.signing import AuthorshipVerdict, verify_capsule_signature_tristate, verify_store_signed
from capsule_emit.verification import verify_capsule
from capsule_emit.verify_canonicalization import verify_canonicalization_id

SPEC_04 = "draft-mih-scitt-agent-action-capsule-04"
SPEC_05 = "draft-mih-scitt-agent-action-capsule-05"
UNRECOGNIZED = "draft-mih-scitt-agent-action-capsule-99"

VECTORS = Path(__file__).resolve().parents[2] / "test-vectors"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAPSULE_WITNESS", "off")


# -- producer: what capsule-emit stamps ------------------------------------


def test_spec_version_constants_are_05_and_accept_04_and_05():
    assert SPEC_VERSION == SPEC_05
    assert ACCEPTED_SPEC_VERSIONS == (SPEC_04, SPEC_05)


def test_spec_version_constants_match_the_pinned_aac_reference():
    # The AAC reference library at the pinned commit carries the same rule;
    # this fails if CI resolves an agent-action-capsule without the -05 wire.
    import agent_action_capsule

    assert SPEC_VERSION == agent_action_capsule.DEFAULT_SPEC_VERSION
    assert ACCEPTED_SPEC_VERSIONS == agent_action_capsule.ACCEPTED_SPEC_VERSIONS


def test_seal_received_and_log_stamp_spec_version_05(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    sealed = seal({"vendor": "Frobozz Supply", "total": "1240.19"}, anchor=False, witness=False, ledger=ledger)
    carried = received(b"mandate-bytes", type="machine-mandate", anchor=False, witness=False, ledger=ledger)
    entry = log(b"artifact-bytes", witness=False, ledger=ledger)
    assert sealed.capsule["spec_version"] == SPEC_05
    assert carried.capsule["spec_version"] == SPEC_05
    assert entry.capsule["spec_version"] == SPEC_05


def test_hold_capsules_stamp_spec_version_05():
    action = Action(verb="transfer_funds", operator="op", developer="dev1", action_class="money.transfer", amount_minor=100)
    capsule = build_hold_reserve_capsule(action=action, reserved_amount_minor=100, aggregate_before_minor=0)
    assert capsule["spec_version"] == SPEC_05


# -- verifier: -04 and -05 both verify -------------------------------------


def test_released_04_producer_envelope_capsule_and_its_05_twin_both_verify_and_differ_only_by_spec_version():
    v04 = _json(VECTORS / "producer-envelope" / "valid" / "capsule.json")
    v05 = _json(VECTORS / "producer-envelope" / "valid-v05" / "capsule.json")
    assert (v04["spec_version"], v05["spec_version"]) == (SPEC_04, SPEC_05)

    # spec_version participates in capsule_id (so the id and its signature differ) ...
    identity_fields = {"spec_version", "capsule_id", "signature"}
    assert {k: v for k, v in v04.items() if k not in identity_fields} == {
        k: v for k, v in v05.items() if k not in identity_fields
    }
    assert v04["capsule_id"] != v05["capsule_id"]

    # ... but selects no algorithm: each verifies the same way.
    for capsule in (v04, v05):
        result = verify_capsule(capsule)
        assert result.ok, result.findings
        assert result.capsule_id == capsule["capsule_id"]
        assert verify_canonicalization_id(capsule).ok
        assert verify_capsule_signature_tristate(capsule)[0] is AuthorshipVerdict.AUTHORED


def test_released_04_slot_composition_capsules_and_their_05_twins_all_verify():
    for case, spec in (("valid", SPEC_04), ("valid-v05", SPEC_05)):
        for name in ("carry_form.json", "slot_form_composition.json"):
            capsule = _json(VECTORS / "slot-composition" / case / name)
            assert capsule["spec_version"] == spec
            result = verify_capsule(capsule)
            assert result.ok, (case, name, result.findings)
            assert verify_capsule_signature_tristate(capsule)[0] is AuthorshipVerdict.AUTHORED


def test_a_chain_crossing_from_04_to_05_verifies_as_one_store(tmp_path, monkeypatch):
    # A ledger written before the bump (-04) and continued after it (-05),
    # the -05 record chaining to the -04 one: every record, and the link
    # between them, verifies from the ledger alone.
    ledger = tmp_path / "ledger.jsonl"
    kwargs = {"operator": "test-org", "developer": "agent@v1", "anchor": False, "witness": False, "ledger": ledger}
    monkeypatch.setattr(core_module, "SPEC_VERSION", SPEC_04)
    first = _emit_capsule(action="before_bump", **kwargs).capsule
    monkeypatch.setattr(core_module, "SPEC_VERSION", SPEC_05)
    second = _emit_capsule(action="after_bump", confirms=first["capsule_id"], **kwargs).capsule
    assert (first["spec_version"], second["spec_version"]) == (SPEC_04, SPEC_05)
    assert second["chain"]["parent_capsule_id"] == first["capsule_id"]

    records = read_ledger(ledger)
    assert [r["spec_version"] for r in records] == [SPEC_04, SPEC_05]
    results = verify_store_signed(records, require_signature=True)
    assert [r.ok for r in results] == [True, True], [r.findings for r in results]


# -- verifier: an unrecognized spec_version is not a rejection ---------------


def test_unrecognized_spec_version_is_not_a_rejection(tmp_path, monkeypatch):
    # Sealed through the real seal() path, signed, with a spec_version no
    # revision defines: it verifies exactly as a -05 record does, and no
    # finding is raised about spec_version.
    monkeypatch.setattr(core_module, "SPEC_VERSION", UNRECOGNIZED)
    ledger = tmp_path / "ledger.jsonl"
    sealed = seal({"a": "1"}, anchor=False, witness=False, ledger=ledger)
    capsule = sealed.capsule
    assert capsule["spec_version"] == UNRECOGNIZED
    assert capsule["spec_version"] not in ACCEPTED_SPEC_VERSIONS

    result = verify_capsule(capsule)
    assert result.ok, result.findings
    assert not [f for f in result.findings if "spec_version" in f.code or "spec_version" in (f.detail or "")]
    assert verify_canonicalization_id(capsule).ok
    assert verify_capsule_signature_tristate(capsule)[0] is AuthorshipVerdict.AUTHORED
    [store_result] = verify_store_signed([capsule], require_signature=True)
    assert store_result.ok, store_result.findings


# -- released vectors stay byte-identical -----------------------------------


@pytest.mark.parametrize("corpus", ["producer-envelope", "slot-composition"])
def test_every_vector_file_matches_its_sha256sums_line(corpus):
    # The released valid/ lines are unchanged since v0.5.0; the valid-v05/
    # lines are new. Every listed file must hash to its line.
    root = VECTORS / corpus
    lines = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    listed = {}
    for line in lines:
        digest, rel = line.split("  ", 1)
        listed[rel] = digest
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == digest, rel
    assert {p.split("/", 1)[0] for p in listed} == {"valid", "valid-v05"}
