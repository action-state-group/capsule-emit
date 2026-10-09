# SPDX-License-Identifier: Apache-2.0
"""capsule-emit's writers emit only the five registered chain.relation values
(agent-action-capsule REGISTRY.md section 6).

Three checks:
- the registered set here is exactly the registry's five;
- every relation capsule-emit's source writes -- a ``relation=`` /
  ``chain_relation=`` keyword literal, a ``Chain(relation=...)`` literal, or a
  module constant named for a relation -- is registered (legacy tokens live
  only in names marked LEGACY and in ``LEGACY_RELATION_ALIASES``);
- each high-level writer, run, writes a registered relation.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from capsule_emit.relations import LEGACY_RELATION_ALIASES, REGISTERED_RELATIONS, registered_meaning

PACKAGE = Path(__file__).resolve().parent.parent / "capsule_emit"
REGISTRY_SECTION_6 = {"follows", "confirms", "supersedes", "epoch_opens", "duplicates"}


def test_the_registered_set_is_the_registrys():
    assert REGISTERED_RELATIONS == REGISTRY_SECTION_6


def test_every_legacy_alias_maps_to_a_registered_value():
    assert not (set(LEGACY_RELATION_ALIASES) & REGISTERED_RELATIONS)
    assert set(LEGACY_RELATION_ALIASES.values()) <= REGISTERED_RELATIONS
    assert registered_meaning("adjudicates") == "confirms"
    assert registered_meaning("escalates") == "supersedes"
    assert registered_meaning("follows") == "follows"
    assert registered_meaning("something-else") is None


def _written_relations():
    """(file:line, value) for every string literal capsule-emit's source
    passes as a relation, or binds to a relation-named constant."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(PACKAGE.parent)

        def where(node, rel=rel):
            return f"{rel}:{node.lineno}"

        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg in ("relation", "chain_relation"):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    found.append((where(node.value), node.value.value))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                value = node.value
                if not names or not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                for name in names:
                    upper = name.upper()
                    if "LEGACY" in upper:
                        continue
                    if "RELATION" in upper or upper in ("SUPERSEDES", "CONFIRMS", "FOLLOWS"):
                        found.append((where(value), value.value))
    return found


def test_every_relation_capsule_emit_writes_is_registered():
    written = _written_relations()
    assert written, "the scan found no relation literals at all"
    unregistered = [(at, value) for at, value in written if value not in REGISTERED_RELATIONS]
    assert not unregistered, f"unregistered chain.relation written at: {unregistered}"


def _parent(ledger):
    from capsule_emit import seal

    return seal(None, action="parent", operator="org", developer="agent@v1", anchor=False, ledger=ledger).capsule_id


def _writers(ledger):
    """Each high-level writer, run once against a fresh parent."""
    from capsule_emit import seal
    from capsule_emit.adjudication import VERDICT_CORROBORATED, seal_adjudication
    from capsule_emit.approval import seal_approval
    from capsule_emit.core import _emit_capsule

    blocked = _emit_capsule(
        action="write_po", operator="org", developer="agent@v1", verdict="blocked",
        effect={"type": "write_po", "status": "planned"}, anchor=False, ledger=ledger,
    ).capsule_id
    yield "seal(confirms=...)", seal(None, action="a", operator="org", developer="d", confirms=_parent(ledger), anchor=False, ledger=ledger)
    yield "seal(relation=None)", seal(None, action="a", operator="org", developer="d", confirms=_parent(ledger), relation=None, anchor=False, ledger=ledger)
    yield "seal_approval(approve)", seal_approval(blocked, "alice@org.example", "approve", "d1", ledger=ledger)
    yield "seal_adjudication", seal_adjudication(
        half_a_capsule_id=_parent(ledger), half_b_capsule_id=_parent(ledger), verdict=VERDICT_CORROBORATED,
        margin=1.0, margin_tau=1.0, ledger=ledger, anchor=False, operator="org", developer="referee@v1",
    )


def test_each_high_level_writer_writes_a_registered_relation(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    seen = {}
    for name, result in _writers(ledger):
        seen[name] = result.capsule["chain"]["relation"]
    assert set(seen.values()) <= REGISTERED_RELATIONS, seen
    assert seen == {
        "seal(confirms=...)": "confirms",
        "seal(relation=None)": "follows",
        "seal_approval(approve)": "supersedes",
        "seal_adjudication": "confirms",
    }


@pytest.mark.parametrize("token", sorted(LEGACY_RELATION_ALIASES))
def test_no_writer_accepts_a_legacy_token(tmp_path, token):
    from capsule_emit import seal

    ledger = tmp_path / "ledger.jsonl"
    with pytest.raises(ValueError, match="not a registered chain.relation"):
        seal(None, action="a", operator="org", developer="d", confirms=_parent(ledger), relation=token, anchor=False, ledger=ledger)
