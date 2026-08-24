# SPDX-License-Identifier: Apache-2.0
"""O16 audit item 9: '#logged @ leaf N' repr.

``seal()``'s log is meant to be ambient -- every capsule is already a leaf in
its ledger before any checkpoint (frozen v4 surface §2.1). ``EmitResult``
carries this as ``.seq`` (1-indexed position in the ledger file), and both
``repr(cap)`` and ``capsule-emit ledger show`` render it as
``#logged @ leaf <seq>``.
"""
from __future__ import annotations

import io

import pytest

from capsule_emit import ledger_show, seal
from capsule_emit.cli import main as cli_main
from capsule_emit.ledger import append_to_ledger


@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "test.jsonl"


def _seal(ledger, **overrides):
    kwargs = dict(
        action="write_order",
        operator="acme-co",
        developer="agent@v1",
        agent_output={"po": "PO-001"},
        verdict="executed",
        anchor=False,
        ledger=ledger,
    )
    kwargs.update(overrides)
    return seal({"vendor": "Frobozz"}, **kwargs)


def test_seq_is_1_indexed_position_in_ledger(tmp_ledger):
    first = _seal(tmp_ledger)
    second = _seal(tmp_ledger)
    assert first.seq == 1
    assert second.seq == 2


def test_repr_includes_logged_at_leaf(tmp_ledger):
    cap = _seal(tmp_ledger)
    assert repr(cap) == (
        f"EmitResult(capsule_id={cap.capsule_id!r}, anchored={cap.anchored}, "
        f"anchor_status={cap.anchor_status!r}) #logged @ leaf {cap.seq}"
    )
    assert "#logged @ leaf 1" in repr(cap)


def test_second_capsule_reprs_its_own_leaf(tmp_ledger):
    _seal(tmp_ledger)
    second = _seal(tmp_ledger)
    assert "#logged @ leaf 2" in repr(second)


def test_append_to_ledger_returns_seq(tmp_ledger):
    assert append_to_ledger({"capsule_id": "a"}, tmp_ledger) == 1
    assert append_to_ledger({"capsule_id": "b"}, tmp_ledger) == 2


def test_ledger_show_renders_logged_at_leaf(tmp_ledger):
    first = _seal(tmp_ledger)
    second = _seal(tmp_ledger)

    buf = io.StringIO()
    ledger_show(tmp_ledger, first.capsule_id, out=buf)
    assert "#logged @ leaf 1" in buf.getvalue()

    buf2 = io.StringIO()
    ledger_show(tmp_ledger, second.capsule_id, out=buf2)
    assert "#logged @ leaf 2" in buf2.getvalue()


def test_cli_ledger_show_renders_logged_at_leaf(tmp_ledger, capsys):
    cap = _seal(tmp_ledger)
    exit_code = cli_main(["ledger", "show", str(tmp_ledger), cap.capsule_id])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "#logged @ leaf 1" in out
