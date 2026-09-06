# SPDX-License-Identifier: Apache-2.0
"""[mesh-ledger-store-migration] capsule_emit.ledger learns the store layout.

``read_ledger``/``read_ledger_entries`` (and therefore ``view``/``view_chains``/
``show``) detect a ``cll.ledger.store.LedgerStore`` directory (``manifest.json``
present) and read it via ``cll.ledger.store``/``cll.ledger.segments`` directly,
with the exact same capsule-only/raw-entries contract a flat JSONL ledger
already has. This is the "ledger show == store layout" capability the mesh
Finder's acceptance line depends on -- capsule-emit-mesh's own consumer test
suite is the other half.
"""
from __future__ import annotations

import json

import pytest
from cll.ledger.store import LedgerStore

from capsule_emit import ledger, seal


def _capsule_ids(records: list[dict]) -> list[str]:
    return [r["capsule_id"] for r in records]


@pytest.fixture
def sealed_capsules(tmp_path):
    """Seal once, to a throwaway scratch file -- the ONE canonical capsule
    set both the flat and store fixtures below import, so they carry
    byte-identical content (two independent seal() calls would each mint a
    fresh action_id UUID/timestamp and never compare equal)."""
    scratch = tmp_path / "sealed-scratch.jsonl"
    return [
        seal(
            {"n": i},
            action=f"act-{i}",
            operator="acme",
            developer="agent@v1",
            anchor=False,
            ledger=scratch,
        ).capsule
        for i in range(3)
    ]


@pytest.fixture
def flat_ledger(tmp_path, sealed_capsules):
    path = tmp_path / "flat.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for cap in sealed_capsules:
            fh.write(json.dumps(cap, separators=(",", ":")) + "\n")
    return path, sealed_capsules


@pytest.fixture
def store_ledger(tmp_path, sealed_capsules):
    """A cll.ledger.store.LedgerStore built the way capsule-emit-mesh's own
    ledger_store_backend.open_ledger_store does (rotate_at_checkpoint=True),
    holding the SAME capsules as ``flat_ledger`` for content comparison."""
    ledger_dir = tmp_path / "store"
    store = LedgerStore(root=ledger_dir, rotate_at_checkpoint=True)
    try:
        for cap in sealed_capsules:
            store.append(cap, consequential=False)
    finally:
        store.close()
    return ledger_dir, sealed_capsules


def test_is_ledger_store_detects_manifest(tmp_path, store_ledger):
    ledger_dir, _caps = store_ledger
    assert ledger.is_ledger_store(ledger_dir) is True


def test_is_ledger_store_false_for_flat_file(flat_ledger):
    path, _caps = flat_ledger
    assert ledger.is_ledger_store(path) is False


def test_is_ledger_store_false_for_plain_directory(tmp_path):
    plain_dir = tmp_path / "not-a-store"
    plain_dir.mkdir()
    assert ledger.is_ledger_store(plain_dir) is False


def test_read_ledger_over_store_matches_flat_equivalent(flat_ledger, store_ledger):
    flat_path, _flat_caps = flat_ledger
    store_dir, sealed_caps = store_ledger

    from_flat = ledger.read_ledger(flat_path)
    from_store = ledger.read_ledger(store_dir)
    assert _capsule_ids(from_flat) == _capsule_ids(sealed_caps)
    assert from_store == from_flat


def test_read_ledger_entries_over_store_excludes_no_bookkeeping_by_default(store_ledger):
    ledger_dir, caps = store_ledger
    entries = ledger.read_ledger_entries(ledger_dir)
    assert _capsule_ids(entries) == _capsule_ids(caps)
    assert all(e.get("kind") is None for e in entries)


def test_view_and_show_work_against_a_store_directory(store_ledger, capsys):
    ledger_dir, caps = store_ledger
    ledger.view(ledger_dir)
    out = capsys.readouterr().out
    for cap in caps:
        assert cap["capsule_id"][:14] in out

    found = ledger.show(ledger_dir, caps[0]["capsule_id"])
    assert found is True
    show_out = capsys.readouterr().out
    assert caps[0]["capsule_id"] in show_out


def test_rotation_and_archival_reported_never_raises(tmp_path):
    """Build-item 4 (shared with capsule-emit-mesh): force a checkpoint ->
    new segment; unmount one -> read_ledger_entries reports it archived,
    never raises SegmentUnmounted, and read_ledger (capsule-only) excludes
    the marker entirely."""
    from cll.checkpoint.index import MmrLedger
    from cll.ledger.segments import MmrCheckpointer

    ledger_dir = tmp_path / "rotating"
    store = LedgerStore(root=ledger_dir, rotate_at_checkpoint=True)
    store._max_segment_bytes = 200

    class _FixedSigner:
        key_id = "00" * 32

        def sign(self, digest_hex: str) -> str:
            return "00" * 64

    mmr = MmrLedger(store)
    store.set_checkpointer(MmrCheckpointer(mmr=mmr, signer=_FixedSigner(), log_id="rotation-demo"))

    capsules = [{"capsule_id": f"{i:064x}", "n": i, "payload": "x" * 40} for i in range(30)]
    for cap in capsules:
        store.append(cap)

    closed = [s for s in store.list_segments() if s.manifest is not None]
    assert closed, "expected at least one rotation to have fired"
    to_unmount = closed[0]
    store.unmount_segment(to_unmount.name)
    store.close()

    entries = ledger.read_ledger_entries(ledger_dir)
    archived = [e for e in entries if e.get("kind") == ledger.ARCHIVED_SEGMENT_KIND]
    assert len(archived) == 1
    assert archived[0]["segment"] == to_unmount.name
    assert archived[0]["note"] == "archived -- mount to view"
    assert archived[0]["record_count"] > 0

    capsule_entries = [e for e in entries if e.get("kind") is None]
    total_visible = len(capsule_entries) + archived[0]["record_count"]
    assert total_visible == len(capsules)

    # read_ledger (capsule-only) never leaks the marker.
    capsule_only = ledger.read_ledger(ledger_dir)
    assert all(c.get("kind") != ledger.ARCHIVED_SEGMENT_KIND for c in capsule_only)
    assert len(capsule_only) == len(capsule_entries)


def test_show_labels_archived_segments_instead_of_a_flat_not_found(tmp_path, capsys):
    from cll.checkpoint.index import MmrLedger
    from cll.ledger.segments import MmrCheckpointer

    ledger_dir = tmp_path / "rotating-show"
    store = LedgerStore(root=ledger_dir, rotate_at_checkpoint=True)
    store._max_segment_bytes = 200

    class _FixedSigner:
        key_id = "00" * 32

        def sign(self, digest_hex: str) -> str:
            return "00" * 64

    mmr = MmrLedger(store)
    store.set_checkpointer(MmrCheckpointer(mmr=mmr, signer=_FixedSigner(), log_id="rotation-demo"))
    capsules = [{"capsule_id": f"{i:064x}", "n": i, "payload": "x" * 40} for i in range(30)]
    for cap in capsules:
        store.append(cap)
    closed = [s for s in store.list_segments() if s.manifest is not None]
    to_unmount = closed[0]
    store.unmount_segment(to_unmount.name)
    store.close()

    found = ledger.show(ledger_dir, "ff" * 32)  # never sealed, and may be archived
    assert found is False
    out = capsys.readouterr().out
    assert "archived" in out
    assert "mount to view" in out
