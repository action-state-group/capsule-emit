# SPDX-License-Identifier: Apache-2.0
"""``confirms=`` names a same-stream parent in ``chain`` and cites any other
capsule in ``references[]`` (agent-action-capsule draft-05, Cross-record
references): ``chain`` is only ever the producer's own same-stream parent."""
from __future__ import annotations

import pytest
from agent_action_capsule.contracts import InvariantError, ReferenceEntry

from capsule_emit import seal
from capsule_emit.ledger import read_ledger

KW = {"action": "write_po", "operator": "acme-co", "developer": "po-agent@v1", "anchor": False}


def _foreign(tmp_path):
    """A capsule sealed into another organization's ledger."""
    return seal({"po_id": "PO-7781"}, ledger=tmp_path / "theirs.jsonl", **{**KW, "operator": "other-co"}).capsule_id


def test_same_stream_target_is_the_chain_parent(tmp_path):
    ledger = tmp_path / "ours.jsonl"
    attempt = seal({"po_id": "PO-7781"}, ledger=ledger, **KW)
    done = seal({"po_id": "PO-7781"}, ledger=ledger, verdict="confirmed", confirms=attempt.capsule_id, **KW)

    assert done.capsule["chain"] == {"parent_capsule_id": attempt.capsule_id, "relation": "confirms"}
    assert "references" not in done.capsule


def test_cross_stream_target_is_a_references_entry(tmp_path):
    theirs = _foreign(tmp_path)
    ledger = tmp_path / "ours.jsonl"
    done = seal(
        {"po_id": "PO-7781"}, ledger=ledger, verdict="confirmed",
        effect={"type": "write_po", "status": "confirmed"}, confirms=theirs, **KW,
    )

    assert "chain" not in done.capsule
    assert done.capsule["references"] == [{
        "type": "agent-action-capsule",
        "digest_alg": "SHA-256",
        "digest": theirs,
        "citation_purpose": "responds_to",
    }]
    # The citation is part of what the capsule_id and signature commit to.
    assert read_ledger(ledger)[-1]["references"] == done.capsule["references"]
    # A confirmed effect still derives its response digest from the target.
    assert done.capsule["effect"]["response_digest"]


@pytest.mark.parametrize("relation", ["supersedes", "follows", None])
def test_every_relation_cites_a_cross_stream_target(tmp_path, relation):
    """No caller breaks: a relation that only has meaning inside one stream is
    not written, and the target is cited instead."""
    theirs = _foreign(tmp_path)
    result = seal(None, ledger=tmp_path / "ours.jsonl", confirms=theirs, relation=relation, **KW)

    assert "chain" not in result.capsule
    assert [r["digest"] for r in result.capsule["references"]] == [theirs]


def test_cross_stream_citation_joins_caller_references(tmp_path):
    theirs = _foreign(tmp_path)
    other = ReferenceEntry(type="agent-action-capsule", digest_alg="SHA-256", digest="b" * 64,
                           citation_purpose="acted_on")
    result = seal(None, ledger=tmp_path / "ours.jsonl", confirms=theirs, references=(other,), **KW)

    assert [(r["digest"], r["citation_purpose"]) for r in result.capsule["references"]] == [
        ("b" * 64, "acted_on"),
        (theirs, "responds_to"),
    ]


def test_a_caller_citation_of_the_target_is_not_repeated(tmp_path):
    theirs = _foreign(tmp_path)
    mine = ReferenceEntry(type="agent-action-capsule", digest_alg="SHA-256", digest=theirs,
                          citation_purpose="acted_on")
    result = seal(None, ledger=tmp_path / "ours.jsonl", confirms=theirs, references=(mine,), **KW)

    assert result.capsule["references"] == [{
        "type": "agent-action-capsule", "digest_alg": "SHA-256", "digest": theirs, "citation_purpose": "acted_on",
    }]


def test_a_target_that_is_not_a_capsule_id_is_still_rejected(tmp_path):
    with pytest.raises(InvariantError, match="64-hex"):
        seal(None, ledger=tmp_path / "ours.jsonl", confirms="not-a-capsule-id", **KW)
