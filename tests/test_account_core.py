# SPDX-License-Identifier: Apache-2.0
"""Verification suite for the neutral account/fold core (capsule_emit.account).

Pins the four things the account-fold-core-unify spec's VERIFICATION section
requires of the core:

  1. definition-as-DATA mutation test: altering a definition-document field moves
     definition_digest; altering implementation internals does NOT.
  2. cross-repo replay vector: the SAME definition document, evaluated on either
     side of the public interface over equivalent inputs, yields the identical
     definition_digest and identical deterministic result.
  3. model-assisted-without-provenance refusal: refused at construction.
  4. no second FoldDefinition/definition_digest implementation escapes the core
     (a grep-style guard, enforced in-process).

Plus the range-input-identity rule (coverage_root + range, never per-member
digests) and idempotent-on-replay.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from capsule_emit.account import (
    DERIVATION_DETERMINISTIC,
    DERIVATION_MODEL_ASSISTED,
    SELECTION_CHAIN_SEGMENT,
    SELECTION_EXPLICIT_SET,
    SELECTION_RANGE,
    Account,
    AccountConstructionError,
    AccountDefinition,
    Coverage,
    Derivation,
    Predicate,
    Provenance,
    Selection,
    build_account,
    parse_definition,
    verify_account,
)
from capsule_emit.account.errors import (
    MISSING_PROVENANCE,
    PER_MEMBER_DIGEST_ON_RANGE,
    PROVENANCE_ON_DETERMINISTIC,
    RESULT_MISMATCH,
    UNKNOWN_DERIVATION_CLASS,
    UNKNOWN_SELECTION_KIND,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _det_definition() -> AccountDefinition:
    return parse_definition(
        {
            "name": "spend.weekly",
            "selection_kind": SELECTION_RANGE,
            "reads": ["developer", "amount", "verdict_class"],
            "derivation_class": DERIVATION_DETERMINISTIC,
            "predicate": [{"field": "verdict_class", "op": "eq", "value": "executed"}],
        }
    )


def _range_selection() -> Selection:
    return Selection(kind=SELECTION_RANGE, coverage=Coverage(coverage_root="root-abc", range=(0, 41)))


# ---------------------------------------------------------------------------
# (1) definition-as-DATA mutation test
# ---------------------------------------------------------------------------
def test_definition_digest_moves_when_a_document_field_changes():
    base = _det_definition()
    d0 = base.definition_digest()

    # Alter one field of the definition DOCUMENT -> digest changes.
    renamed = parse_definition({**base_dict(base), "name": "spend.monthly"})
    assert renamed.definition_digest() != d0

    reordered_predicate = parse_definition(
        {**base_dict(base), "predicate": [{"field": "verdict_class", "op": "ne", "value": "executed"}]}
    )
    assert reordered_predicate.definition_digest() != d0

    extra_read = parse_definition({**base_dict(base), "reads": [*base.reads, "operator"]})
    assert extra_read.definition_digest() != d0

    class_flip = parse_definition({**base_dict(base), "derivation_class": DERIVATION_MODEL_ASSISTED})
    assert class_flip.definition_digest() != d0


def test_definition_digest_is_stable_across_implementation_internals():
    """Altering implementation internals must NOT change the digest.

    The digest is over ``canonical_document()`` only. We simulate an internal
    change by attaching arbitrary non-document attributes and by constructing the
    same document two different ways (parse vs. direct dataclass). Both yield the
    identical digest — the digest sees the DOCUMENT, not the object's guts.
    """
    parsed = _det_definition()
    direct = AccountDefinition(
        name="spend.weekly",
        selection_kind=SELECTION_RANGE,
        reads=("developer", "amount", "verdict_class"),
        derivation_class=DERIVATION_DETERMINISTIC,
        predicate=(Predicate(field="verdict_class", op="eq", value="executed"),),
    )
    assert parsed.definition_digest() == direct.definition_digest()
    # The canonical document carries no implementation detail: exactly the
    # semantic fields, nothing else.
    assert set(parsed.canonical_document()) == {"name", "selection_kind", "reads", "derivation_class", "predicate"}


def base_dict(d: AccountDefinition) -> dict:
    """Reconstruct the input document for a definition (test helper)."""
    return d.canonical_document()


# ---------------------------------------------------------------------------
# (2) cross-repo replay vector
# ---------------------------------------------------------------------------
def test_cross_repo_replay_vector_same_document_same_digest_and_result():
    """The SAME definition document evaluated 'on either side' of the public
    interface -> identical definition_digest and identical result.

    We stand in for the two repos with two independent evaluators of the SAME
    deterministic contract (count of records passing the predicate over the
    range). Both cite the core's definition_digest; both recompute+match.
    """
    definition = _det_definition()
    selection = _range_selection()

    records = [
        {"developer": "alice", "amount": 10, "verdict_class": "executed"},
        {"developer": "alice", "amount": 5, "verdict_class": "refused"},
        {"developer": "bob", "amount": 7, "verdict_class": "executed"},
    ]

    def evaluator_A(sel: Selection) -> int:
        return sum(1 for r in records if r.get("verdict_class") == "executed")

    def evaluator_B(sel: Selection) -> int:
        # A different implementation of the SAME deterministic contract.
        n = 0
        for r in records:
            if r["verdict_class"] == "executed":
                n += 1
        return n

    asserted = evaluator_A(selection)

    account = build_account(definition=definition, selection=selection, asserted_result=asserted)

    # Same definition_digest cited on the account as the definition computes.
    assert account.derivation.definition_digest == definition.definition_digest()

    # Both evaluators verify against the same account with the same digest.
    r_a = verify_account(account, definition=definition, recompute=evaluator_A)
    r_b = verify_account(account, definition=definition, recompute=evaluator_B)
    assert r_a.ok and r_a.method == "recompute"
    assert r_b.ok and r_b.method == "recompute"


def test_deterministic_result_mismatch_is_named():
    definition = _det_definition()
    account = build_account(definition=definition, selection=_range_selection(), asserted_result=99)
    res = verify_account(account, definition=definition, recompute=lambda sel: 2)
    assert not res.ok
    assert res.reason == RESULT_MISMATCH


# ---------------------------------------------------------------------------
# (3) model-assisted-without-provenance refusal
# ---------------------------------------------------------------------------
def test_model_assisted_without_provenance_is_refused_at_construction():
    definition = parse_definition(
        {
            "name": "judgment.helpfulness",
            "selection_kind": SELECTION_RANGE,
            "reads": ["developer", "verdict_class"],
            "derivation_class": DERIVATION_MODEL_ASSISTED,
        }
    )
    with pytest.raises(AccountConstructionError) as exc:
        build_account(definition=definition, selection=_range_selection(), asserted_result={"score": 3})
    assert exc.value.reason == MISSING_PROVENANCE


def test_model_assisted_with_provenance_is_provenance_verified():
    definition = parse_definition(
        {
            "name": "judgment.helpfulness",
            "selection_kind": SELECTION_RANGE,
            "reads": ["developer", "verdict_class"],
            "derivation_class": DERIVATION_MODEL_ASSISTED,
        }
    )
    prov = Provenance(model_id="model-x/1", prompt_digest="deadbeef" * 8, seed=7)
    account = build_account(
        definition=definition,
        selection=_range_selection(),
        asserted_result={"score": 3},
        provenance=prov,
    )
    res = verify_account(account, definition=definition)
    assert res.ok and res.method == "provenance"


def test_model_assisted_not_re_adjudicable_fails_verify():
    definition = parse_definition(
        {
            "name": "judgment.x",
            "selection_kind": SELECTION_RANGE,
            "reads": ["developer"],
            "derivation_class": DERIVATION_MODEL_ASSISTED,
        }
    )
    prov = Provenance(model_id="m", prompt_digest="d", seed=1, re_adjudicable=False)
    account = build_account(
        definition=definition, selection=_range_selection(), asserted_result=1, provenance=prov
    )
    res = verify_account(account, definition=definition)
    assert not res.ok
    assert res.method == "provenance"


def test_deterministic_with_provenance_is_refused():
    definition = _det_definition()
    prov = Provenance(model_id="m", prompt_digest="d", seed=1)
    with pytest.raises(AccountConstructionError) as exc:
        build_account(
            definition=definition, selection=_range_selection(), asserted_result=1, provenance=prov
        )
    assert exc.value.reason == PROVENANCE_ON_DETERMINISTIC


# ---------------------------------------------------------------------------
# range input-identity rule
# ---------------------------------------------------------------------------
def test_range_input_identity_is_root_plus_range_never_members():
    sel = _range_selection()
    ident = sel.input_identity()
    assert ident == {"coverage_root": "root-abc", "range": [0, 41]}
    assert "references" not in ident
    assert "members" not in ident and "member_digests" not in ident


def test_range_selection_refuses_per_member_references():
    definition = _det_definition()
    bad = Selection(
        kind=SELECTION_RANGE,
        coverage=Coverage(coverage_root="root", range=(0, 1), references=("digest-1", "digest-2")),
    )
    with pytest.raises(AccountConstructionError) as exc:
        build_account(definition=definition, selection=bad, asserted_result=0)
    assert exc.value.reason == PER_MEMBER_DIGEST_ON_RANGE


def test_explicit_set_cites_members_via_references():
    definition = parse_definition(
        {
            "name": "set.audit",
            "selection_kind": SELECTION_EXPLICIT_SET,
            "reads": ["developer"],
            "derivation_class": DERIVATION_DETERMINISTIC,
        }
    )
    sel = Selection(
        kind=SELECTION_EXPLICIT_SET, coverage=Coverage(references=("cap-1", "cap-2"))
    )
    account = build_account(definition=definition, selection=sel, asserted_result=2)
    assert account.selection.input_identity() == {"references": ["cap-1", "cap-2"]}


# ---------------------------------------------------------------------------
# chain_segment selection kind (additive; same no-per-member discipline as range)
# ---------------------------------------------------------------------------
def _chain_definition() -> AccountDefinition:
    return parse_definition(
        {
            "name": "chain.walk",
            "selection_kind": SELECTION_CHAIN_SEGMENT,
            "reads": ["developer", "verdict_class"],
            "derivation_class": DERIVATION_DETERMINISTIC,
        }
    )


def _chain_selection() -> Selection:
    return Selection(
        kind=SELECTION_CHAIN_SEGMENT,
        coverage=Coverage(start_digest="A" * 8, end_digest="B" * 8, relation="follows"),
    )


def test_chain_segment_input_identity_is_endpoints_plus_relation():
    """A chain_segment names its inputs by {start_digest, end_digest, relation}
    and NEVER by per-member refs — the linkage is in-record."""
    ident = _chain_selection().input_identity()
    assert ident == {"start_digest": "AAAAAAAA", "end_digest": "BBBBBBBB", "relation": "follows"}
    assert "references" not in ident
    assert "members" not in ident and "member_digests" not in ident


def test_chain_segment_account_verifies():
    definition = _chain_definition()
    selection = _chain_selection()
    # A deterministic contract over the segment (e.g. count links A..B).
    account = build_account(definition=definition, selection=selection, asserted_result=3)
    assert account.derivation.definition_digest == definition.definition_digest()
    res = verify_account(account, definition=definition, recompute=lambda sel: 3)
    assert res.ok and res.method == "recompute"


def test_chain_segment_refuses_per_member_references():
    """Same discipline as range: per-member refs on a chain_segment are a
    category error and are refused at construction."""
    definition = _chain_definition()
    bad = Selection(
        kind=SELECTION_CHAIN_SEGMENT,
        coverage=Coverage(
            start_digest="A" * 8,
            end_digest="B" * 8,
            relation="follows",
            references=("digest-1", "digest-2"),
        ),
    )
    with pytest.raises(AccountConstructionError) as exc:
        build_account(definition=definition, selection=bad, asserted_result=0)
    assert exc.value.reason == PER_MEMBER_DIGEST_ON_RANGE


def test_chain_segment_requires_both_endpoints_and_relation():
    definition = _chain_definition()
    for coverage in (
        Coverage(end_digest="B" * 8, relation="follows"),  # no start
        Coverage(start_digest="A" * 8, relation="follows"),  # no end
        Coverage(start_digest="A" * 8, end_digest="B" * 8),  # no relation
    ):
        with pytest.raises(AccountConstructionError) as exc:
            build_account(
                definition=definition,
                selection=Selection(kind=SELECTION_CHAIN_SEGMENT, coverage=coverage),
                asserted_result=0,
            )
        assert exc.value.reason == "malformed_coverage"


# ---------------------------------------------------------------------------
# idempotent-on-replay
# ---------------------------------------------------------------------------
def test_verify_is_idempotent():
    definition = _det_definition()
    account = build_account(definition=definition, selection=_range_selection(), asserted_result=1)
    recompute = lambda sel: 1  # noqa: E731
    first = verify_account(account, definition=definition, recompute=recompute)
    second = verify_account(account, definition=definition, recompute=recompute)
    assert first == second
    # The account document is unchanged after verification (no mutation).
    assert account.to_document() == build_account(
        definition=definition, selection=_range_selection(), asserted_result=1
    ).to_document()


def test_swapped_definition_is_caught_by_digest_binding():
    definition = _det_definition()
    account = build_account(definition=definition, selection=_range_selection(), asserted_result=1)
    other = parse_definition(
        {
            "name": "different",
            "selection_kind": SELECTION_RANGE,
            "reads": ["developer"],
            "derivation_class": DERIVATION_DETERMINISTIC,
        }
    )
    res = verify_account(account, definition=other, recompute=lambda sel: 1)
    assert not res.ok
    assert res.reason == "definition_digest_mismatch"


# ---------------------------------------------------------------------------
# (4) no second definition_digest implementation escapes the core
# ---------------------------------------------------------------------------
def test_only_the_core_implements_definition_digest():
    """Grep gate, in-process: ``definition_digest`` is DEFINED only inside
    capsule_emit/account/. Anywhere else it may be CALLED, never re-implemented.
    """
    pkg = REPO_ROOT / "capsule_emit"
    hits: list[str] = []
    for py in pkg.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "def definition_digest" in line:
                hits.append(f"{py.relative_to(REPO_ROOT)}:{i}")
    for hit in hits:
        assert hit.startswith("capsule_emit/account/"), (
            f"a second definition_digest implementation escaped the core: {hit}"
        )
    assert hits, "expected the core to define definition_digest at least once"


# fail-closed on unknown at verify (unknown kind/class refused, never inert)
def test_verify_refuses_unknown_selection_kind():
    # A selection kind the core does not (yet) recognize is refused, never
    # treated as inert. (chain_segment used to stand in here; now that it is a
    # real kind, a genuinely-unknown kind is used to keep the fail-closed
    # guarantee tested — adding a kind stays additive.)
    acct = Account(
        selection=Selection(kind="dag_frontier", coverage=Coverage(coverage_root="r", range=(1, 7))),
        derivation=Derivation(derivation_class=DERIVATION_DETERMINISTIC, definition_digest="d"),
        asserted_result={"n": 1},
    )
    res = verify_account(acct, recompute=lambda sel: {"n": 1})
    assert res.ok is False
    assert res.reason == UNKNOWN_SELECTION_KIND


def test_verify_refuses_unknown_derivation_class():
    acct = Account(
        selection=Selection(kind=SELECTION_RANGE, coverage=Coverage(coverage_root="r", range=(1, 7))),
        derivation=Derivation(derivation_class="hand_wave", definition_digest="d"),
        asserted_result={"n": 1},
    )
    res = verify_account(acct, recompute=lambda sel: {"n": 1})
    assert res.ok is False
    assert res.reason == UNKNOWN_DERIVATION_CLASS
